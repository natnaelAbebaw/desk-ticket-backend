import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_api_key
from app.db.session import get_db
from app.models.ticket import TicketCategory, TicketPriority, TicketStatus
from app.schemas.common import Page
from app.schemas.ticket import (
    TicketCreate,
    TicketEventRead,
    TicketMessageCreate,
    TicketMessageRead,
    TicketRead,
    TicketUpdate,
)
from app.services import tickets as ticket_service

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("", response_model=TicketRead, status_code=201)
async def create_ticket(
    workspace_id: uuid.UUID,
    payload: TicketCreate,
    session: AsyncSession = Depends(get_db),
) -> TicketRead:
    ticket = await ticket_service.create_ticket(session, workspace_id, payload)
    return TicketRead.model_validate(ticket)


@router.get("", response_model=Page[TicketRead])
async def list_tickets(
    workspace_id: uuid.UUID,
    status: TicketStatus | None = None,
    priority: TicketPriority | None = None,
    category: TicketCategory | None = None,
    assigned_to: uuid.UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> Page[TicketRead]:
    items, total = await ticket_service.list_tickets(
        session,
        workspace_id,
        status=status.value if status else None,
        priority=priority.value if priority else None,
        category=category.value if category else None,
        assigned_to=assigned_to,
        limit=limit,
        offset=offset,
    )
    return Page(
        items=[TicketRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{ticket_id}", response_model=TicketRead)
async def get_ticket(
    workspace_id: uuid.UUID,
    ticket_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> TicketRead:
    ticket = await ticket_service.get_ticket(session, workspace_id, ticket_id)
    return TicketRead.model_validate(ticket)


@router.patch("/{ticket_id}", response_model=TicketRead)
async def update_ticket(
    workspace_id: uuid.UUID,
    ticket_id: uuid.UUID,
    payload: TicketUpdate,
    session: AsyncSession = Depends(get_db),
) -> TicketRead:
    ticket = await ticket_service.update_ticket(session, workspace_id, ticket_id, payload)
    return TicketRead.model_validate(ticket)


@router.post("/{ticket_id}/messages", response_model=TicketMessageRead, status_code=201)
async def add_message(
    workspace_id: uuid.UUID,
    ticket_id: uuid.UUID,
    payload: TicketMessageCreate,
    session: AsyncSession = Depends(get_db),
) -> TicketMessageRead:
    message = await ticket_service.add_message(session, workspace_id, ticket_id, payload)
    return TicketMessageRead.model_validate(message)


@router.get("/{ticket_id}/messages", response_model=Page[TicketMessageRead])
async def list_messages(
    workspace_id: uuid.UUID,
    ticket_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> Page[TicketMessageRead]:
    items, total = await ticket_service.list_messages(
        session, workspace_id, ticket_id, limit=limit, offset=offset
    )
    return Page(
        items=[TicketMessageRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{ticket_id}/events", response_model=Page[TicketEventRead])
async def list_events(
    workspace_id: uuid.UUID,
    ticket_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> Page[TicketEventRead]:
    items, total = await ticket_service.list_events(
        session, workspace_id, ticket_id, limit=limit, offset=offset
    )
    return Page(
        items=[TicketEventRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )
