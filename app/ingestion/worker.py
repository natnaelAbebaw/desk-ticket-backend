"""The ingestion worker: `python -m app.ingestion.worker` (or `make worker`).

Runs separately from the API so chunking and slow embedding calls never hold up an
HTTP request. Several copies can run at once; SKIP LOCKED in jobs.claim_next keeps
them from taking the same job.

Each job goes through these steps, each in its own transaction:
1. claim: mark the job running and commit, so other workers see it is taken.
2. sync: make the chunk rows match the source's text, and commit. All or nothing: a
   half-written set of chunks is never visible.
3. embed: give chunks without a current vector one, in batches, committing after each
   batch and extending the lease each time (app/embeddings/embed.py).
4. finish: mark the job succeeded.
If any step fails, the failure is recorded in a separate transaction and the job
retries. Work already committed (synced rows, embedded batches) is kept, and the retry
skips it: sync keeps unchanged rows, and embed only picks rows still missing a vector.
"""

import asyncio
import logging
import signal
import traceback

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.embeddings import Embedder, build_embedder
from app.embeddings.embed import embed_source
from app.ingestion import jobs
from app.ingestion.sync import sync_source
from app.models.ingestion import IngestionJob

logger = logging.getLogger("app.ingestion")


async def process(
    session_factory: async_sessionmaker[AsyncSession],
    job: IngestionJob,
    settings: Settings,
    embedder: Embedder,
) -> None:
    async def still_working(session: AsyncSession) -> None:
        await jobs.extend_lease(session, job.id, lease_seconds=settings.ingestion_lease_seconds)

    try:
        async with session_factory() as session:
            result = await sync_source(session, job.source_id)
            await session.commit()
        # Runs for archived sources too: sync just deleted their chunks, so there is
        # nothing to embed and it returns 0 after one quick query.
        embedded = await embed_source(
            session_factory,
            job.source_id,
            embedder,
            batch_size=settings.embedding_batch_size,
            on_batch=still_working,
        )
        async with session_factory() as session:
            await jobs.mark_succeeded(
                session, job.id, chunk_count=result.chunk_count, embedded_count=embedded
            )
            await session.commit()
        logger.info(
            "ingested source %s: %d chunks (%d added, %d kept, %d removed), %d embedded",
            job.source_id,
            result.chunk_count,
            result.added,
            result.kept,
            result.removed,
            embedded,
        )
    # Deliberately broad: any error in one job must not stop the worker from serving
    # the others. asyncio.CancelledError is a BaseException, so shutdown still works.
    except Exception:
        logger.exception("ingestion failed for source %s (attempt %d)", job.source_id, job.attempts)
        async with session_factory() as session:
            status = await jobs.mark_attempt_failed(
                session,
                job,
                traceback.format_exc(),
                max_attempts=settings.ingestion_max_attempts,
            )
            await session.commit()
        logger.warning("job %s is now %s", job.id, status)


async def run_once(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, embedder: Embedder
) -> bool:
    """Claim and process one job. Returns False when nothing was due."""
    async with session_factory() as session:
        job = await jobs.claim_next(session, lease_seconds=settings.ingestion_lease_seconds)
    if job is None:
        return False
    # attempts counts claims. Past the limit means the job was reclaimed after its lease
    # ran out again and again, almost always because it crashes or hangs its worker.
    # Running it once more would likely do the same, so it fails without running.
    if job.attempts > settings.ingestion_max_attempts:
        async with session_factory() as session:
            await jobs.mark_attempt_failed(
                session,
                job,
                "Lease expired too many times; the job probably crashes its worker",
                max_attempts=settings.ingestion_max_attempts,
            )
            await session.commit()
        logger.error("job %s for source %s abandoned", job.id, job.source_id)
        return True
    await process(session_factory, job, settings, embedder)
    return True


async def run_forever(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: Embedder,
    stop: asyncio.Event,
) -> None:
    logger.info("ingestion worker started (embedding model %s)", embedder.model)
    while not stop.is_set():
        if await run_once(session_factory, settings, embedder):
            # Go straight to the next job while there is a backlog.
            continue
        try:
            # Sleep until the poll interval passes, or wake early if asked to stop.
            await asyncio.wait_for(stop.wait(), timeout=settings.ingestion_poll_seconds)
        except TimeoutError:
            pass
    logger.info("ingestion worker stopped")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    # Imported here, not at the top: app.db.session builds its engine on import, and
    # tests import this module with their own engine.
    from app.db.session import SessionLocal, engine

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # SIGTERM is what `docker stop` and Kubernetes send. Setting the event lets the
    # current job finish; the loop exits before claiming the next one.
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    # Built once and shared by every job, so its HTTP connection to Ollama is reused.
    embedder = build_embedder(settings)
    try:
        await run_forever(SessionLocal, settings, embedder, stop)
    finally:
        await embedder.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
