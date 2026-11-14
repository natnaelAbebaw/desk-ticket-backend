import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.timestamps import utcnow


# StrEnum members are also plain strings, so they serialize to JSON as "document" etc.
# The columns below store them as String(32) rather than a Postgres ENUM type, because
# adding a value to a Postgres ENUM needs its own migration step.
class KnowledgeKind(StrEnum):
    # A help article, policy, or product doc written for the knowledge base.
    document = "document"
    # A cleaned-up question and answer taken from a resolved ticket.
    ticket_answer = "ticket_answer"


class KnowledgeStatus(StrEnum):
    pending_review = "pending_review"
    # Only approved sources are ingested and can be retrieved for answers.
    approved = "approved"
    rejected = "rejected"
    # Kept for history; never retrieved again.
    archived = "archived"


# Partial unique indexes: they only cover live rows (pending or approved), so a
# rejected or archived copy never blocks a new one.
_LIVE = text("status IN ('pending_review', 'approved')")


class KnowledgeSource(Base):
    """One piece of company knowledge the assistant may later cite.

    Chunks and embeddings are derived from approved sources in later milestones.
    The source row is the record of truth: what the text says, who wrote it,
    and who approved it.
    """

    __tablename__ = "knowledge_sources"
    __table_args__ = (
        # Serves the list endpoint and, later, ingestion's "approved sources in this
        # workspace" query. workspace_id comes first because every query filters on it.
        Index("ix_knowledge_sources_workspace_status", "workspace_id", "status"),
        # No two live sources in a workspace with the same content.
        Index(
            "uq_knowledge_sources_live_content",
            "workspace_id",
            "content_hash",
            unique=True,
            postgresql_where=_LIVE,
        ),
        # At most one live knowledge entry per ticket.
        Index(
            "uq_knowledge_sources_live_ticket",
            "source_ticket_id",
            unique=True,
            postgresql_where=_LIVE,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(300))
    # The customer's question. Set for ticket answers, empty for documents.
    question: Mapped[str | None] = mapped_column(Text)
    # Markdown. The article text, or the approved answer.
    body: Mapped[str] = mapped_column(Text)
    # SHA-256 of the normalized content, used to reject duplicates.
    content_hash: Mapped[str] = mapped_column(String(64))
    # Goes up by one each time title, question, or body changes.
    version: Mapped[int] = mapped_column(Integer, default=1)
    # SET NULL: deleting the ticket keeps the knowledge, which no longer needs the ticket.
    source_ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tickets.id", ondelete="SET NULL")
    )
    # RESTRICT: a user who wrote knowledge cannot be deleted without deciding what
    # happens to it first. Same rule as tickets.created_by.
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="RESTRICT"))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
