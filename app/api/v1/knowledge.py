"""HTTP routes for the knowledge base.

Routes stay thin: they pick the permission dependency, call the service, and turn the
returned row into a response schema. Review rules live in app/services/knowledge.py.

Every handler gets the workspace from the caller's membership row (`editor.workspace_id`),
never from the request body, so a caller can only act on a workspace they belong to.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_embedder, rate_limit_default, rate_limit_writes, require_role
from app.core.errors import AppError
from app.db.session import get_db
from app.embeddings import Embedder
from app.ingestion import jobs as ingestion_jobs
from app.models.ingestion import KnowledgeChunk
from app.models.knowledge import KnowledgeKind, KnowledgeStatus
from app.models.workspace import WorkspaceMember, WorkspaceRole
from app.schemas.common import Page
from app.schemas.ingestion import ChunkRead, IngestionJobRead
from app.schemas.knowledge import (
    DocumentCreate,
    KnowledgeRead,
    KnowledgeReject,
    KnowledgeSummary,
    KnowledgeUpdate,
    TicketAnswerCreate,
)
from app.schemas.search import SearchHitRead, SearchRequest, SearchResponse
from app.search import service as search_service
from app.services import knowledge as knowledge_service

# Permission aliases. Declaring a parameter with one of these types runs, in order:
# token check (401) -> workspace membership (404 for outsiders) -> role check (403).
# Knowledge is internal, including drafts and rejection notes, so plain members
# (role "member") cannot see it.
KnowledgeEditor = Annotated[
    WorkspaceMember,
    Depends(require_role(WorkspaceRole.owner, WorkspaceRole.admin, WorkspaceRole.agent)),
]
KnowledgeReviewer = Annotated[
    WorkspaceMember,
    Depends(require_role(WorkspaceRole.owner, WorkspaceRole.admin)),
]

# Router-level dependencies run before every route in the router. Write routes add the
# stricter write limit on top.
router = APIRouter(dependencies=[Depends(rate_limit_default)])
# Mounted under /workspaces/{workspace_id}/tickets, next to the ticket routes.
ticket_router = APIRouter(dependencies=[Depends(rate_limit_default)])


@router.post(
    "",
    response_model=KnowledgeRead,
    status_code=201,
    dependencies=[Depends(rate_limit_writes)],
)
async def create_document(
    payload: DocumentCreate,
    editor: KnowledgeEditor,
    session: AsyncSession = Depends(get_db),
) -> KnowledgeRead:
    """Add a Markdown document. Owners and admins publish it approved; agents submit it
    for review."""
    source = await knowledge_service.create_document(
        session, editor.workspace_id, payload, actor=editor
    )
    return KnowledgeRead.model_validate(source)


# Returns KnowledgeSummary, not KnowledgeRead: bodies can be ~200 KB each, so a page of
# 100 full documents would be huge. Clients fetch one source to see its body.
@router.get("", response_model=Page[KnowledgeSummary])
async def list_knowledge(
    editor: KnowledgeEditor,
    # Typing the query params as enums makes FastAPI reject unknown values with 422.
    status: KnowledgeStatus | None = None,
    kind: KnowledgeKind | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> Page[KnowledgeSummary]:
    items, total = await knowledge_service.list_sources(
        session,
        editor.workspace_id,
        status=status.value if status else None,
        kind=kind.value if kind else None,
        limit=limit,
        offset=offset,
    )
    return Page(
        items=[KnowledgeSummary.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


# Declared before the /{source_id} routes. Only GET and PATCH use that bare path, so a
# POST could not be mistaken for one anyway, but keeping fixed paths first avoids
# surprises if a POST /{source_id} is ever added.
@router.post("/search", response_model=SearchResponse)
async def search_knowledge(
    payload: SearchRequest,
    editor: KnowledgeEditor,
    session: AsyncSession = Depends(get_db),
    embedder: Embedder = Depends(get_embedder),
) -> SearchResponse:
    """Find the approved chunks that best match a question, in this workspace only.

    POST rather than GET: the question can be long and may quote a customer, and
    query strings end up in access logs and browser history.
    """
    try:
        result = await search_service.search(
            session,
            embedder,
            workspace_id=editor.workspace_id,
            query=payload.query,
            mode=payload.mode,
            limit=payload.limit,
        )
    except search_service.VectorSearchUnavailable as exc:
        # 503: the request was fine, a dependency is down; retrying later may work.
        raise AppError("embedding_unavailable", str(exc), status_code=503) from exc
    return SearchResponse(
        mode=result.mode,
        hits=[SearchHitRead.model_validate(hit) for hit in result.hits],
        degraded=result.degraded,
    )


@router.get("/{source_id}", response_model=KnowledgeRead)
async def get_knowledge(
    source_id: uuid.UUID,
    editor: KnowledgeEditor,
    session: AsyncSession = Depends(get_db),
) -> KnowledgeRead:
    source = await knowledge_service.get_source(session, editor.workspace_id, source_id)
    return KnowledgeRead.model_validate(source)


@router.patch("/{source_id}", response_model=KnowledgeRead)
async def update_knowledge(
    source_id: uuid.UUID,
    payload: KnowledgeUpdate,
    editor: KnowledgeEditor,
    session: AsyncSession = Depends(get_db),
) -> KnowledgeRead:
    source = await knowledge_service.update_source(
        session, editor.workspace_id, source_id, payload, actor=editor
    )
    return KnowledgeRead.model_validate(source)


@router.get("/{source_id}/chunks", response_model=Page[ChunkRead])
async def list_chunks(
    source_id: uuid.UUID,
    editor: KnowledgeEditor,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> Page[ChunkRead]:
    """The chunks the worker built from this source, in order. Empty until the source is
    approved and the worker has run."""
    # get_source returns 404 for a source in another workspace, before any chunk query.
    source = await knowledge_service.get_source(session, editor.workspace_id, source_id)
    filters = (KnowledgeChunk.source_id == source.id,)
    total = await session.scalar(select(func.count()).select_from(KnowledgeChunk).where(*filters))
    rows = await session.scalars(
        select(KnowledgeChunk)
        .where(*filters)
        .order_by(KnowledgeChunk.position)
        .limit(limit)
        .offset(offset)
    )
    return Page(
        items=[ChunkRead.model_validate(row) for row in rows],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


@router.get("/{source_id}/ingestion", response_model=IngestionJobRead | None)
async def latest_ingestion(
    source_id: uuid.UUID,
    editor: KnowledgeEditor,
    session: AsyncSession = Depends(get_db),
) -> IngestionJobRead | None:
    """The newest ingestion job for this source, or null if it was never queued (for
    example, a draft that is still pending review)."""
    source = await knowledge_service.get_source(session, editor.workspace_id, source_id)
    job = await ingestion_jobs.latest_job(session, source.id)
    return IngestionJobRead.model_validate(job) if job else None


@router.post("/{source_id}/approve", response_model=KnowledgeRead)
async def approve_knowledge(
    source_id: uuid.UUID,
    reviewer: KnowledgeReviewer,
    session: AsyncSession = Depends(get_db),
) -> KnowledgeRead:
    source = await knowledge_service.approve_source(
        session, reviewer.workspace_id, source_id, actor=reviewer
    )
    return KnowledgeRead.model_validate(source)


@router.post("/{source_id}/reject", response_model=KnowledgeRead)
async def reject_knowledge(
    source_id: uuid.UUID,
    payload: KnowledgeReject,
    reviewer: KnowledgeReviewer,
    session: AsyncSession = Depends(get_db),
) -> KnowledgeRead:
    source = await knowledge_service.reject_source(
        session, reviewer.workspace_id, source_id, payload.note, actor=reviewer
    )
    return KnowledgeRead.model_validate(source)


@router.post("/{source_id}/archive", response_model=KnowledgeRead)
async def archive_knowledge(
    source_id: uuid.UUID,
    reviewer: KnowledgeReviewer,
    session: AsyncSession = Depends(get_db),
) -> KnowledgeRead:
    source = await knowledge_service.archive_source(session, reviewer.workspace_id, source_id)
    return KnowledgeRead.model_validate(source)


@ticket_router.post(
    "/{ticket_id}/knowledge",
    response_model=KnowledgeRead,
    status_code=201,
    dependencies=[Depends(rate_limit_writes)],
)
async def propose_ticket_answer(
    ticket_id: uuid.UUID,
    payload: TicketAnswerCreate,
    editor: KnowledgeEditor,
    session: AsyncSession = Depends(get_db),
) -> KnowledgeRead:
    """Turn a resolved ticket into a reusable question and answer. Always needs review."""
    source = await knowledge_service.propose_ticket_answer(
        session, editor.workspace_id, ticket_id, payload, actor=editor
    )
    return KnowledgeRead.model_validate(source)
