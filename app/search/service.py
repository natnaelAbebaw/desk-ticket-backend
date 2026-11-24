"""Find the chunks that best answer a question, within one workspace.

Three modes:
- keyword: Postgres full-text search. Good at exact terms: error codes, plan names,
  product words the customer typed verbatim.
- vector: nearest chunks by embedding. Good at meaning: "get my money back" finds
  "refunds" with no word in common.
- hybrid (the default): both, merged with Reciprocal Rank Fusion (fusion.py). Each
  covers the other's blind spot, which is why production RAG systems usually use it.

Every query applies the same three filters, in SQL, never after the fact:
- the chunk's workspace is the caller's workspace (the tenant boundary);
- the chunk's source is approved right now. Chunks of an archived source stay until
  the worker runs, and must not be served meanwhile;
- for vector search, the chunk was embedded by the current model, because vectors
  from other models (or none) cannot be compared with the query vector.
"""

import logging
import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import Row, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.embeddings.base import Embedder, EmbeddingError
from app.models.ingestion import EMBEDDING_DIMENSIONS, SEARCH_CONFIG, KnowledgeChunk
from app.models.knowledge import KnowledgeSource, KnowledgeStatus
from app.search.fusion import RRF_K, reciprocal_rank_fusion

logger = logging.getLogger("app.search")

# How many results each side of a hybrid search contributes before fusion. Larger than
# the final limit, so a chunk ranked 12th by keywords and 3rd by vectors can still win.
CANDIDATES = 50
# HNSW search width: how many graph candidates Postgres keeps while searching. Higher
# finds the true nearest neighbours more often, at some speed cost. pgvector's
# default is 40, below CANDIDATES, which would cap the result count.
HNSW_EF_SEARCH = 100
# Words used from a question for "any word" keyword matching (see _any_word). A cap
# keeps one long question from building a huge OR query; 1000 characters is far more
# than any real question needs, so the cap rarely matters.
MAX_QUERY_WORDS = 32
_WORD = re.compile(r"[\w-]+")


@dataclass(frozen=True)
class SearchTuning:
    """The knobs of hybrid search, grouped so the retrieval eval (app/evals/) can try
    other values without editing constants. The API always uses DEFAULT_TUNING;
    change the defaults only when the eval shows a clear improvement."""

    # Results each side contributes before fusion (see CANDIDATES).
    candidates: int = CANDIDATES
    # RRF damping constant (see fusion.py). Lower k favours each list's top ranks.
    rrf_k: int = RRF_K
    # Keyword side of fusion: any word (True) or every word, search-box syntax (False).
    # Strict, because the retrieval eval found that any-word matches on common words
    # ("take", "old") push right vector results down, and won nothing back at recall@5.
    # Does not affect the keyword-only fallback, which always uses any word.
    hybrid_any_word: bool = False


DEFAULT_TUNING = SearchTuning()


class SearchMode(StrEnum):
    hybrid = "hybrid"
    keyword = "keyword"
    vector = "vector"


@dataclass(frozen=True)
class Hit:
    chunk_id: uuid.UUID
    source_id: uuid.UUID
    source_title: str
    source_kind: str
    source_version: int
    heading: str
    content: str
    # Meaning depends on the mode: keyword -> text rank (0 to 1), vector -> cosine
    # similarity (1 is identical), hybrid -> RRF score. Compare scores within one
    # response only.
    score: float
    # Position (1 = best) in each underlying search, or None if it did not find this
    # chunk. Shows why a chunk ranked where it did; the eval in milestone 5 uses them.
    keyword_rank: int | None
    vector_rank: int | None


@dataclass(frozen=True)
class SearchResult:
    mode: SearchMode
    hits: list[Hit]
    # True when hybrid search fell back to keywords only because the embedding model
    # was unavailable. Results are still useful, just weaker; callers may say so.
    degraded: bool = False


class VectorSearchUnavailable(Exception):
    """Vector-only search was asked for, but the embedding model is unavailable."""


