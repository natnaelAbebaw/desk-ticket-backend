import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import bump_ticket_list_version
from app.core.errors import AppError
from app.models.ticket import (
    Ticket,
    TicketEvent,
    TicketEventType,
    TicketMessage,
    TicketStatus,
)
from app.models.timestamps import utcnow
from app.schemas.ticket import TicketCreate, TicketMessageCreate, TicketUpdate
from app.services import workspaces as workspace_service

_FIELD_EVENTS = {
    "subject": TicketEventType.subject_changed,
    "description": TicketEventType.description_changed,
    "priority": TicketEventType.priority_changed,
    "category": TicketEventType.category_changed,
    "assigned_to": TicketEventType.assignee_changed,
}

_CLOSED_STATUSES = {TicketStatus.resolved.value, TicketStatus.closed.value}
_OPEN_STATUSES = {TicketStatus.open.value, TicketStatus.pending.value}


def _stored(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _status_event(old: str, new: str) -> TicketEventType:
    if new == TicketStatus.closed.value:
        return TicketEventType.closed
    if old in _CLOSED_STATUSES and new in _OPEN_STATUSES:
        return TicketEventType.reopened
    return TicketEventType.status_changed


def _add_event(
    session: AsyncSession,
    ticket: Ticket,
    *,
    actor_id: uuid.UUID | None,
    event_type: TicketEventType,
    old_value: str | None = None,
    new_value: str | None = None,
    metadata: dict | None = None,
) -> None:
    session.add(
        TicketEvent(
            ticket_id=ticket.id,
            actor_id=actor_id,
            event_type=event_type.value,
            old_value=old_value,
            new_value=new_value,
            metadata_=metadata,
        )
    )


async def create_ticket(
    session: AsyncSession, workspace_id: uuid.UUID, payload: TicketCreate
) -> Ticket:
    await workspace_service.get_workspace(session, workspace_id)
    await workspace_service.require_member(session, workspace_id, payload.created_by)
    if payload.assigned_to is not None:
        await workspace_service.require_member(session, workspace_id, payload.assigned_to)
    ticket = Ticket(
        workspace_id=workspace_id,
        created_by=payload.created_by,
        assigned_to=payload.assigned_to,
        subject=payload.subject,
        description=payload.description,
        priority=payload.priority.value,
        category=payload.category.value,
    )
    session.add(ticket)
    await session.flush()
    _add_event(session, ticket, actor_id=payload.created_by, event_type=TicketEventType.created)
    await session.commit()
    await session.refresh(ticket)
    await bump_ticket_list_version(workspace_id)
    return ticket


async def list_tickets(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    status: str | None,
    priority: str | None,
    category: str | None,
    assigned_to: uuid.UUID | None,
    limit: int,
    offset: int,
) -> tuple[list[Ticket], int]:
    await workspace_service.get_workspace(session, workspace_id)
    filters = [Ticket.workspace_id == workspace_id]
    if status is not None:
        filters.append(Ticket.status == status)
    if priority is not None:
        filters.append(Ticket.priority == priority)
    if category is not None:
        filters.append(Ticket.category == category)
    if assigned_to is not None:
        filters.append(Ticket.assigned_to == assigned_to)

    total = await session.scalar(select(func.count()).select_from(Ticket).where(*filters))
    result = await session.scalars(
        select(Ticket)
        .where(*filters)
        .order_by(Ticket.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.all()), int(total or 0)


async def get_ticket(
    session: AsyncSession, workspace_id: uuid.UUID, ticket_id: uuid.UUID
) -> Ticket:
    await workspace_service.get_workspace(session, workspace_id)
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None or ticket.workspace_id != workspace_id:
        raise AppError("not_found", "Ticket not found", status_code=404)
    return ticket


async def update_ticket(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    ticket_id: uuid.UUID,
    payload: TicketUpdate,
) -> Ticket:
    ticket = await get_ticket(session, workspace_id, ticket_id)
    await workspace_service.require_member(session, workspace_id, payload.actor_id)
    changes = payload.model_dump(exclude_unset=True)
    changes.pop("actor_id")
    if not changes:
        raise AppError("empty_update", "At least one field is required", status_code=400)
    if "assigned_to" in changes and changes["assigned_to"] is not None:
        await workspace_service.require_member(session, workspace_id, changes["assigned_to"])

    changed = False
    for field, value in changes.items():
        stored = value.value if hasattr(value, "value") else value
        previous = getattr(ticket, field)
        if _stored(previous) == _stored(stored):
            continue
        if field == "status":
            event_type = _status_event(_stored(previous) or "", _stored(stored) or "")
            if stored in _CLOSED_STATUSES:
                ticket.closed_at = utcnow()
            else:
                ticket.closed_at = None
        else:
            event_type = _FIELD_EVENTS[field]
        setattr(ticket, field, stored)
        _add_event(
            session,
            ticket,
            actor_id=payload.actor_id,
            event_type=event_type,
            old_value=_stored(previous),
            new_value=_stored(stored),
        )
        changed = True

    if not changed:
        raise AppError("empty_update", "At least one field is required", status_code=400)
    ticket.updated_at = utcnow()
    await session.commit()
    await session.refresh(ticket)
    await bump_ticket_list_version(workspace_id)
    return ticket


async def add_message(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    ticket_id: uuid.UUID,
    payload: TicketMessageCreate,
) -> TicketMessage:
    ticket = await get_ticket(session, workspace_id, ticket_id)
    if payload.author_id is not None:
        await workspace_service.require_member(session, workspace_id, payload.author_id)
    message = TicketMessage(
        ticket_id=ticket.id,
        author_id=payload.author_id,
        body=payload.body,
        message_type=payload.message_type.value,
    )
    session.add(message)
    await session.flush()
    _add_event(
        session,
        ticket,
        actor_id=payload.author_id,
        event_type=TicketEventType.message_added,
        new_value=str(message.id),
        metadata={"message_type": payload.message_type.value},
    )
    ticket.updated_at = utcnow()
    await session.commit()
    await session.refresh(message)
    await bump_ticket_list_version(workspace_id)
    return message


async def list_messages(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    ticket_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[TicketMessage], int]:
    ticket = await get_ticket(session, workspace_id, ticket_id)
    filters = TicketMessage.ticket_id == ticket.id
    total = await session.scalar(select(func.count()).select_from(TicketMessage).where(filters))
    result = await session.scalars(
        select(TicketMessage)
        .where(filters)
        .order_by(TicketMessage.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.all()), int(total or 0)


async def list_events(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    ticket_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[TicketEvent], int]:
    ticket = await get_ticket(session, workspace_id, ticket_id)
    filters = TicketEvent.ticket_id == ticket.id
    total = await session.scalar(select(func.count()).select_from(TicketEvent).where(filters))
    result = await session.scalars(
        select(TicketEvent)
        .where(filters)
        .order_by(TicketEvent.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.all()), int(total or 0)
