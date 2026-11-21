"""The demo seed loads through the real services and produces searchable knowledge.

This keeps `make seed` from silently breaking when a service rule changes: if a rule
starts rejecting the demo data (say, a new required field), this test fails first.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.embeddings import FakeEmbedder
from app.ingestion import worker
from app.models.ingestion import IngestionJob, JobStatus
from app.models.knowledge import KnowledgeSource, KnowledgeStatus
from app.models.ticket import Ticket
from app.search.service import SearchMode, search
from app.seed.__main__ import already_seeded, reset, seed_workspace
from app.seed.data import WORKSPACES


async def test_seed_loads_and_becomes_searchable(
    db_engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: FakeEmbedder,
) -> None:
    async with factory() as session:
        kettle_id, harbor_id = [await seed_workspace(session, spec) for spec in WORKSPACES]
        assert await already_seeded(session)

        kettle = WORKSPACES[0]
        assert await session.scalar(
            select(func.count()).where(Ticket.workspace_id == kettle_id)
        ) == len(kettle.tickets)
        statuses = dict(
            (
                await session.execute(
                    select(KnowledgeSource.status, func.count())
                    .where(KnowledgeSource.workspace_id == kettle_id)
                    .group_by(KnowledgeSource.status)
                )
            ).all()
        )
        # 6 published documents + 4 approved ticket answers. The agent's gift-card draft
        # and one ticket answer wait for review; one document and one answer rejected.
        assert statuses == {
            KnowledgeStatus.approved.value: 10,
            KnowledgeStatus.pending_review.value: 2,
            KnowledgeStatus.rejected.value: 2,
        }

    # Drain the queue the way `make worker` would.
    while await worker.run_once(factory, settings, embedder):
        pass

    async with factory() as session:
        assert not await session.scalar(
            select(func.count()).where(IngestionJob.status != JobStatus.succeeded.value)
        )
        result = await search(
            session, embedder, workspace_id=kettle_id, query="K-415", mode=SearchMode.keyword
        )
        assert result.hits, "the card reader article should match its own error code"
        refunds = await search(session, embedder, workspace_id=kettle_id, query="refund", limit=20)
        text = " ".join(hit.content for hit in refunds.hits)
        # Harbor Books' 30-day policy must never show up in Kettle's results.
        assert "30 days of delivery" not in text
        # The pending ticket answer (refund to a new card) is not retrievable yet.
        assert all(hit.source_title != "Refund to a replacement card" for hit in refunds.hits)

        await reset(session)
        assert not await already_seeded(session)
        assert harbor_id  # both workspaces were created
