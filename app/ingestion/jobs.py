"""A job queue in Postgres.

Why not Redis: a job is inserted in the same transaction as the change that needs it
(approving, editing, or archiving a source). Both commit or neither does, so there is
no window where the source is approved but its job was lost. With an external queue
you would commit first and enqueue second, and a crash in between loses the job.

How several workers share the queue safely: `claim_next` uses
SELECT ... FOR UPDATE SKIP LOCKED. FOR UPDATE locks the row it picks; SKIP LOCKED makes
other workers pass over rows already locked instead of waiting on them. Each worker
therefore gets a different job, with no coordinator.

How crashes are handled: a claimed job gets a lease (`locked_until`). A worker that
dies leaves the job "running" with a lease that runs out, and the next claim picks it
up again. `attempts` counts every claim, so a job that kills its worker every time
still ends up failed instead of looping forever.
"""

import uuid
from datetime import timedelta

from sqlalchemy import and_, exists, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ingestion import IngestionJob, JobStatus

# Retry delay doubles each attempt: 10s, 20s, 40s, ... capped at one hour.
BACKOFF_BASE_SECONDS = 10
BACKOFF_CAP_SECONDS = 3_600
# Long tracebacks are trimmed; the full one goes to the worker log.
ERROR_MAX_CHARS = 2_000


async def enqueue_sync(
    session: AsyncSession, *, workspace_id: uuid.UUID, source_id: uuid.UUID
) -> None:
    """Ask the worker to re-sync a source. Call it inside the transaction that changes
    the source; the caller commits.

    If a queued job for the source already exists, it is reused (the partial unique
    index allows only one), and pulled forward to run now with a fresh attempt count.
    The fresh count matters: if the last attempts failed because of the content, this
    edit may be the fix, and it deserves a full set of retries.
    """
    statement = (
        insert(IngestionJob)
        .values(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            source_id=source_id,
            status=JobStatus.queued.value,
            attempts=0,
        )
        .on_conflict_do_update(
            # Must name the same columns and WHERE as uq_ingestion_jobs_queued_source,
            # so Postgres knows which unique index the conflict refers to.
            index_elements=[IngestionJob.source_id],
            index_where=text("status = 'queued'"),
            set_={"run_after": func.now(), "attempts": 0},
        )
    )
    await session.execute(statement)


async def claim_next(session: AsyncSession, *, lease_seconds: int) -> IngestionJob | None:
    """Claim the oldest due job, or return None. Commits, so the claim is visible to
    other workers before the (possibly slow) work starts."""
    now = func.now()
    due = (
        select(IngestionJob.id)
        .where(
            or_(
                and_(IngestionJob.status == JobStatus.queued.value, IngestionJob.run_after <= now),
                # A running job past its lease was abandoned by a crashed worker.
                and_(
                    IngestionJob.status == JobStatus.running.value,
                    IngestionJob.locked_until < now,
                ),
            )
        )
        .order_by(IngestionJob.run_after)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    # One statement: pick a row and mark it ours. RETURNING hands back the updated row.
    statement = (
        update(IngestionJob)
        .where(IngestionJob.id == due)
        .values(
            status=JobStatus.running.value,
            attempts=IngestionJob.attempts + 1,
            locked_until=now + timedelta(seconds=lease_seconds),
            started_at=now,
        )
        .returning(IngestionJob)
        # The returned object comes straight from RETURNING; there is nothing loaded in
        # this session that needs syncing.
        .execution_options(synchronize_session=False)
    )
    job = (await session.scalars(statement)).one_or_none()
    await session.commit()
    return job


async def extend_lease(session: AsyncSession, job_id: uuid.UUID, *, lease_seconds: int) -> None:
    """Push the lease forward: "still working on it". The worker calls this after each
    embedding batch, so a large document can take longer than one lease in total
    without another worker deciding the job was abandoned and starting it again."""
    await session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job_id, IngestionJob.status == JobStatus.running.value)
        .values(locked_until=func.now() + timedelta(seconds=lease_seconds))
    )


async def mark_succeeded(
    session: AsyncSession, job_id: uuid.UUID, *, chunk_count: int, embedded_count: int
) -> None:
    """Call only after every phase finished, so a succeeded job means the source's
    chunks are written and embedded."""
    await session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job_id)
        .values(
            status=JobStatus.succeeded.value,
            chunk_count=chunk_count,
            embedded_count=embedded_count,
            locked_until=None,
            finished_at=func.now(),
            last_error=None,
        )
    )


def backoff(attempts: int) -> timedelta:
    return timedelta(
        seconds=min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_CAP_SECONDS)
    )


async def mark_attempt_failed(
    session: AsyncSession, job: IngestionJob, error: str, *, max_attempts: int
) -> str:
    """Schedule a retry with backoff, or give up. Returns the new status."""
    error = error[-ERROR_MAX_CHARS:]
    # An edit while this job ran already queued a newer job for the source. Putting
    # this one back in the queue would break the one-queued-job rule, and the newer
    # job re-syncs the latest content anyway, so this one just closes as failed.
    superseded = await session.scalar(
        select(
            exists().where(
                IngestionJob.source_id == job.source_id,
                IngestionJob.status == JobStatus.queued.value,
                IngestionJob.id != job.id,
            )
        )
    )
    if job.attempts >= max_attempts or superseded:
        status = JobStatus.failed.value
        values: dict[str, object] = {"finished_at": func.now()}
    else:
        status = JobStatus.queued.value
        values = {"run_after": func.now() + backoff(job.attempts)}
    await session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job.id)
        .values(status=status, locked_until=None, last_error=error, **values)
    )
    return status


async def latest_job(session: AsyncSession, source_id: uuid.UUID) -> IngestionJob | None:
    return await session.scalar(
        select(IngestionJob)
        .where(IngestionJob.source_id == source_id)
        .order_by(IngestionJob.created_at.desc(), IngestionJob.id.desc())
        .limit(1)
    )
