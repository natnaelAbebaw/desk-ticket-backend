"""Bring one source's chunks in line with the source's current state.

The rule is simple: an approved source has exactly the chunks its current text
produces; any other source (pending, rejected, archived, deleted) has none.

Unchanged chunks are kept, not rewritten. A chunk whose hash already exists keeps its
row (and, from milestone 3, its embedding); only new text is inserted and only text
that disappeared is deleted. Fixing a typo in one paragraph of a long document then
re-embeds one chunk, not all of them.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion.chunking import Chunk, chunk_document, chunk_ticket_answer
from app.models.ingestion import KnowledgeChunk
from app.models.knowledge import KnowledgeKind, KnowledgeSource, KnowledgeStatus
from app.models.timestamps import utcnow


@dataclass(frozen=True)
class SyncResult:
    chunk_count: int
    added: int
    kept: int
    removed: int


def chunk_source(source: KnowledgeSource) -> list[Chunk]:
    if source.kind == KnowledgeKind.ticket_answer.value:
        return chunk_ticket_answer(source.title, source.question or "", source.body)
    return chunk_document(source.title, source.body)


async def sync_source(session: AsyncSession, source_id: uuid.UUID) -> SyncResult:
    """Does not commit; the worker commits together with marking the job succeeded."""
    # FOR UPDATE locks the source row until commit. Two workers syncing the same source
    # at once (a reclaimed job plus a fresh one) then run one after the other instead
    # of interleaving their inserts and deletes.
    source = await session.scalar(
        select(KnowledgeSource).where(KnowledgeSource.id == source_id).with_for_update()
    )
    if source is None:
        # Deleted with its workspace; the foreign key cascade already removed chunks.
        return SyncResult(chunk_count=0, added=0, kept=0, removed=0)

    if source.status != KnowledgeStatus.approved.value:
        removed = await session.execute(
            delete(KnowledgeChunk).where(KnowledgeChunk.source_id == source.id)
        )
        return SyncResult(chunk_count=0, added=0, kept=0, removed=removed.rowcount or 0)

    existing = await session.scalars(
        select(KnowledgeChunk).where(KnowledgeChunk.source_id == source.id)
    )
    # A list per hash, because the same text can legitimately appear twice in one source
    # (a repeated "Contact support" paragraph); each copy is matched once.
    by_hash: dict[str, list[KnowledgeChunk]] = defaultdict(list)
    for row in existing:
        by_hash[row.content_hash].append(row)

    now = utcnow()
    added = kept = 0
    chunks = chunk_source(source)
    for chunk in chunks:
        bucket = by_hash.get(chunk.content_hash)
        if bucket:
            row = bucket.pop()
            row.position = chunk.position
            row.source_version = source.version
            row.updated_at = now
            kept += 1
            continue
        session.add(
            KnowledgeChunk(
                workspace_id=source.workspace_id,
                source_id=source.id,
                source_version=source.version,
                position=chunk.position,
                heading=chunk.heading,
                content=chunk.content,
                content_hash=chunk.content_hash,
                token_estimate=chunk.token_estimate,
                created_at=now,
                updated_at=now,
            )
        )
        added += 1

    # Whatever was not matched is text that no longer exists in the source.
    leftovers = [row for rows in by_hash.values() for row in rows]
    for row in leftovers:
        await session.delete(row)
    await session.flush()
    return SyncResult(chunk_count=len(chunks), added=added, kept=kept, removed=len(leftovers))