async def search(
    session: AsyncSession,
    embedder: Embedder,
    *,
    workspace_id: uuid.UUID,
    query: str,
    mode: SearchMode = SearchMode.hybrid,
    limit: int = 5,
    tuning: SearchTuning = DEFAULT_TUNING,
) -> SearchResult:
    if mode is SearchMode.keyword:
        rows = await _keyword(session, workspace_id, query, limit, any_word=False)
        return SearchResult(mode, [_hit(row, keyword_rank=i) for i, row in _ranked(rows)])

    try:
        vector = await _embed_query(embedder, query)
    except EmbeddingError as exc:
        if mode is SearchMode.vector:
            raise VectorSearchUnavailable(str(exc)) from exc
        # Hybrid degrades instead of failing: keyword results beat an error page.
        logger.warning("vector search unavailable, using keywords only: %s", exc)
        # Always any word here. With no vectors to carry a full-sentence question, strict
        # matching finds almost nothing (recall@5 about 0.2 against 0.88 in the eval).
        rows = await _keyword(session, workspace_id, query, limit, any_word=True)
        hits = [_hit(row, keyword_rank=i) for i, row in _ranked(rows)]
        return SearchResult(mode, hits, degraded=True)

    if mode is SearchMode.vector:
        rows = await _vector(session, workspace_id, vector, embedder.model, limit)
        return SearchResult(mode, [_hit(row, vector_rank=i) for i, row in _ranked(rows)])

    vector_rows = await _vector(session, workspace_id, vector, embedder.model, tuning.candidates)
    # No vectors at all (for example after a model change, before `make reembed`) leaves
    # keywords on their own, like the fallback above, so match any word there too.
    any_word = tuning.hybrid_any_word or not vector_rows
    keyword_rows = await _keyword(
        session, workspace_id, query, tuning.candidates, any_word=any_word
    )
    by_id = {row.chunk_id: row for row in [*keyword_rows, *vector_rows]}
    keyword_ranks = {row.chunk_id: i for i, row in _ranked(keyword_rows)}
    vector_ranks = {row.chunk_id: i for i, row in _ranked(vector_rows)}
    fused = reciprocal_rank_fusion(
        [[row.chunk_id for row in keyword_rows], [row.chunk_id for row in vector_rows]],
        k=tuning.rrf_k,
    )
    hits = [
        _hit(
            by_id[chunk_id],
            score=score,
            keyword_rank=keyword_ranks.get(chunk_id),
            vector_rank=vector_ranks.get(chunk_id),
        )
        for chunk_id, score in fused[:limit]
    ]
    return SearchResult(mode, hits)


def _retrievable(workspace_id: uuid.UUID, score: object) -> Select:
    """The columns every search returns, with the tenant and approval filters."""
    return (
        select(
            KnowledgeChunk.id.label("chunk_id"),
            KnowledgeChunk.source_id,
            KnowledgeSource.title.label("source_title"),
            KnowledgeSource.kind.label("source_kind"),
            KnowledgeChunk.source_version,
            KnowledgeChunk.heading,
            KnowledgeChunk.content,
            score,
        )
        .join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
        .where(
            KnowledgeChunk.workspace_id == workspace_id,
            # Redundant with the line above, on purpose: if a bug ever wrote a chunk
            # with the wrong workspace_id, the source's own workspace still guards it.
            KnowledgeSource.workspace_id == workspace_id,
            KnowledgeSource.status == KnowledgeStatus.approved.value,
        )
    )


async def _keyword(
    session: AsyncSession, workspace_id: uuid.UUID, query: str, limit: int, *, any_word: bool
) -> list[Row[Any]]:
    """Full-text search, in one of two matching styles.

    any_word=False (keyword mode, and hybrid's keyword side): websearch_to_tsquery, the
    search-box syntax. Every word must match, and "quoted phrases", `or`, and -excluded
    words work. Precise, for someone typing an error code or a few exact terms. In hybrid
    it adds evidence only when it is strong, and stays silent on most full sentences,
    leaving those to vectors.

    any_word=True (hybrid when there are no vectors to use): a chunk matches if it has
    any of the words. Questions are sentences: "refund to a new card" has three content
    words, and requiring all three finds nothing whenever one ("new") is missing. The
    ranking below puts chunks with more of the words, closer together, first. But
    ts_rank_cd does not know that some words are rarer than others, so a common word
    counts as much as a telling one; that noise is why fusion does not use it.
    """
    tsquery = _any_word(query) if any_word else func.websearch_to_tsquery(SEARCH_CONFIG, query)
    # ts_rank_cd rewards matches that are frequent, close together, and in the
    # heading (weight A). Normalization 32 maps the rank into 0..1 as rank/(rank+1).
    rank = func.ts_rank_cd(KnowledgeChunk.search_vector, tsquery, 32).label("score")
    statement = (
        _retrievable(workspace_id, rank)
        # @@ is "matches". It is what lets Postgres use the GIN index.
        .where(KnowledgeChunk.search_vector.op("@@")(tsquery))
        # The id tiebreak keeps the order stable between identical scores.
        .order_by(rank.desc(), KnowledgeChunk.id)
        .limit(limit)
    )
    return list((await session.execute(statement)).all())


