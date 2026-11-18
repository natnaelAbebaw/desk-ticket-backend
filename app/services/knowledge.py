"""Knowledge sources and their review flow.

Status changes:

    created by owner/admin (document)  -> approved
    created by agent, or ticket answer -> pending_review
    pending_review --approve--> approved
    pending_review --reject---> rejected
    rejected --edit-----------> pending_review
    any (except archived) --archive--> archived

Only approved sources are chunked and retrieved. Any change that can alter what is
retrievable (a source becoming approved, approved text being edited, or an approved
source being archived) queues an ingestion job in the same transaction. See
app/ingestion/jobs.py for why that matters.
"""

import hashlib
import json
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.ingestion.jobs import enqueue_sync
from app.models.knowledge import KnowledgeKind, KnowledgeSource, KnowledgeStatus
from app.models.ticket import TicketStatus
from app.models.timestamps import utcnow
from app.models.workspace import WorkspaceMember, WorkspaceRole
from app.schemas.knowledge import DocumentCreate, KnowledgeUpdate, TicketAnswerCreate
from app.services import tickets as ticket_service

# Statuses are stored as plain strings (String(32) columns), so comparisons use `.value`.
REVIEWER_ROLES = {WorkspaceRole.owner.value, WorkspaceRole.admin.value}
LIVE_STATUSES = (KnowledgeStatus.pending_review.value, KnowledgeStatus.approved.value)
_RESOLVED_TICKET_STATUSES = {TicketStatus.resolved.value, TicketStatus.closed.value}

# Names of the partial unique indexes in migration 0004. Matched against
# IntegrityError so a lost race gets the same 409 as the normal check.
UNIQUE_CONTENT_INDEX = "uq_knowledge_sources_live_content"
UNIQUE_TICKET_INDEX = "uq_knowledge_sources_live_ticket"


def is_reviewer(member: WorkspaceMember) -> bool:
    return member.role in REVIEWER_ROLES


def content_hash(kind: str, question: str | None, body: str) -> str:
    """Hash of the content with whitespace collapsed. The title is left out on purpose:
    the same text under a new title is still a duplicate."""

    def normalize(text: str | None) -> str:
        # str.split() with no argument splits on any run of spaces, tabs, or newlines.
        return " ".join((text or "").split())

    # json.dumps of a list keeps the fields apart, so ("ab", "c") and ("a", "bc")
    # cannot produce the same string the way plain concatenation could.
    payload = json.dumps([kind, normalize(question), normalize(body)])
    return hashlib.sha256(payload.encode()).hexdigest()


def _duplicate(existing_id: uuid.UUID | None = None) -> AppError:
    suffix = f" ({existing_id})" if existing_id else ""
    return AppError(
        "duplicate_knowledge",
        f"The same content is already pending or approved in this workspace{suffix}",
        status_code=409,
    )


