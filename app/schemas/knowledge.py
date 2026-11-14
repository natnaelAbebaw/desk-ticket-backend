"""Request and response bodies for the knowledge API.

Field limits are enforced by Pydantic before any route code runs; a request over a
limit gets 422. Nothing here carries a workspace or user id: those come from the token.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.knowledge import KnowledgeKind, KnowledgeStatus

TITLE_MAX = 300
QUESTION_MAX = 5_000
# Roughly 50 pages of text. Bigger documents should be split before upload.
BODY_MAX = 200_000


class DocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    body: str = Field(min_length=1, max_length=BODY_MAX, description="Markdown")


class TicketAnswerCreate(BaseModel):
    """A reviewed question and answer written from a resolved ticket.

    Write it for reuse: no customer names, emails, account numbers, or other
    details specific to one customer.
    """

    title: str = Field(min_length=1, max_length=TITLE_MAX)
    question: str = Field(min_length=1, max_length=QUESTION_MAX)
    body: str = Field(min_length=1, max_length=BODY_MAX, description="Markdown answer")


class KnowledgeUpdate(BaseModel):
    """PATCH body. Every field is optional; only the ones sent are changed."""

    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX)
    question: str | None = Field(default=None, min_length=1, max_length=QUESTION_MAX)
    body: str | None = Field(default=None, min_length=1, max_length=BODY_MAX)


class KnowledgeReject(BaseModel):
    note: str = Field(min_length=1, max_length=2_000, description="Why it was rejected")


class KnowledgeRead(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    kind: KnowledgeKind
    status: KnowledgeStatus
    title: str
    question: str | None
    body: str
    version: int
    source_ticket_id: uuid.UUID | None
    created_by: uuid.UUID
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    review_note: str | None
    created_at: datetime
    updated_at: datetime

    # from_attributes lets model_validate read a SQLAlchemy row's attributes directly.
    model_config = {"from_attributes": True}


class KnowledgeSummary(BaseModel):
    """List item without the body, which can be large."""

    id: uuid.UUID
    kind: KnowledgeKind
    status: KnowledgeStatus
    title: str
    version: int
    source_ticket_id: uuid.UUID | None
    created_by: uuid.UUID
    updated_at: datetime

    model_config = {"from_attributes": True}
