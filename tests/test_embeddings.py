"""Embeddings: every chunk of an approved source gets a vector from the current model,
unchanged chunks are not re-embedded, model outages retry without losing progress, and
switching models re-embeds everything.

The worker runs with FakeEmbedder, so no test needs Ollama. The Ollama client is tested
on its own against httpx.MockTransport, which plays the part of the Ollama server.
"""

import json
import math
import uuid
from datetime import datetime

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.embeddings import EmbeddingError, FakeEmbedder, OllamaEmbedder
from app.embeddings.backfill import enqueue_stale_sources
from app.ingestion import worker
from app.models.ingestion import EMBEDDING_DIMENSIONS, IngestionJob, KnowledgeChunk

from helpers import Account, create_workspace, signup

SECTIONS = "# Setup\nInstall the app.\n\n# Billing\nPay monthly.\n\n# Support\nEmail us."


async def _publish(client: AsyncClient, body: str = SECTIONS) -> tuple[Account, str, str]:
    """An owner, their workspace's knowledge URL, and one approved document's id."""
    owner = await signup(client, "owner@acme.test", "Owner")
    workspace_id = await create_workspace(client, owner, slug="acme")
    url = f"/api/v1/workspaces/{workspace_id}/knowledge"
    response = await client.post(url, json={"title": "Guide", "body": body}, headers=owner.headers)
    assert response.status_code == 201, response.text
    return owner, url, response.json()["id"]


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


async def _job(factory: async_sessionmaker[AsyncSession], source_id: str) -> IngestionJob:
    async with factory() as session:
        job = await session.scalar(
            select(IngestionJob)
            .where(IngestionJob.source_id == uuid.UUID(source_id))
            .order_by(IngestionJob.created_at.desc())
            .limit(1)
        )
    assert job is not None
    return job


