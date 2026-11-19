"""Ingestion: jobs are queued with the change that needs them, workers share the queue
safely, failures retry and then stop, and chunks always match the approved text.

The worker is driven directly with `run_once` instead of as a background process, so
each test controls exactly when a job runs.
"""

import asyncio
import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.ingestion import jobs, worker
from app.models.ingestion import IngestionJob, JobStatus, KnowledgeChunk

from helpers import Account, add_member, create_workspace, signup

SECTIONS = "# Setup\nInstall the app.\n\n# Billing\nPay monthly.\n\n# Support\nEmail us."


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(
        update={"ingestion_max_attempts": 3, "ingestion_lease_seconds": 60}
    )


class Desk:
    def __init__(self, client: AsyncClient, workspace_id: str, owner: Account, agent: Account):
        self.client = client
        self.workspace_id = workspace_id
        self.owner = owner
        self.agent = agent
        self.url = f"/api/v1/workspaces/{workspace_id}/knowledge"

    async def document(self, author: Account, body: str = SECTIONS) -> dict:
        response = await self.client.post(
            self.url, json={"title": "Guide", "body": body}, headers=author.headers
        )
        assert response.status_code == 201, response.text
        return response.json()

    async def call(self, method: str, path: str, json: dict | None = None) -> dict:
        response = await self.client.request(
            method, f"{self.url}{path}", json=json, headers=self.owner.headers
        )
        assert response.status_code < 300, response.text
        return response.json()


async def _desk(client: AsyncClient, slug: str = "acme") -> Desk:
    owner = await signup(client, f"owner@{slug}.test", "Owner")
    agent = await signup(client, f"agent@{slug}.test", "Agent")
    workspace_id = await create_workspace(client, owner, slug=slug)
    await add_member(client, workspace_id, owner, agent, role="agent")
    return Desk(client, workspace_id, owner, agent)


async def _jobs(factory: async_sessionmaker[AsyncSession], source_id: str) -> list[IngestionJob]:
    async with factory() as session:
        rows = await session.scalars(
            select(IngestionJob)
            .where(IngestionJob.source_id == uuid.UUID(source_id))
            .order_by(IngestionJob.created_at, IngestionJob.id)
        )
        return list(rows)


async def _chunks(
    factory: async_sessionmaker[AsyncSession], source_id: str
) -> list[KnowledgeChunk]:
    async with factory() as session:
        rows = await session.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.source_id == uuid.UUID(source_id))
            .order_by(KnowledgeChunk.position)
        )
        return list(rows)


async def _make_due(factory: async_sessionmaker[AsyncSession]) -> None:
    """Skip retry backoff so the next run_once picks the job up immediately."""
    async with factory() as session:
        await session.execute(
            update(IngestionJob)
            .where(IngestionJob.status == JobStatus.queued.value)
            .values(run_after=func.now())
        )
        await session.commit()


# Queueing


async def test_jobs_are_queued_only_for_retrievable_changes(
    client: AsyncClient, factory: async_sessionmaker[AsyncSession]
) -> None:
    desk = await _desk(client)
    published = await desk.document(desk.owner)
    assert [j.status for j in await _jobs(factory, published["id"])] == ["queued"]

    draft = await desk.document(desk.agent, body="Draft text")
    assert await _jobs(factory, draft["id"]) == []
    await desk.call("POST", f"/{draft['id']}/reject", {"note": "No"})
    assert await _jobs(factory, draft["id"]) == []

    pending = await desk.document(desk.agent, body="Pending text")
    await desk.call("POST", f"/{pending['id']}/approve")
    assert len(await _jobs(factory, pending["id"])) == 1


async def test_repeated_edits_share_one_queued_job(
    client: AsyncClient, factory: async_sessionmaker[AsyncSession]
) -> None:
    desk = await _desk(client)
    doc = await desk.document(desk.owner)
    for index in range(3):
        await desk.call("PATCH", f"/{doc['id']}", {"body": f"{SECTIONS}\n\nEdit {index}."})
    queued = await _jobs(factory, doc["id"])
    assert len(queued) == 1
    assert queued[0].status == "queued"


# Running


