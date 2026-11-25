"""Load the fictional demo workspaces from app/seed/data.py into the database.

    python -m app.seed            # create, or say it is already there
    python -m app.seed --reset    # delete the demo data first, then create it again
    python -m app.seed --wait     # also wait until the worker has embedded everything

Everything goes through the same service functions the API routes call, not raw
INSERTs. So the demo data follows the real rules: an owner's document is published,
an agent's waits for review, a ticket answer needs a resolved ticket, every change
writes ticket events, and approved knowledge queues an ingestion job for the worker.

The worker (`make worker`) and Ollama must be running for the knowledge to become
searchable. Seeding only queues the jobs.
"""

import argparse
import asyncio
import time
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.models.ingestion import IngestionJob, JobStatus
from app.models.knowledge import KnowledgeSource
from app.models.ticket import Ticket as TicketRow
from app.models.user import User
from app.models.workspace import Workspace as WorkspaceRow
from app.models.workspace import WorkspaceMember, WorkspaceRole
from app.schemas.knowledge import DocumentCreate, TicketAnswerCreate
from app.schemas.ticket import TicketCreate, TicketMessageCreate, TicketUpdate
from app.schemas.user import UserCreate
from app.schemas.workspace import MemberCreate, WorkspaceCreate
from app.seed.data import PASSWORD, WORKSPACES, Workspace
from app.services import knowledge as knowledge_service
from app.services import tickets as ticket_service
from app.services import users as user_service
from app.services import workspaces as workspace_service

# Give up waiting for the worker after this long. Embedding ~30 chunks on a laptop CPU
# takes well under a minute; much longer means the worker or Ollama is not running.
WAIT_TIMEOUT_SECONDS = 300


async def already_seeded(session: AsyncSession) -> bool:
    slugs = [ws.slug for ws in WORKSPACES]
    found = await session.scalar(select(func.count()).where(WorkspaceRow.slug.in_(slugs)))
    return bool(found)


async def reset(session: AsyncSession) -> None:
    """Delete the demo workspaces and users.

    Deleting a workspace removes its members, tickets, messages, events, knowledge,
    chunks, and jobs: their foreign keys are ON DELETE CASCADE, so Postgres does it in
    the same statement. Users go last, because tickets and knowledge reference their
    creator with ON DELETE RESTRICT and would block the delete while they exist.
    """
    slugs = [ws.slug for ws in WORKSPACES]
    emails = [person.email for ws in WORKSPACES for person in ws.people]
    await session.execute(delete(WorkspaceRow).where(WorkspaceRow.slug.in_(slugs)))
    await session.execute(delete(User).where(User.email.in_(emails)))
    await session.commit()


async def seed_workspace(session: AsyncSession, spec: Workspace) -> uuid.UUID:
    # key in data.py -> user id
    ids: dict[str, uuid.UUID] = {}
    for person in spec.people:
        user = await user_service.create_user(
            session, UserCreate(email=person.email, name=person.name, password=PASSWORD)
        )
        ids[person.key] = user.id

    owner = next(p for p in spec.people if p.role == WorkspaceRole.owner.value)
    workspace = await workspace_service.create_workspace(
        session, WorkspaceCreate(name=spec.name, slug=spec.slug), owner_id=ids[owner.key]
    )
    for person in spec.people:
        if person is owner:
            continue  # create_workspace already made them the owner
        await workspace_service.add_member(
            session,
            workspace.id,
            MemberCreate(user_id=ids[person.key], role=WorkspaceRole(person.role)),
            actor_role=WorkspaceRole.owner.value,
        )

    async def member(key: str) -> WorkspaceMember:
        # The knowledge services take the membership row: it carries the role that
        # decides, for example, whether a new document is published or waits for review.
        if key not in ids:
            # Most likely a typo in a knowledge file's "author:" line.
            raise ValueError(f"{spec.slug}: unknown person {key!r}; known: {sorted(ids)}")
        row = await session.get(WorkspaceMember, (workspace.id, ids[key]))
        assert row is not None
        return row

    owner_member = await member(owner.key)

    for doc in spec.docs:
        source = await knowledge_service.create_document(
            session,
            workspace.id,
            DocumentCreate(title=doc.title, body=doc.body),
            actor=await member(doc.author),
        )
        if doc.review == "approve":
            await knowledge_service.approve_source(
                session, workspace.id, source.id, actor=owner_member
            )
        elif doc.review == "reject":
            await knowledge_service.reject_source(
                session, workspace.id, source.id, doc.note, actor=owner_member
            )

    for spec_ticket in spec.tickets:
        ticket = await ticket_service.create_ticket(
            session,
            workspace.id,
            TicketCreate(
                subject=spec_ticket.subject,
                description=spec_ticket.description,
                priority=spec_ticket.priority,
                category=spec_ticket.category,
                assigned_to=ids[spec_ticket.assignee] if spec_ticket.assignee else None,
            ),
            actor_id=ids[spec_ticket.requester],
        )
        for author, message_type, body in spec_ticket.messages:
            await ticket_service.add_message(
                session,
                workspace.id,
                ticket.id,
                TicketMessageCreate(body=body, message_type=message_type),
                author_id=ids[author],
            )
        if spec_ticket.status != "open":
            # The assignee closes their own tickets, so the event log shows who did it.
            actor = spec_ticket.assignee or owner.key
            await ticket_service.update_ticket(
                session,
                workspace.id,
                ticket.id,
                TicketUpdate(status=spec_ticket.status),
                actor_id=ids[actor],
            )
        if spec_ticket.answer is not None:
            answer = spec_ticket.answer
            source = await knowledge_service.propose_ticket_answer(
                session,
                workspace.id,
                ticket.id,
                TicketAnswerCreate(title=answer.title, question=answer.question, body=answer.body),
                actor=await member(spec_ticket.assignee or owner.key),
            )
            if answer.review == "approve":
                await knowledge_service.approve_source(
                    session, workspace.id, source.id, actor=owner_member
                )
            elif answer.review == "reject":
                await knowledge_service.reject_source(
                    session, workspace.id, source.id, answer.note, actor=owner_member
                )
    return workspace.id


