import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.timestamps import utcnow


class TicketStatus(StrEnum):
    open = "open"
    pending = "pending"
    resolved = "resolved"
    closed = "closed"


class TicketPriority(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    urgent = "urgent"


class TicketCategory(StrEnum):
    general = "general"
    billing = "billing"
    technical = "technical"
    account = "account"
    bug = "bug"
    feature_request = "feature_request"


class MessageType(StrEnum):
    reply = "reply"
    internal_note = "internal_note"
    system = "system"


class TicketEventType(StrEnum):
    created = "created"
    status_changed = "status_changed"
    priority_changed = "priority_changed"
    assignee_changed = "assignee_changed"
    category_changed = "category_changed"
    subject_changed = "subject_changed"
    description_changed = "description_changed"
    closed = "closed"
    reopened = "reopened"
    message_added = "message_added"


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="RESTRICT"))
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    subject: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default=TicketStatus.open.value, index=True)
    priority: Mapped[str] = mapped_column(
        String(32), default=TicketPriority.medium.value, index=True
    )
    category: Mapped[str] = mapped_column(
        String(32), default=TicketCategory.general.value, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    body: Mapped[str] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(String(32), default=MessageType.reply.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TicketEvent(Base):
    __tablename__ = "ticket_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(String(32))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