def _any_word(query: str) -> Any:
    """A tsquery matching any word of the query: w1 | w2 | ...

    Built as plainto_tsquery(word1) || plainto_tsquery(word2) || ..., where || on two
    tsqueries means OR. Each word goes to Postgres as a bound parameter, so no input can
    break the query syntax, and plainto_tsquery stems each word with the same 'english'
    config as the indexed column ("refunded" -> refund) and drops stop words ("to",
    "a"). A word that is only a stop word becomes an empty query, which || ignores.
    """
    words = list(dict.fromkeys(word.lower() for word in _WORD.findall(query)))
    words = words[:MAX_QUERY_WORDS] or [query]
    tsquery = func.plainto_tsquery(SEARCH_CONFIG, words[0])
    for word in words[1:]:
        tsquery = tsquery.op("||")(func.plainto_tsquery(SEARCH_CONFIG, word))
    return tsquery


async def _vector(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    vector: list[float],
    model: str,
    limit: int,
) -> list[Row[Any]]:
    # set_config(..., true) is SET LOCAL: it lasts only until this transaction ends,
    # so it never leaks to other requests sharing the pooled connection.
    #
    # iterative_scan matters for multi-tenant search. HNSW first finds the nearest
    # vectors across all tenants, and only then is WHERE workspace_id = ... applied.
    # Without iterative scans, a small workspace in a big table could get back zero
    # rows: all ef_search candidates belonged to other tenants. With it, pgvector
    # (0.8+) keeps searching until enough rows pass the filters. strict_order keeps
    # results exactly sorted by distance.
    await session.execute(select(func.set_config("hnsw.iterative_scan", "strict_order", True)))
    await session.execute(select(func.set_config("hnsw.ef_search", str(HNSW_EF_SEARCH), True)))

    # <=> is cosine distance (0 = same direction, 2 = opposite), the operator the HNSW
    # index was built for. Similarity is 1 - distance, so higher is better.
    distance = KnowledgeChunk.embedding.cosine_distance(vector)
    statement = (
        _retrievable(workspace_id, (1 - distance).label("score"))
        .where(KnowledgeChunk.embedding_model == model)
        # ORDER BY distance LIMIT n is the shape that lets Postgres use the HNSW index.
        .order_by(distance, KnowledgeChunk.id)
        .limit(limit)
    )
    return list((await session.execute(statement)).all())


async def _embed_query(embedder: Embedder, query: str) -> list[float]:
    vector = await embedder.embed_query(query)
    if len(vector) != EMBEDDING_DIMENSIONS:
        raise EmbeddingError(
            f"{embedder.model} returned a {len(vector)}-dimension query vector; "
            f"chunks hold {EMBEDDING_DIMENSIONS}"
        )
    return vector


def _ranked(rows: list[Row[Any]]) -> list[tuple[int, Row[Any]]]:
    return [(index + 1, row) for index, row in enumerate(rows)]


def _hit(
    row: Row[Any],
    *,
    score: float | None = None,
    keyword_rank: int | None = None,
    vector_rank: int | None = None,
) -> Hit:
    return Hit(
        chunk_id=row.chunk_id,
        source_id=row.source_id,
        source_title=row.source_title,
        source_kind=row.source_kind,
        source_version=row.source_version,
        heading=row.heading,
        content=row.content,
        score=float(score if score is not None else row.score),
        keyword_rank=keyword_rank,
        vector_rank=vector_rank,
    )
