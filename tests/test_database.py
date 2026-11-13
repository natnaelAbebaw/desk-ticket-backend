from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.base import Base


async def test_migrations_match_models(engine: AsyncEngine) -> None:
    """Fails when a model changes without a matching Alembic migration."""

    def diff(connection) -> list:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        return compare_metadata(context, Base.metadata)

    async with engine.connect() as connection:
        changes = await connection.run_sync(diff)
    assert changes == []


async def test_pgvector_is_available(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        version = await connection.scalar(
            text("SELECT default_version FROM pg_available_extensions WHERE name = 'vector'")
        )
    assert version is not None, "Postgres image has no pgvector; use pgvector/pgvector"
