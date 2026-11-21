"""Search: each mode finds what it is good at, hybrid merges them, and results never
cross the tenant, approval, or embedding-model boundaries.

Documents are chunked and embedded by the real worker with the FakeEmbedder, and the
API searches with the same fake (conftest overrides get_embedder), so query and chunk
vectors come from one model, as they do in production.
"""

import uuid
from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.deps import get_embedder
from app.core.config import Settings
from app.embeddings import EmbeddingError, FakeEmbedder
from app.ingestion import worker
from app.main import app
from app.models.ingestion import KnowledgeChunk
from app.search.fusion import reciprocal_rank_fusion

from helpers import Account, add_member, create_workspace, signup

BILLING = """# Refunds
Customers can get a full refund within 14 days of their first payment.

# Failed payments
When a card payment fails the customer sees error code E4021,
and we retry the charge after 1, 3 and 7 days.

# Plans
Team and Business plans are billed per seat, monthly or yearly."""

ACCOUNTS = """# Password reset
Use the forgot password link on the sign in page to choose a new password.

# Two-factor authentication
An owner can remove and re-invite a user who lost their two-factor device."""


@dataclass
class Workspace:
    owner: Account
    url: str
    billing_id: str
    accounts_id: str

    async def search(self, client: AsyncClient, query: str, **options: object) -> dict:
        response = await client.post(
            f"{self.url}/search", json={"query": query, **options}, headers=self.owner.headers
        )
        assert response.status_code == 200, response.text
        return response.json()


