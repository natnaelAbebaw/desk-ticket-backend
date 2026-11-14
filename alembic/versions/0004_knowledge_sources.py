"""knowledge sources: documents and approved ticket answers

Revision ID: 0004_knowledge_sources
Revises: 0003_ticket_workspace_created
Create Date: 2026-11-13

"""

import sqlalchemy as sa

from alembic import op

# Must match _LIVE in app/models/knowledge.py, or tests/test_database.py reports drift.
# postgresql_where makes these partial indexes: only rows matching the condition are
# indexed, so uniqueness applies to live rows and ignores rejected or archived copies.
_LIVE = sa.text("status IN ('pending_review', 'approved')")

revision: str = "0004_knowledge_sources"
down_revision: str | None = "0003_ticket_workspace_created"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_ticket_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_ticket_id"], ["tickets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_sources_workspace_status",
        "knowledge_sources",
        ["workspace_id", "status"],
    )
    op.create_index(
        "uq_knowledge_sources_live_content",
        "knowledge_sources",
        ["workspace_id", "content_hash"],
        unique=True,
        postgresql_where=_LIVE,
    )
    op.create_index(
        "uq_knowledge_sources_live_ticket",
        "knowledge_sources",
        ["source_ticket_id"],
        unique=True,
        postgresql_where=_LIVE,
    )


def downgrade() -> None:
    op.drop_index("uq_knowledge_sources_live_ticket", table_name="knowledge_sources")
    op.drop_index("uq_knowledge_sources_live_content", table_name="knowledge_sources")
    op.drop_index("ix_knowledge_sources_workspace_status", table_name="knowledge_sources")
    op.drop_table("knowledge_sources")