async def test_worker_builds_chunks_and_reports_them(
    client: AsyncClient, factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    desk = await _desk(client)
    doc = await desk.document(desk.owner)

    assert await worker.run_once(factory, settings) is True
    assert await worker.run_once(factory, settings) is False  # queue is empty now

    chunks = await _chunks(factory, doc["id"])
    assert [c.heading for c in chunks] == ["Guide > Setup", "Guide > Billing", "Guide > Support"]
    assert {str(c.workspace_id) for c in chunks} == {desk.workspace_id}
    assert {c.source_version for c in chunks} == {1}

    job = await desk.call("GET", f"/{doc['id']}/ingestion")
    assert job["status"] == "succeeded"
    assert job["chunk_count"] == 3
    assert job["attempts"] == 1

    page = await desk.call("GET", f"/{doc['id']}/chunks")
    assert page["total"] == 3
    assert page["items"][1]["content"] == "Pay monthly."


async def test_resync_keeps_unchanged_chunks(
    client: AsyncClient, factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    desk = await _desk(client)
    doc = await desk.document(desk.owner)
    await worker.run_once(factory, settings)
    before = {c.heading: c.id for c in await _chunks(factory, doc["id"])}

    edited = SECTIONS.replace("Pay monthly.", "Pay yearly.")
    await desk.call("PATCH", f"/{doc['id']}", {"body": edited})
    await worker.run_once(factory, settings)
    after = await _chunks(factory, doc["id"])

    by_heading = {c.heading: c for c in after}
    # Untouched sections keep their rows (and, later, their embeddings).
    assert by_heading["Guide > Setup"].id == before["Guide > Setup"]
    assert by_heading["Guide > Support"].id == before["Guide > Support"]
    # The edited section is a new row with the new text.
    assert by_heading["Guide > Billing"].id != before["Guide > Billing"]
    assert by_heading["Guide > Billing"].content == "Pay yearly."
    assert {c.source_version for c in after} == {2}


async def test_archiving_removes_chunks(
    client: AsyncClient, factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    desk = await _desk(client)
    doc = await desk.document(desk.owner)
    await worker.run_once(factory, settings)
    assert len(await _chunks(factory, doc["id"])) == 3

    await desk.call("POST", f"/{doc['id']}/archive")
    await worker.run_once(factory, settings)
    assert await _chunks(factory, doc["id"]) == []


async def test_ticket_answers_are_chunked_after_approval(
    client: AsyncClient, factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    desk = await _desk(client)
    tickets = f"/api/v1/workspaces/{desk.workspace_id}/tickets"
    ticket = await client.post(tickets, json={"subject": "Reset"}, headers=desk.agent.headers)
    ticket_id = ticket.json()["id"]
    await client.patch(
        f"{tickets}/{ticket_id}", json={"status": "resolved"}, headers=desk.agent.headers
    )
    proposed = await client.post(
        f"{tickets}/{ticket_id}/knowledge",
        json={"title": "Reset", "question": "How do I reset?", "body": "Use Settings."},
        headers=desk.agent.headers,
    )
    source_id = proposed.json()["id"]
    assert await worker.run_once(factory, settings) is False  # pending: nothing to do

    await desk.call("POST", f"/{source_id}/approve")
    await worker.run_once(factory, settings)
    [chunk] = await _chunks(factory, source_id)
    assert chunk.content == "Question: How do I reset?\n\nAnswer: Use Settings."


# Sharing the queue


async def test_concurrent_claims_never_take_the_same_job(
    client: AsyncClient, factory: async_sessionmaker[AsyncSession]
) -> None:
    desk = await _desk(client)
    await desk.document(desk.owner, body="One")
    await desk.document(desk.owner, body="Two")

    async def claim() -> IngestionJob | None:
        async with factory() as session:
            return await jobs.claim_next(session, lease_seconds=60)

    # Three workers race for two jobs: two win different jobs, the third gets nothing.
    results = await asyncio.gather(claim(), claim(), claim())
    claimed = [job.id for job in results if job is not None]
    assert len(claimed) == 2
    assert len(set(claimed)) == 2


async def test_expired_lease_is_reclaimed(
    client: AsyncClient, factory: async_sessionmaker[AsyncSession]
) -> None:
    desk = await _desk(client)
    doc = await desk.document(desk.owner)
    async with factory() as session:
        first = await jobs.claim_next(session, lease_seconds=60)
    assert first is not None

    async with factory() as session:
        # Still leased: nobody else can take it.
        assert await jobs.claim_next(session, lease_seconds=60) is None
        # Simulate the worker dying: the lease runs out.
        await session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == first.id)
            .values(locked_until=func.now() - timedelta(seconds=1))
        )
        await session.commit()
        again = await jobs.claim_next(session, lease_seconds=60)
    assert again is not None
    assert again.id == first.id
    assert again.attempts == 2
    assert str(again.source_id) == doc["id"]


# Failures


async def test_failures_retry_with_backoff_then_give_up(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(session: AsyncSession, source_id: uuid.UUID) -> None:
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(worker, "sync_source", broken)
    desk = await _desk(client)
    doc = await desk.document(desk.owner)

    await worker.run_once(factory, settings)
    [job] = await _jobs(factory, doc["id"])
    assert job.status == "queued"
    assert job.attempts == 1
    assert "parser exploded" in (job.last_error or "")
    # Backoff: not due yet, so an immediate run finds nothing.
    assert await worker.run_once(factory, settings) is False

    for _ in range(settings.ingestion_max_attempts - 1):
        await _make_due(factory)
        await worker.run_once(factory, settings)
    [job] = await _jobs(factory, doc["id"])
    assert job.status == "failed"
    assert job.attempts == settings.ingestion_max_attempts
    assert job.finished_at is not None


async def test_job_that_keeps_killing_workers_is_abandoned(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def must_not_run(session: AsyncSession, source_id: uuid.UUID) -> None:
        raise AssertionError("an abandoned job must not run again")

    desk = await _desk(client)
    doc = await desk.document(desk.owner)
    async with factory() as session:
        # As if max_attempts workers each claimed it and died before finishing.
        await session.execute(
            update(IngestionJob)
            .where(IngestionJob.source_id == uuid.UUID(doc["id"]))
            .values(
                status=JobStatus.running.value,
                attempts=settings.ingestion_max_attempts,
                locked_until=func.now() - timedelta(seconds=1),
            )
        )
        await session.commit()

    monkeypatch.setattr(worker, "sync_source", must_not_run)
    assert await worker.run_once(factory, settings) is True
    [job] = await _jobs(factory, doc["id"])
    assert job.status == "failed"
    assert "Lease expired" in (job.last_error or "")


async def test_failed_run_after_an_edit_defers_to_the_newer_job(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    desk = await _desk(client)
    doc = await desk.document(desk.owner)
    async with factory() as session:
        running = await jobs.claim_next(session, lease_seconds=60)
    assert running is not None

    # An edit lands while the job runs, which queues a second job.
    await desk.call("PATCH", f"/{doc['id']}", {"body": "New text"})
    async with factory() as session:
        status = await jobs.mark_attempt_failed(
            session, running, "boom", max_attempts=settings.ingestion_max_attempts
        )
        await session.commit()
    # Re-queueing it would break the one-queued-job rule; the newer job covers it.
    assert status == "failed"
    assert [j.status for j in await _jobs(factory, doc["id"])] == ["failed", "queued"]

    await worker.run_once(factory, settings)
    [chunk] = await _chunks(factory, doc["id"])
    assert chunk.content == "New text"


def test_backoff_doubles_and_caps() -> None:
    assert [jobs.backoff(n).total_seconds() for n in (1, 2, 3, 4)] == [10, 20, 40, 80]
    assert jobs.backoff(30).total_seconds() == jobs.BACKOFF_CAP_SECONDS


# Isolation


async def test_outsiders_cannot_read_chunks_or_jobs(
    client: AsyncClient, factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    desk = await _desk(client)
    doc = await desk.document(desk.owner)
    await worker.run_once(factory, settings)
    other = await _desk(client, slug="globex")
    for path in ("chunks", "ingestion"):
        response = await client.get(f"{desk.url}/{doc['id']}/{path}", headers=other.owner.headers)
        assert response.status_code == 404
        # Their own workspace path with our source id is 404 too.
        response = await client.get(f"{other.url}/{doc['id']}/{path}", headers=other.owner.headers)
        assert response.status_code == 404