async def _workspace(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
    slug: str = "acme",
) -> Workspace:
    owner = await signup(client, f"owner@{slug}.test", "Owner")
    workspace_id = await create_workspace(client, owner, slug=slug)
    url = f"/api/v1/workspaces/{workspace_id}/knowledge"
    ids = []
    for title, body in (("Billing guide", BILLING), ("Account guide", ACCOUNTS)):
        response = await client.post(
            url, json={"title": title, "body": body}, headers=owner.headers
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    # Run the worker until the queue is empty: chunk and embed both documents.
    while await worker.run_once(factory, settings, embedder):
        pass
    return Workspace(owner, url, *ids)


def _headings(result: dict) -> list[str]:
    return [hit["heading"] for hit in result["hits"]]


# Keyword search


async def test_keyword_search_finds_exact_terms(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    ws = await _workspace(client, factory, settings, embedder)
    result = await ws.search(client, "E4021", mode="keyword")
    assert _headings(result) == ["Billing guide > Failed payments"]
    [hit] = result["hits"]
    assert (hit["keyword_rank"], hit["vector_rank"]) == (1, None)
    assert hit["source_title"] == "Billing guide"
    assert hit["source_kind"] == "document"
    assert 0 < hit["score"] <= 1


async def test_keyword_search_matches_word_forms(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    ws = await _workspace(client, factory, settings, embedder)
    # "refunded" and "refund" share the stem "refund"; "the" is a stop word.
    result = await ws.search(client, "the refunded payment", mode="keyword")
    assert _headings(result)[0] == "Billing guide > Refunds"
    # Every word must match (AND), so a query with an unknown word finds nothing.
    assert (await ws.search(client, "refund zebra", mode="keyword"))["hits"] == []


async def test_hybrid_keyword_side_matches_any_word(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    """A question rarely has every word in one chunk. Keyword mode stays strict; hybrid
    still gets keyword evidence from the words that do match."""
    ws = await _workspace(client, factory, settings, embedder)
    question = "how do I get a refund for my zebra"
    assert (await ws.search(client, question, mode="keyword"))["hits"] == []

    result = await ws.search(client, question, mode="hybrid")
    by_heading = {hit["heading"]: hit for hit in result["hits"]}
    assert by_heading["Billing guide > Refunds"]["keyword_rank"] == 1


async def test_any_word_ranks_chunks_with_more_matching_words_first(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    ws = await _workspace(client, factory, settings, embedder)
    # "payment" is in Refunds and Failed payments; "card", "retry", and "charge" only
    # in Failed payments, so it has the most matches and ranks first.
    result = await ws.search(client, "card payment retry charge", mode="hybrid", limit=10)
    keyword_order = sorted(
        (hit for hit in result["hits"] if hit["keyword_rank"]), key=lambda h: h["keyword_rank"]
    )
    assert keyword_order[0]["heading"] == "Billing guide > Failed payments"
    assert "Billing guide > Refunds" in [hit["heading"] for hit in keyword_order]


async def test_any_word_query_is_safe_with_odd_input(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    # Characters that are tsquery syntax (& | ! : ' ( )) must not cause a 500, and a
    # query of only stop words or symbols just finds nothing on the keyword side.
    ws = await _workspace(client, factory, settings, embedder)
    for query in ["refund & | ! :* ' ( )", "the a of", "!!!", "E4021-"]:
        result = await ws.search(client, query, mode="hybrid")
        assert result["degraded"] is False


# Vector search


async def test_vector_search_ranks_by_closeness(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    ws = await _workspace(client, factory, settings, embedder)
    result = await ws.search(client, "lost my two-factor device", mode="vector", limit=3)
    assert _headings(result)[0] == "Account guide > Two-factor authentication"
    # Vector search always returns the nearest chunks, even weak matches, so it fills
    # the limit. Deciding what is too weak to use is answer generation's job.
    assert len(result["hits"]) == 3
    scores = [hit["score"] for hit in result["hits"]]
    assert scores == sorted(scores, reverse=True)
    assert all(-1 <= score <= 1 for score in scores)
    assert [hit["vector_rank"] for hit in result["hits"]] == [1, 2, 3]


# Hybrid search


async def test_hybrid_merges_both_rankings(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    ws = await _workspace(client, factory, settings, embedder)
    result = await ws.search(client, "refund payment", limit=4)
    assert result["mode"] == "hybrid"
    assert result["degraded"] is False
    top = result["hits"][0]
    # Found by both searches, so it outranks chunks only one of them found.
    assert top["heading"] == "Billing guide > Refunds"
    assert top["keyword_rank"] is not None and top["vector_rank"] is not None
    scores = [hit["score"] for hit in result["hits"]]
    assert scores == sorted(scores, reverse=True)
    assert len(result["hits"]) == 4
    # Chunks without the keywords still appear, through vector search alone.
    assert any(hit["keyword_rank"] is None for hit in result["hits"])


def test_rrf_prefers_items_found_by_both_lists() -> None:
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["c", "d"]])
    keys = [key for key, _ in fused]
    # c is 3rd and 1st: 1/63 + 1/61 beats a's single 1/61.
    assert keys[0] == "c"
    assert keys[1:] == ["a", "b", "d"]  # a and d tie on rank; the first list wins ties
    assert fused[0][1] == pytest.approx(1 / 63 + 1 / 61)


def test_rrf_handles_empty_lists() -> None:
    assert reciprocal_rank_fusion([[], []]) == []
    assert [key for key, _ in reciprocal_rank_fusion([[], ["x"]])] == ["x"]


# Boundaries


async def test_search_never_returns_another_workspaces_chunks(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    ours = await _workspace(client, factory, settings, embedder)
    # Another tenant with exactly the same documents: identical text and vectors.
    theirs = await _workspace(client, factory, settings, embedder, slug="globex")
    for mode in ("keyword", "vector", "hybrid"):
        result = await ours.search(client, "refund payment", mode=mode, limit=20)
        assert result["hits"]
        assert {hit["source_id"] for hit in result["hits"]} <= {
            ours.billing_id,
            ours.accounts_id,
        }, mode
    # And the other way round: their search only sees their sources.
    result = await client.post(
        f"{theirs.url}/search", json={"query": "refund", "limit": 20}, headers=theirs.owner.headers
    )
    assert {hit["source_id"] for hit in result.json()["hits"]} <= {
        theirs.billing_id,
        theirs.accounts_id,
    }


async def test_archived_source_disappears_before_the_worker_runs(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    ws = await _workspace(client, factory, settings, embedder)
    archived = await client.post(f"{ws.url}/{ws.billing_id}/archive", headers=ws.owner.headers)
    assert archived.status_code == 200
    # The chunks are still in the table: the worker has not removed them yet.
    async with factory() as session:
        remaining = await session.scalar(
            text("SELECT count(*) FROM knowledge_chunks WHERE source_id = :id"),
            {"id": uuid.UUID(ws.billing_id)},
        )
    assert remaining == 3
    # Search filters on the source's status, so they are already gone from results.
    for mode in ("keyword", "vector", "hybrid"):
        result = await ws.search(client, "refund payment", mode=mode, limit=20)
        assert ws.billing_id not in {hit["source_id"] for hit in result["hits"]}, mode


async def test_vectors_from_another_model_are_not_compared(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    ws = await _workspace(client, factory, settings, embedder)
    async with factory() as session:
        # As if these vectors came from the previous model and await `make reembed`.
        await session.execute(update(KnowledgeChunk).values(embedding_model="old-model"))
        await session.commit()
    assert (await ws.search(client, "refund", mode="vector"))["hits"] == []
    # Keyword search does not depend on vectors, so it still finds them, and hybrid
    # still returns the keyword side.
    assert (await ws.search(client, "refund", mode="keyword"))["hits"]
    assert (await ws.search(client, "refund"))["hits"]


# Model outage


class BrokenEmbedder(FakeEmbedder):
    async def embed_query(self, text: str) -> list[float]:
        raise EmbeddingError("Cannot reach Ollama")


async def test_hybrid_falls_back_to_keywords_when_the_model_is_down(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    ws = await _workspace(client, factory, settings, embedder)
    app.dependency_overrides[get_embedder] = lambda: BrokenEmbedder()

    result = await ws.search(client, "refund")
    assert result["degraded"] is True
    assert _headings(result) == ["Billing guide > Refunds"]
    assert result["hits"][0]["vector_rank"] is None

    response = await client.post(
        f"{ws.url}/search", json={"query": "refund", "mode": "vector"}, headers=ws.owner.headers
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "embedding_unavailable"


# Access and validation


async def test_search_requires_an_internal_role(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    ws = await _workspace(client, factory, settings, embedder)
    workspace_id = ws.url.split("/")[4]
    member = await signup(client, "member@acme.test", "Member")
    await add_member(client, workspace_id, ws.owner, member, role="member")
    outsider = await signup(client, "outsider@else.test", "Outsider")

    body = {"query": "refund"}
    response = await client.post(f"{ws.url}/search", json=body, headers=member.headers)
    assert response.status_code == 403
    response = await client.post(f"{ws.url}/search", json=body, headers=outsider.headers)
    assert response.status_code == 404
    response = await client.post(f"{ws.url}/search", json=body)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"query": ""},
        {"query": "x" * 1001},
        {"query": "refund", "limit": 21},
        {"query": "a", "mode": "fuzzy"},
    ],
)
async def test_search_validates_input(
    client: AsyncClient,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
    body: dict,
) -> None:
    ws = await _workspace(client, factory, settings, embedder)
    response = await client.post(f"{ws.url}/search", json=body, headers=ws.owner.headers)
    assert response.status_code == 422


# Indexes


async def test_queries_can_use_the_search_indexes(engine: AsyncEngine) -> None:
    """The operator in a query must match the index's operator class, or Postgres
    silently scans the whole table. Turning sequential scans off makes the planner
    prove it can use each index for the query shapes search.py runs."""
    vector = "[" + ",".join(["0.1"] * 768) + "]"
    async with engine.connect() as connection:
        await connection.execute(text("SET LOCAL enable_seqscan = off"))
        # EXPLAIN returns one row per line of the plan.
        rows = await connection.execute(
            text(
                "EXPLAIN SELECT id FROM knowledge_chunks "
                "ORDER BY embedding <=> CAST(:v AS vector) LIMIT 5"
            ),
            {"v": vector},
        )
        assert "ix_knowledge_chunks_embedding_hnsw" in "\n".join(row[0] for row in rows)
        rows = await connection.execute(
            text(
                "EXPLAIN SELECT id FROM knowledge_chunks "
                "WHERE search_vector @@ websearch_to_tsquery('english', 'refund')"
            )
        )
        assert "ix_knowledge_chunks_search_vector" in "\n".join(row[0] for row in rows)
