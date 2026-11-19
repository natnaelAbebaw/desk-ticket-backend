"""Re-queue every source whose chunks lack a vector from the current model.

    python -m app.embeddings.backfill     (or: make reembed)

Use it:
- after changing EMBEDDING_MODEL, to re-embed everything with the new model;
- after the model was down long enough for jobs to use up their retries and fail.

It only queues jobs; the worker does the embedding at its normal pace. Running it twice
is harmless: enqueue_sync reuses a source's queued job instead of adding another.
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.ingestion.jobs import enqueue_sync
from app.models.ingestion import KnowledgeChunk

logger = logging.getLogger("app.embeddings")


async def enqueue_stale_sources(session: AsyncSession, *, model: str) -> int:
    """Queue a sync job for each source with at least one chunk not embedded by `model`.
    Commits. Returns the number of sources queued."""
    stale = await session.execute(
        select(KnowledgeChunk.workspace_id, KnowledgeChunk.source_id)
        .where(KnowledgeChunk.embedding_model.is_distinct_from(model))
        .distinct()
    )
    count = 0
    for workspace_id, source_id in stale:
        await enqueue_sync(session, workspace_id=workspace_id, source_id=source_id)
        count += 1
    # One commit for all of them. Fine for thousands of sources; a much larger table
    # would want batches so one huge transaction does not hold locks for long.
    await session.commit()
    return count


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    # Imported here for the same reason as in the worker: building the engine on import
    # would get in the way of tests that import this module.
    from app.db.session import SessionLocal, engine

    try:
        async with SessionLocal() as session:
            count = await enqueue_stale_sources(session, model=settings.embedding_model)
        logger.info("queued %d sources for re-embedding with %s", count, settings.embedding_model)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
