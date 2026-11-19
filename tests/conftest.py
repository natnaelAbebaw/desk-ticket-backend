import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://desk:desk@localhost:5432/desk_test"


def _test_url() -> URL:
    url = make_url(os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL))
    if url.get_backend_name() != "postgresql":
        pytest.exit("TEST_DATABASE_URL must point at Postgres", returncode=2)
    # The test database is dropped and recreated on every run. Refuse anything that
    # does not look like a throwaway database.
    if not (url.database or "").endswith("_test"):
        pytest.exit("TEST_DATABASE_URL database name must end with _test", returncode=2)
    return url


TEST_URL = _test_url()
# app.db.session builds its engine at import time from DATABASE_URL, and a local .env
# may point somewhere else. Environment variables win over .env, so set it before any
# app import below. This file sits outside the import sort for that reason (E402).
os.environ["DATABASE_URL"] = TEST_URL.render_as_string(hide_password=False)
# Belt and braces: tests pass a FakeEmbedder explicitly, but anything that builds an
# embedder from settings must never reach out to a real model either.
os.environ["EMBEDDING_PROVIDER"] = "fake"

import pytest_asyncio
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from app.core.config import Settings, get_settings
from app.core.redis import close_redis
from app.db.base import Base
from app.db.session import get_db
from app.embeddings import FakeEmbedder
from app.main import app


async def _recreate_database(url: URL) -> None:
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
            await connection.execute(text(f'CREATE DATABASE "{url.database}"'))
    except (OSError, SQLAlchemyError) as exc:
        pytest.exit(
            f"Postgres is not reachable at {url.host}:{url.port} ({exc}). Start it with `make db`.",
            returncode=2,
        )
    finally:
        await admin.dispose()


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """A fresh database built by the real migrations, once per test run."""
    asyncio.run(_recreate_database(TEST_URL))
    get_settings.cache_clear()
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    yield os.environ["DATABASE_URL"]


@pytest_asyncio.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_engine(engine: AsyncEngine) -> AsyncEngine:
    """The shared engine, with every table emptied before the test."""
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return engine


@pytest_asyncio.fixture
async def client(
    monkeypatch: pytest.MonkeyPatch, db_engine: AsyncEngine
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-that-is-at-least-32-chars")
    get_settings.cache_clear()
    await close_redis()
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()
    await close_redis()
    get_settings.cache_clear()


# Worker fixtures, for tests that drive the ingestion worker directly.


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions for the worker under test, like app.db.session.SessionLocal."""
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def settings() -> Settings:
    # Small numbers so retry tests finish in a few rounds.
    return get_settings().model_copy(
        update={"ingestion_max_attempts": 3, "ingestion_lease_seconds": 60}
    )


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()