async def wait_for_worker(session: AsyncSession, workspace_ids: list[uuid.UUID]) -> bool:
    """Poll until no job for these workspaces is queued or running."""
    unfinished = (JobStatus.queued.value, JobStatus.running.value)
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        left = await session.scalar(
            select(func.count())
            .select_from(IngestionJob)
            .where(
                IngestionJob.workspace_id.in_(workspace_ids),
                IngestionJob.status.in_(unfinished),
            )
        )
        # Each poll is its own read; ending the transaction lets the next one see the
        # worker's newer commits instead of the same snapshot.
        await session.rollback()
        if not left:
            return True
        print(f"  waiting for the worker: {left} job(s) left")
        await asyncio.sleep(2)
    return False


async def report(session: AsyncSession, workspace_ids: list[uuid.UUID]) -> None:
    for spec, workspace_id in zip(WORKSPACES, workspace_ids, strict=True):
        tickets = await session.scalar(
            select(func.count()).where(TicketRow.workspace_id == workspace_id)
        )
        rows = await session.execute(
            select(KnowledgeSource.status, func.count())
            .where(KnowledgeSource.workspace_id == workspace_id)
            .group_by(KnowledgeSource.status)
        )
        knowledge = ", ".join(f"{count} {status}" for status, count in rows.all())
        jobs = await session.execute(
            select(IngestionJob.status, func.count())
            .where(IngestionJob.workspace_id == workspace_id)
            .group_by(IngestionJob.status)
        )
        job_summary = ", ".join(f"{count} {status}" for status, count in jobs.all())
        print(f"\n{spec.name}  (slug {spec.slug}, id {workspace_id})")
        print(f"  tickets: {tickets}   knowledge: {knowledge}   jobs: {job_summary}")
        for person in spec.people:
            print(f"  {person.role:<7} {person.name:<16} {person.email}")
    print(f"\nEvery password: {PASSWORD}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--reset", action="store_true", help="delete the demo data first")
    parser.add_argument("--wait", action="store_true", help="wait for the worker to finish")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings)
    # Imported here, like the worker does, so importing this module never builds an engine.
    from app.db.session import SessionLocal, engine

    try:
        async with SessionLocal() as session:
            if await already_seeded(session):
                if not args.reset:
                    print("Demo data is already loaded. Use --reset to load it again.")
                    return
                await reset(session)
                print("Deleted the old demo data.")
            elif args.reset:
                # Users from a half-finished earlier run would make sign-up fail with 409.
                await reset(session)

            workspace_ids = [await seed_workspace(session, spec) for spec in WORKSPACES]
            print("Loaded the demo data.")

            if args.wait and not await wait_for_worker(session, workspace_ids):
                print("The worker did not finish. Is `make worker` running, and Ollama?")
            await report(session, workspace_ids)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
