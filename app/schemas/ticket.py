import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.models.ticket import (
    MessageType,
    TicketCategory,
    TicketEventType,
    TicketPriority,
    TicketStatus,
)


class TicketCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    description: str = ""
    priority: TicketPriority = TicketPriority.medium
    category: TicketCategory = TicketCategory.general
    assigned_to: uuid.UUID | None = None


class TicketUpdate(BaseModel):
    subject: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    category: TicketCategory | None = None
    assigned_to: uuid.UUID | None = None


class TicketRead(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    created_by: uuid.UUID
    assigned_to: uuid.UUID | None
    subject: str
    description: str
    status: TicketStatus
    priority: TicketPriority
    category: TicketCategory
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None

    model_config = {"from_attributes": True}


class TicketMessageCreate(BaseModel):
    body: str = Field(min_length=1)
    message_type: MessageType = MessageType.reply

    @model_validator(mode="after")
    def system_messages_are_server_only(self) -> "TicketMessageCreate":
        if self.message_type == MessageType.system:
            raise ValueError("system messages are written by the server, not by API callers")
        return self


class TicketMessageRead(BaseModel):
    id: uuid.UUID
    ticket_id: uuid.UUID
    author_id: uuid.UUID | None
    body: str
    message_type: MessageType
    created_at: datetime

    model_config = {"from_attributes": True}


class TicketEventRead(BaseModel):
    id: uuid.UUID
    ticket_id: uuid.UUID
    actor_id: uuid.UUID | None
    event_type: TicketEventType
    old_value: str | None
    new_value: str | None
    metadata: dict | None = Field(default=None, validation_alias="metadata_")
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}
