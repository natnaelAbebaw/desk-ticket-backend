"""chunk embeddings

Revision ID: 0006_embeddings
Revises: 0005_ingestion
Create Date: 2026-11-19

"""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

revision: str = "0006_embeddings"
down_revision: str | None = "0005_ingestion"
branch_labels: str | None = None
depends_on: str | None = None

# Copied, not imported from app.models: a migration must keep describing the schema as
# it was when written, even if the model's constant changes later.
EMBEDDING_DIMENSIONS = 768


def upgrade() -> None:
    # The pgvector image ships the extension, but each database has to turn it on once.
    # IF NOT EXISTS keeps this safe on databases where someone already enabled it.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # All new columns are nullable, so this is a fast metadata-only change even on a
    # large table: no existing row has to be rewritten.
    op.add_column(
        "knowledge_chunks",
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
    )
    op.add_column(
        "knowledge_chunks",
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "knowledge_chunks",
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("ingestion_jobs", sa.Column("embedded_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("ingestion_jobs", "embedded_count")
    op.drop_column("knowledge_chunks", "embedded_at")
    op.drop_column("knowledge_chunks", "embedding_model")
    op.drop_column("knowledge_chunks", "embedding")
    # The extension is left installed: dropping it would fail if anything else in the
    # database used it, and an unused extension costs nothing.