async def _ensure_unique_content(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    digest: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> None:
    query = select(KnowledgeSource.id).where(
        KnowledgeSource.workspace_id == workspace_id,
        KnowledgeSource.content_hash == digest,
        KnowledgeSource.status.in_(LIVE_STATUSES),
    )
    if exclude_id is not None:
        query = query.where(KnowledgeSource.id != exclude_id)
    # This check gives a friendly error with the existing id. It is not enough on its own:
    # two requests can both pass it before either commits. The partial unique index
    # catches that case in _commit.
    existing = await session.scalar(query.limit(1))
    if existing is not None:
        raise _duplicate(existing)


async def _commit(
    session: AsyncSession, source: KnowledgeSource, *, resync: bool = False
) -> KnowledgeSource:
    """Commit the source change. With resync=True, also queue an ingestion job in the
    same transaction, so the change and its job are saved together or not at all."""
    try:
        if resync:
            # flush sends the INSERT/UPDATE now, inside the open transaction. A new
            # source needs that before the job row can reference it by foreign key.
            # A duplicate also fails here, and lands in the except below like at commit.
            await session.flush()
            await enqueue_sync(session, workspace_id=source.workspace_id, source_id=source.id)
        await session.commit()
    except IntegrityError as exc:
        # After a failed commit the session is unusable until rolled back.
        await session.rollback()
        # exc.orig is the driver's error. Postgres names the violated index in it.
        detail = str(exc.orig)
        if UNIQUE_CONTENT_INDEX in detail:
            raise _duplicate() from exc
        if UNIQUE_TICKET_INDEX in detail:
            raise _already_proposed() from exc
        # Any other integrity error (for example a foreign key) is a real bug: let it
        # reach the 500 handler instead of hiding it behind a 409.
        raise
    # Re-read the row so the returned object matches exactly what was stored.
    await session.refresh(source)
    return source


def _already_proposed() -> AppError:
    return AppError(
        "already_proposed",
        "This ticket already has a pending or approved knowledge entry",
        status_code=409,
    )


def _mark_reviewed(source: KnowledgeSource, reviewer_id: uuid.UUID, note: str | None) -> None:
    source.reviewed_by = reviewer_id
    source.reviewed_at = utcnow()
    source.review_note = note


def _clear_review(source: KnowledgeSource) -> None:
    source.reviewed_by = None
    source.reviewed_at = None
    source.review_note = None


async def create_document(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    payload: DocumentCreate,
    *,
    actor: WorkspaceMember,
) -> KnowledgeSource:
    kind = KnowledgeKind.document.value
    digest = content_hash(kind, None, payload.body)
    await _ensure_unique_content(session, workspace_id, digest)
    source = KnowledgeSource(
        workspace_id=workspace_id,
        kind=kind,
        status=KnowledgeStatus.pending_review.value,
        title=payload.title.strip(),
        question=None,
        body=payload.body,
        content_hash=digest,
        version=1,
        created_by=actor.user_id,
    )
    # Owners and admins are the reviewers, so their documents count as reviewed by them.
    if is_reviewer(actor):
        source.status = KnowledgeStatus.approved.value
        _mark_reviewed(source, actor.user_id, None)
    session.add(source)
    approved = source.status == KnowledgeStatus.approved.value
    return await _commit(session, source, resync=approved)


async def propose_ticket_answer(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    ticket_id: uuid.UUID,
    payload: TicketAnswerCreate,
    *,
    actor: WorkspaceMember,
) -> KnowledgeSource:
    """Every ticket answer waits for review, even from owners and admins, because
    it is written from a real customer conversation and may still hold their details."""
    # get_ticket returns 404 when the ticket belongs to another workspace.
    ticket = await ticket_service.get_ticket(session, workspace_id, ticket_id)
    # Open tickets have no confirmed answer yet, so there is nothing safe to reuse.
    if ticket.status not in _RESOLVED_TICKET_STATUSES:
        raise AppError(
            "ticket_not_resolved",
            "Only resolved or closed tickets can become knowledge",
            status_code=409,
        )
    existing = await session.scalar(
        select(KnowledgeSource.id)
        .where(
            KnowledgeSource.source_ticket_id == ticket.id,
            KnowledgeSource.status.in_(LIVE_STATUSES),
        )
        .limit(1)
    )
    if existing is not None:
        raise _already_proposed()

    kind = KnowledgeKind.ticket_answer.value
    digest = content_hash(kind, payload.question, payload.body)
    await _ensure_unique_content(session, workspace_id, digest)
    source = KnowledgeSource(
        workspace_id=workspace_id,
        kind=kind,
        status=KnowledgeStatus.pending_review.value,
        title=payload.title.strip(),
        question=payload.question,
        body=payload.body,
        content_hash=digest,
        version=1,
        source_ticket_id=ticket.id,
        created_by=actor.user_id,
    )
    session.add(source)
    return await _commit(session, source)


async def list_sources(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    status: str | None,
    kind: str | None,
    limit: int,
    offset: int,
) -> tuple[list[KnowledgeSource], int]:
    # The workspace filter is always first. It matches the leading column of the
    # (workspace_id, status) index, so Postgres can use that index for both filters.
    filters = [KnowledgeSource.workspace_id == workspace_id]
    if status is not None:
        filters.append(KnowledgeSource.status == status)
    if kind is not None:
        filters.append(KnowledgeSource.kind == kind)
    total = await session.scalar(select(func.count()).select_from(KnowledgeSource).where(*filters))
    result = await session.scalars(
        select(KnowledgeSource)
        .where(*filters)
        # id breaks ties between equal timestamps, so pages never repeat or skip rows.
        .order_by(KnowledgeSource.updated_at.desc(), KnowledgeSource.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result.all()), int(total or 0)


async def get_source(
    session: AsyncSession, workspace_id: uuid.UUID, source_id: uuid.UUID
) -> KnowledgeSource:
    # session.get looks up by primary key only, so the workspace must be checked here.
    # Without it, a member of workspace A could read B's source by guessing its id.
    source = await session.get(KnowledgeSource, source_id)
    if source is None or source.workspace_id != workspace_id:
        raise AppError("not_found", "Knowledge source not found", status_code=404)
    return source


async def update_source(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    payload: KnowledgeUpdate,
    *,
    actor: WorkspaceMember,
) -> KnowledgeSource:
    """Owners and admins can edit anything not archived; approved text they edit stays
    approved, with them as reviewer. Agents can edit only their own pending or rejected
    sources. Editing a rejected source sends it back for review."""
    source = await get_source(session, workspace_id, source_id)
    if source.status == KnowledgeStatus.archived.value:
        raise AppError("archived", "Archived knowledge cannot be edited", status_code=409)
    reviewer = is_reviewer(actor)
    if not reviewer:
        editable = {KnowledgeStatus.pending_review.value, KnowledgeStatus.rejected.value}
        if source.created_by != actor.user_id or source.status not in editable:
            raise AppError(
                "forbidden",
                "Agents can edit only their own pending or rejected knowledge",
                status_code=403,
            )

    # exclude_unset keeps only fields the client actually sent, so a PATCH with just
    # {"title": ...} leaves question and body alone.
    changes = payload.model_dump(exclude_unset=True)
    if "question" in changes and source.kind != KnowledgeKind.ticket_answer.value:
        raise AppError("invalid_field", "Only ticket answers have a question", status_code=400)
    for field in ("title", "question", "body"):
        if field in changes and changes[field] is None:
            raise AppError("invalid_field", f"{field} cannot be null", status_code=400)
    if "title" in changes:
        changes["title"] = changes["title"].strip()
    # Drop fields sent with their current value, so a no-op edit does not bump the version.
    changed = {key: value for key, value in changes.items() if getattr(source, key) != value}
    if not changed:
        raise AppError("empty_update", "At least one field must change", status_code=400)

    question = changed.get("question", source.question)
    body = changed.get("body", source.body)
    digest = content_hash(source.kind, question, body)
    # A title-only edit keeps the same hash and cannot create a duplicate.
    if digest != source.content_hash:
        await _ensure_unique_content(session, workspace_id, digest, exclude_id=source.id)

    for key, value in changed.items():
        setattr(source, key, value)
    source.content_hash = digest
    source.version += 1
    source.updated_at = utcnow()

    # Only reviewers reach this branch: agents were refused above for approved sources.
    if source.status == KnowledgeStatus.approved.value:
        _mark_reviewed(source, actor.user_id, None)
    elif source.status == KnowledgeStatus.rejected.value:
        source.status = KnowledgeStatus.pending_review.value
        _clear_review(source)
    # Edits to pending or rejected drafts change nothing retrievable.
    approved = source.status == KnowledgeStatus.approved.value
    return await _commit(session, source, resync=approved)


async def approve_source(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    actor: WorkspaceMember,
) -> KnowledgeSource:
    source = await get_source(session, workspace_id, source_id)
    if source.status != KnowledgeStatus.pending_review.value:
        raise AppError("invalid_status", "Only pending knowledge can be approved", status_code=409)
    source.status = KnowledgeStatus.approved.value
    _mark_reviewed(source, actor.user_id, None)
    source.updated_at = utcnow()
    return await _commit(session, source, resync=True)


async def reject_source(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    note: str,
    *,
    actor: WorkspaceMember,
) -> KnowledgeSource:
    source = await get_source(session, workspace_id, source_id)
    if source.status != KnowledgeStatus.pending_review.value:
        raise AppError("invalid_status", "Only pending knowledge can be rejected", status_code=409)
    source.status = KnowledgeStatus.rejected.value
    _mark_reviewed(source, actor.user_id, note.strip())
    source.updated_at = utcnow()
    return await _commit(session, source)


async def archive_source(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
) -> KnowledgeSource:
    """Archiving replaces delete, so answers that cited this source stay traceable."""
    source = await get_source(session, workspace_id, source_id)
    if source.status == KnowledgeStatus.archived.value:
        raise AppError("invalid_status", "Knowledge is already archived", status_code=409)
    # Only an approved source has chunks to remove.
    was_approved = source.status == KnowledgeStatus.approved.value
    source.status = KnowledgeStatus.archived.value
    source.updated_at = utcnow()
    return await _commit(session, source, resync=was_approved)
