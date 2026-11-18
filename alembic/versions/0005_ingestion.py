"""knowledge chunks and the ingestion job queue

Revision ID: 0005_ingestion
Revises: 0004_knowledge_sources
Create Date: 2026-11-15

"""

import sqlalchemy as sa

from alembic import op

revision: str = "0005_ingestion"
down_revision: str | None = "0004_knowledge_sources"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("heading", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("token_estimate", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["knowledge_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_chunks_workspace_source",
        "knowledge_chunks",
        ["workspace_id", "source_id"],
    )
    op.create_index(
        "ix_knowledge_chunks_source_position",
        "knowledge_chunks",
        ["source_id", "position"],
    )

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "run_after",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["knowledge_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_ingestion_jobs_queued_source",
        "ingestion_jobs",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_ingestion_jobs_claimable",
        "ingestion_jobs",
        ["run_after"],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_ingestion_jobs_source_created",
        "ingestion_jobs",
        ["source_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ingestion_jobs_source_created", table_name="ingestion_jobs")
    op.drop_index("ix_ingestion_jobs_claimable", table_name="ingestion_jobs")
    op.drop_index("uq_ingestion_jobs_queued_source", table_name="ingestion_jobs")
    op.drop_table("ingestion_jobs")
    op.drop_index("ix_knowledge_chunks_source_position", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_workspace_source", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
