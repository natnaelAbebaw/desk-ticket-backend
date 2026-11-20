import uuid

from pydantic import BaseModel, Field

from app.models.knowledge import KnowledgeKind
from app.search.service import SearchMode

# A question, not a document. Long queries embed poorly and cost more.
QUERY_MAX = 1_000


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=QUERY_MAX)
    mode: SearchMode = SearchMode.hybrid
    # Small on purpose: these chunks end up in a model prompt (milestone 6), where every
    # extra chunk costs tokens and can distract the model from the right one.
    limit: int = Field(default=5, ge=1, le=20)


class SearchHitRead(BaseModel):
    chunk_id: uuid.UUID
    source_id: uuid.UUID
    source_title: str
    source_kind: KnowledgeKind
    source_version: int
    heading: str
    content: str
    score: float = Field(description="keyword: text rank, vector: cosine similarity, hybrid: RRF")
    keyword_rank: int | None = Field(description="1-based rank in keyword search, if found")
    vector_rank: int | None = Field(description="1-based rank in vector search, if found")

    model_config = {"from_attributes": True}


class SearchResponse(BaseModel):
    mode: SearchMode
    hits: list[SearchHitRead]
    degraded: bool = Field(
        description="True when hybrid search used keywords only because the "
        "embedding model was unavailable"
    )
