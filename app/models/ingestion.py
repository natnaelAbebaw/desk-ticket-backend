import uuid
from datetime import datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.timestamps import utcnow

# Size of the vectors in knowledge_chunks.embedding. pgvector fixes the size per column,
# and every vector in one column must come from a model with this output size. 768 is
# nomic-embed-text's size. Moving to a model with another size needs a migration that
# changes the column, which is deliberate: vectors of different sizes (or different
# models) cannot be compared, so the old ones are useless after such a switch anyway.
EMBEDDING_DIMENSIONS = 768

# The text search configuration: how words are split and reduced to their stem, so
# "refunds", "refunded" and "refunding" all match "refund", and stop words like "the"
# are dropped. One fixed language for now; per-workspace languages would need this
# stored per row. Queries must use the same configuration, so it lives here.
SEARCH_CONFIG = "english"


class KnowledgeChunk(Base):
    """A retrievable piece of an approved knowledge source.

    Chunks are derived data: they can always be rebuilt from knowledge_sources, so
    they are replaced freely.

    A chunk row is written first (by sync) and embedded a moment later (by the worker's
    embed phase), so for a short time, or longer if the embedding model is down, a
    chunk exists without a vector. Search must only use rows whose embedding_model
    matches the current model, which also skips rows with no vector at all.
    """

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        # Every retrieval query filters by workspace first, so workspace_id leads.
        Index("ix_knowledge_chunks_workspace_source", "workspace_id", "source_id"),
        Index("ix_knowledge_chunks_source_position", "source_id", "position"),
        # GIN is an inverted index: for each word stem, the list of rows containing it.
        # It makes `search_vector @@ query` fast without scanning every chunk.
        Index("ix_knowledge_chunks_search_vector", "search_vector", postgresql_using="gin"),
        # HNSW is pgvector's approximate nearest-neighbour index: a layered graph that
        # finds close vectors by hopping between neighbours instead of comparing the
        # query with every row. vector_cosine_ops makes it serve the cosine distance
        # operator (<=>). A query using a different operator (<-> is L2) would
        # silently ignore this index and scan the whole table.
        Index(
            "ix_knowledge_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Copied from the source on purpose. Search can then filter chunks by tenant
    # without a join, and a bug in a join can never leak another tenant's chunks.
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("knowledge_sources.id", ondelete="CASCADE")
    )
    # The source version these chunks were built from. Answers can cite "v3 of X".
    source_version: Mapped[int] = mapped_column(Integer)
    # Order within the source, starting at 0. Not unique: during a re-sync, kept
    # chunks are renumbered in place, and a unique constraint would trip mid-update.
    position: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    # sha256 of heading + content. Unchanged hash after an edit = keep this row.
    content_hash: Mapped[str] = mapped_column(String(64))
    token_estimate: Mapped[int] = mapped_column(Integer)
    # pgvector's vector type. In Python it is a plain list[float].
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    # Which model produced `embedding`. Vectors from two models live in different
    # spaces, so a search must only compare a query vector with chunk vectors from the
    # same model. Storing the name per row is what makes a model switch safe: rows
    # whose name differs from the configured model are re-embedded in the background
    # and ignored by search until then.
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Keyword search document: the chunk's words, stemmed, with positions. A generated
    # column, so Postgres computes it on every insert or update and it can never go
    # out of sync with the text. Heading words get weight A and content words weight
    # B, so a match in the heading ranks higher. deferred=True leaves it out of normal
    # SELECTs: only search reads it, and only inside SQL.
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            f"setweight(to_tsvector('{SEARCH_CONFIG}', heading), 'A') || "
            f"setweight(to_tsvector('{SEARCH_CONFIG}', content), 'B')",
            persisted=True,
        ),
        deferred=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    # Gave up after max attempts. Needs a person to look at last_error.
    failed = "failed"


class IngestionJob(Base):
    """A request to bring one source's chunks in line with the source's current state.

    The job carries no content, only the source id. The worker reads the source when it
    runs, so a job queued for version 2 that runs after an edit to version 3 indexes
    version 3. That makes jobs idempotent: running one twice gives the same chunks.
    """

    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        # At most one queued job per source. Five quick edits leave one job, not five.
        # A running job is not covered: an edit during a run must queue a fresh job,
        # or the run would finish with stale content and nothing would fix it.
        Index(
            "uq_ingestion_jobs_queued_source",
            "source_id",
            unique=True,
            postgresql_where=text("status = 'queued'"),
        ),
        # The worker's claim query scans only unfinished jobs, so the index skips the
        # (much larger) history of finished ones.
        Index(
            "ix_ingestion_jobs_claimable",
            "run_after",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_ingestion_jobs_source_created", "source_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("knowledge_sources.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.queued.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Times come from the database clock (now()), not Python's, so several workers
    # on machines with slightly different clocks still agree on what is due.
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Lease: a running job whose lease has passed is assumed abandoned (the worker
    # crashed or was killed) and can be claimed again.
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    chunk_count: Mapped[int | None] = mapped_column(Integer)
    # Chunks this run sent to the embedding model. Usually far below chunk_count after
    # an edit, because unchanged chunks keep their vectors. A rough cost signal.
    embedded_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
