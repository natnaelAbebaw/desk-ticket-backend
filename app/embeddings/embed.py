"""The worker's embed phase: give every chunk of a source a vector from the current model.

It runs after sync has committed the source's chunk rows, and works in batches:

    read a batch of chunks that need a vector   (short transaction, then closed)
    send their text to the model                (no transaction open: may take seconds)
    write the vectors back                      (short transaction)

Holding a transaction (and the source row lock) open across a slow network call would
block edits to the source and tie up a database connection for the whole call. Doing
it this way, a failure part way through keeps the batches already written. The retry
then only embeds what is still missing.

"Needs a vector" means embedding_model is not the current model. That covers new
chunks (NULL) and chunks embedded by a previous model, so the same code handles first
ingestion, edits, and model switches.
"""

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import bindparam, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.embeddings.base import Embedder, EmbeddingError
from app.ingestion.chunking import embedding_text
from app.models.ingestion import EMBEDDING_DIMENSIONS, KnowledgeChunk
from app.models.timestamps import utcnow

# Called inside each batch's write transaction, before commit. The worker uses it to
# extend the job's lease, so the lease and the vectors are saved together.
OnBatch = Callable[[AsyncSession], Awaitable[None]]

# A Core UPDATE with placeholders, run once per batch with a list of parameter sets
# (executemany). Core, not the ORM: the ORM's bulk update expects every row to exist,
# and a row deleted meanwhile by a concurrent re-sync is fine to skip here.
_chunks = KnowledgeChunk.__table__
_WRITE_VECTOR = (
    update(_chunks)
    .where(_chunks.c.id == bindparam("chunk_id"))
    .values(
        # Typed bindparam so pgvector's type turns the Python list into a vector value.
        embedding=bindparam("vector", type_=_chunks.c.embedding.type),
        embedding_model=bindparam("model"),
        embedded_at=bindparam("at"),
    )
)


async def embed_source(
    session_factory: async_sessionmaker[AsyncSession],
    source_id: uuid.UUID,
    embedder: Embedder,
    *,
    batch_size: int,
    on_batch: OnBatch | None = None,
) -> int:
    """Embed every chunk of the source that lacks a current vector. Returns how many
    chunks were sent to the model."""
    embedded = 0
    while True:
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(KnowledgeChunk.id, KnowledgeChunk.heading, KnowledgeChunk.content)
                    .where(
                        KnowledgeChunk.source_id == source_id,
                        # IS DISTINCT FROM, not !=: in SQL, NULL != 'x' is NULL (not
                        # true), so a plain != would skip chunks never embedded at all.
                        KnowledgeChunk.embedding_model.is_distinct_from(embedder.model),
                    )
                    .order_by(KnowledgeChunk.position)
                    .limit(batch_size)
                )
            ).all()
        if not rows:
            return embedded

        vectors = await embedder.embed_documents(
            [embedding_text(row.heading, row.content) for row in rows]
        )
        _check(vectors, expected=len(rows), model=embedder.model)

        now = utcnow()
        async with session_factory() as session:
            await session.execute(
                _WRITE_VECTOR,
                [
                    {"chunk_id": row.id, "vector": vector, "model": embedder.model, "at": now}
                    for row, vector in zip(rows, vectors, strict=True)
                ],
            )
            if on_batch is not None:
                await on_batch(session)
            await session.commit()
        # Every row in this batch now has the current model (or was deleted), so the
        # next query moves on to different rows and the loop always ends.
        embedded += len(rows)


def _check(vectors: list[list[float]], *, expected: int, model: str) -> None:
    # Checked here, not only in each embedder, because a wrong vector size would
    # otherwise surface as a confusing database error on the write.
    if len(vectors) != expected:
        raise EmbeddingError(f"{model} returned {len(vectors)} vectors for {expected} texts")
    for vector in vectors:
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise EmbeddingError(
                f"{model} returns {len(vector)}-dimension vectors, but "
                f"knowledge_chunks.embedding holds {EMBEDDING_DIMENSIONS}. Pick a model "
                "with that size, or change the column with a migration."
            )
