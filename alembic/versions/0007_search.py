"""keyword and vector search indexes

Revision ID: 0007_search
Revises: 0006_embeddings
Create Date: 2026-11-19

"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR

from alembic import op

revision: str = "0007_search"
down_revision: str | None = "0006_embeddings"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # A STORED generated column: Postgres fills it for existing rows now (rewriting the
    # table once) and keeps it current on every insert and update after that.
    op.add_column(
        "knowledge_chunks",
        sa.Column(
            "search_vector",
            TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('english', heading), 'A') || "
                "setweight(to_tsvector('english', content), 'B')",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_search_vector",
        "knowledge_chunks",
        ["search_vector"],
        postgresql_using="gin",
    )
    # Building an HNSW index over millions of vectors takes a while and, like any
    # CREATE INDEX, blocks writes to the table meanwhile. On a large live table you
    # would run CREATE INDEX CONCURRENTLY outside a transaction instead. Here the table
    # is small, so a plain build inside the migration is fine.
    op.create_index(
        "ix_knowledge_chunks_embedding_hnsw",
        "knowledge_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_chunks_embedding_hnsw", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_search_vector", table_name="knowledge_chunks")
    op.drop_column("knowledge_chunks", "search_vector")