async def _make_due(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        await session.execute(
            update(IngestionJob).where(IngestionJob.status == "queued").values(run_after=func.now())
        )
        await session.commit()


class FlakyEmbedder(FakeEmbedder):
    """Fails on the listed call numbers (1-based), like a model server that drops out."""

    def __init__(self, fail_on: set[int], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.fail_on = fail_on
        self.attempted = 0

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.attempted += 1
        if self.attempted in self.fail_on:
            raise EmbeddingError("model server unavailable")
        return await super().embed_documents(texts)


# The worker


async def test_worker_embeds_every_chunk(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    owner, url, source_id = await _publish(client)
    await worker.run_once(factory, settings, embedder)

    chunks = await _chunks(factory, source_id)
    assert len(chunks) == 3
    for chunk in chunks:
        assert chunk.embedding_model == embedder.model
        assert chunk.embedded_at is not None
        assert chunk.embedding is not None
        assert len(chunk.embedding) == EMBEDDING_DIMENSIONS
        # The stored vector is the one the model returned for heading + content.
        expected = embedder.vector(f"{chunk.heading}\n\n{chunk.content}")
        assert chunk.embedding == pytest.approx(expected, abs=1e-6)

    job = await _job(factory, source_id)
    assert (job.status, job.chunk_count, job.embedded_count) == ("succeeded", 3, 3)

    page = (await client.get(f"{url}/{source_id}/chunks", headers=owner.headers)).json()
    assert {item["embedding_model"] for item in page["items"]} == {embedder.model}
    assert "embedding" not in page["items"][0]  # the raw vector is not exposed
    ingestion = (await client.get(f"{url}/{source_id}/ingestion", headers=owner.headers)).json()
    assert ingestion["embedded_count"] == 3


async def test_edit_reembeds_only_the_changed_chunk(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    owner, url, source_id = await _publish(client)
    await worker.run_once(factory, settings, embedder)
    embedder.calls.clear()

    edited = SECTIONS.replace("Pay monthly.", "Pay yearly.")
    response = await client.patch(
        f"{url}/{source_id}", json={"body": edited}, headers=owner.headers
    )
    assert response.status_code == 200
    await worker.run_once(factory, settings, embedder)

    # Only the edited section went to the model; the other two kept their vectors.
    assert embedder.embedded_texts == ["Guide > Billing\n\nPay yearly."]
    job = await _job(factory, source_id)
    assert (job.chunk_count, job.embedded_count) == (3, 1)
    assert all(chunk.embedding_model for chunk in await _chunks(factory, source_id))


async def test_archived_source_sends_nothing_to_the_model(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    owner, url, source_id = await _publish(client)
    await worker.run_once(factory, settings, embedder)
    embedder.calls.clear()

    await client.post(f"{url}/{source_id}/archive", headers=owner.headers)
    await worker.run_once(factory, settings, embedder)
    assert embedder.calls == []
    assert await _chunks(factory, source_id) == []


# Failures


async def test_model_outage_keeps_finished_batches_and_retries_the_rest(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    _, _, source_id = await _publish(client)
    one_per_batch = settings.model_copy(update={"embedding_batch_size": 1})
    flaky = FlakyEmbedder(fail_on={2})

    # Batch 1 succeeds, batch 2 fails: the job goes back to the queue.
    await worker.run_once(factory, one_per_batch, flaky)
    job = await _job(factory, source_id)
    assert job.status == "queued"
    assert "model server unavailable" in (job.last_error or "")
    # The chunk rows exist (sync committed) and batch 1's vector was kept.
    chunks = await _chunks(factory, source_id)
    assert [c.embedding_model is not None for c in chunks] == [True, False, False]

    # The retry embeds only the two chunks still missing a vector.
    await _make_due(factory)
    flaky.calls.clear()
    await worker.run_once(factory, one_per_batch, flaky)
    assert len(flaky.embedded_texts) == 2
    job = await _job(factory, source_id)
    assert (job.status, job.attempts, job.embedded_count) == ("succeeded", 2, 2)
    assert all(c.embedding_model for c in await _chunks(factory, source_id))


async def test_wrong_vector_size_fails_with_a_clear_message(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    _, _, source_id = await _publish(client)
    await worker.run_once(factory, settings, FakeEmbedder(dimensions=384))

    job = await _job(factory, source_id)
    assert job.status == "queued"  # retried like any failure, then fails for good
    assert "384-dimension vectors" in (job.last_error or "")
    assert all(c.embedding is None for c in await _chunks(factory, source_id))


async def test_lease_is_extended_after_each_batch(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    _, _, source_id = await _publish(client)
    leases: list[datetime] = []

    class LeaseWatcher(FakeEmbedder):
        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            leases.append((await _job(factory, source_id)).locked_until)  # type: ignore[arg-type]
            return await super().embed_documents(texts)

    await worker.run_once(
        factory, settings.model_copy(update={"embedding_batch_size": 1}), LeaseWatcher()
    )
    # Batch 1 sees the lease from the claim; batches 2 and 3 see it pushed forward.
    assert len(leases) == 3
    assert leases[0] < leases[1] < leases[2]


# Switching models


async def test_switching_models_reembeds_everything_via_backfill(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    _, _, source_id = await _publish(client)
    await worker.run_once(factory, settings, embedder)

    new_model = FakeEmbedder(model="fake-embedder-v2")
    async with factory() as session:
        assert await enqueue_stale_sources(session, model=new_model.model) == 1
        # Queuing twice reuses the queued job instead of adding a second one.
        assert await enqueue_stale_sources(session, model=new_model.model) == 1
    await worker.run_once(factory, settings, new_model)

    assert len(new_model.embedded_texts) == 3
    assert {c.embedding_model for c in await _chunks(factory, source_id)} == {new_model.model}
    async with factory() as session:
        assert await enqueue_stale_sources(session, model=new_model.model) == 0


# The fake embedder


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))  # both have length 1


def test_fake_embedder_is_deterministic_and_rewards_shared_words() -> None:
    fake = FakeEmbedder()
    refund = fake.vector("How do I get a refund for my invoice?")
    assert refund == fake.vector("How do I get a refund for my invoice?")
    assert math.isclose(math.sqrt(sum(v * v for v in refund)), 1.0)
    close = _cosine(refund, fake.vector("Refund an invoice from the admin panel"))
    far = _cosine(refund, fake.vector("Read-only mode starts after failed card payments"))
    assert close > far


# The Ollama client, against a fake Ollama server


def _ollama(handler: httpx.MockTransport) -> OllamaEmbedder:
    return OllamaEmbedder(
        base_url="http://ollama.test",
        model="nomic-embed-text",
        timeout_seconds=5,
        document_prefix="search_document: ",
        query_prefix="search_query: ",
        transport=handler,
    )


async def test_ollama_sends_one_request_per_batch_with_task_prefixes() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/embed"
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={"embeddings": [[0.5] * 3 for _ in body["input"]]})

    ollama = _ollama(httpx.MockTransport(handler))
    vectors = await ollama.embed_documents(["first chunk", "second chunk"])
    query = await ollama.embed_query("refund?")
    await ollama.aclose()

    assert vectors == [[0.5] * 3, [0.5] * 3]
    assert query == [0.5] * 3
    assert requests[0]["model"] == "nomic-embed-text"
    assert requests[0]["input"] == ["search_document: first chunk", "search_document: second chunk"]
    assert requests[1]["input"] == ["search_query: refund?"]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(404, json={"error": "model not found"}), "ollama pull nomic-embed-text"),
        (httpx.Response(500, text="out of memory"), "500: out of memory"),
        (httpx.Response(200, json={"embeddings": [[1.0]]}), "1 vectors for 2 inputs"),
        (httpx.Response(200, json={"oops": True}), "no 'embeddings' field"),
    ],
)
async def test_ollama_bad_replies_become_embedding_errors(
    response: httpx.Response, message: str
) -> None:
    ollama = _ollama(httpx.MockTransport(lambda request: response))
    with pytest.raises(EmbeddingError, match=message):
        await ollama.embed_documents(["a", "b"])
    await ollama.aclose()


async def test_ollama_down_says_how_to_start_it() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    ollama = _ollama(httpx.MockTransport(refuse))
    with pytest.raises(EmbeddingError, match="make ollama"):
        await ollama.embed_documents(["a"])
    await ollama.aclose()
