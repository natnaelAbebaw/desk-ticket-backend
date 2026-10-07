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
    created_by: uuid.UUID
    assigned_to: uuid.UUID | None = None


class TicketUpdate(BaseModel):
    actor_id: uuid.UUID
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
    author_id: uuid.UUID | None = None
    body: str = Field(min_length=1)
    message_type: MessageType = MessageType.reply

    @model_validator(mode="after")
    def author_required_for_people(self) -> "TicketMessageCreate":
        if self.message_type != MessageType.system and self.author_id is None:
            raise ValueError("author_id is required unless message_type is system")
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
