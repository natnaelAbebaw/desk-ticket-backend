"""Knowledge base: review flow, permissions, duplicates, and tenant isolation.

Most tests drive the HTTP API like a real client. test_database_blocks_live_duplicates
writes rows directly to check the database constraint on its own.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.models.knowledge import KnowledgeSource
from app.services.knowledge import content_hash

from helpers import Account, add_member, create_ticket, create_workspace, signup

ARTICLE = "# Resetting your password\n\nOpen **Settings → Security** and choose *Reset*."


class Team:
    """One workspace with an owner, an agent, and a plain member, plus URL helpers."""

    def __init__(self, workspace_id: str, owner: Account, agent: Account, member: Account):
        self.workspace_id = workspace_id
        self.owner = owner
        self.agent = agent
        self.member = member

    @property
    def knowledge(self) -> str:
        return f"/api/v1/workspaces/{self.workspace_id}/knowledge"

    def ticket_knowledge(self, ticket_id: str) -> str:
        return f"/api/v1/workspaces/{self.workspace_id}/tickets/{ticket_id}/knowledge"


async def _team(client: AsyncClient) -> Team:
    owner = await signup(client, "owner@acme.test", "Owner")
    agent = await signup(client, "agent@acme.test", "Agent")
    member = await signup(client, "member@acme.test", "Member")
    workspace_id = await create_workspace(client, owner)
    await add_member(client, workspace_id, owner, agent, role="agent")
    await add_member(client, workspace_id, owner, member, role="member")
    return Team(workspace_id, owner, agent, member)


async def _resolved_ticket(client: AsyncClient, team: Team) -> str:
    ticket_id = await create_ticket(client, team.workspace_id, team.member, "Can't log in")
    resolved = await client.patch(
        f"/api/v1/workspaces/{team.workspace_id}/tickets/{ticket_id}",
        json={"status": "resolved"},
        headers=team.agent.headers,
    )
    assert resolved.status_code == 200
    return ticket_id


def _answer(body: str = "Use Settings → Security → Reset password.") -> dict:
    return {"title": "Password reset", "question": "How do I reset my password?", "body": body}


async def _doc(client: AsyncClient, team: Team, author: Account, body: str = ARTICLE) -> dict:
    response = await client.post(
        team.knowledge, json={"title": "Password reset", "body": body}, headers=author.headers
    )
    assert response.status_code == 201, response.text
    return response.json()


# Documents


async def test_owner_document_is_published_approved(client: AsyncClient) -> None:
    team = await _team(client)
    doc = await _doc(client, team, team.owner)
    assert doc["kind"] == "document"
    assert doc["status"] == "approved"
    assert doc["reviewed_by"] == team.owner.id
    assert doc["created_by"] == team.owner.id
    assert doc["question"] is None
    assert doc["version"] == 1


async def test_agent_document_waits_for_review(client: AsyncClient) -> None:
    team = await _team(client)
    doc = await _doc(client, team, team.agent)
    assert doc["status"] == "pending_review"
    assert doc["reviewed_by"] is None

    by_agent = await client.post(
        f"{team.knowledge}/{doc['id']}/approve", headers=team.agent.headers
    )
    assert by_agent.status_code == 403

    approved = await client.post(
        f"{team.knowledge}/{doc['id']}/approve", headers=team.owner.headers
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["reviewed_by"] == team.owner.id


async def test_plain_members_cannot_see_or_add_knowledge(client: AsyncClient) -> None:
    team = await _team(client)
    doc = await _doc(client, team, team.owner)
    for method, url, body in [
        ("GET", team.knowledge, None),
        ("GET", f"{team.knowledge}/{doc['id']}", None),
        ("POST", team.knowledge, {"title": "T", "body": "B"}),
    ]:
        response = await client.request(method, url, json=body, headers=team.member.headers)
        assert response.status_code == 403, f"{method} {url}"


# Ticket answers


async def test_ticket_must_be_resolved_before_it_becomes_knowledge(client: AsyncClient) -> None:
    team = await _team(client)
    open_ticket = await create_ticket(client, team.workspace_id, team.member)
    response = await client.post(
        team.ticket_knowledge(open_ticket), json=_answer(), headers=team.agent.headers
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ticket_not_resolved"


async def test_ticket_answers_need_review_even_from_owners(client: AsyncClient) -> None:
    team = await _team(client)
    ticket_id = await _resolved_ticket(client, team)
    response = await client.post(
        team.ticket_knowledge(ticket_id), json=_answer(), headers=team.owner.headers
    )
    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == "ticket_answer"
    assert body["status"] == "pending_review"
    assert body["source_ticket_id"] == ticket_id
    assert body["question"] == "How do I reset my password?"


async def test_one_live_proposal_per_ticket(client: AsyncClient) -> None:
    team = await _team(client)
    ticket_id = await _resolved_ticket(client, team)
    url = team.ticket_knowledge(ticket_id)
    first = await client.post(url, json=_answer(), headers=team.agent.headers)
    assert first.status_code == 201

    second = await client.post(url, json=_answer("Different words."), headers=team.agent.headers)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "already_proposed"

    rejected = await client.post(
        f"{team.knowledge}/{first.json()['id']}/reject",
        json={"note": "Contains the customer's email"},
        headers=team.owner.headers,
    )
    assert rejected.status_code == 200
    assert rejected.json()["review_note"] == "Contains the customer's email"

    retry = await client.post(url, json=_answer("Different words."), headers=team.agent.headers)
    assert retry.status_code == 201


async def test_ticket_from_another_workspace_is_404(client: AsyncClient) -> None:
    team = await _team(client)
    other_owner = await signup(client, "other@globex.test", "Other")
    globex = await create_workspace(client, other_owner, slug="globex")
    globex_ticket = await create_ticket(client, globex, other_owner)
    response = await client.post(
        team.ticket_knowledge(globex_ticket), json=_answer(), headers=team.owner.headers
    )
    assert response.status_code == 404


# Duplicates


async def test_duplicate_content_is_rejected_until_archived(client: AsyncClient) -> None:
    team = await _team(client)
    original = await _doc(client, team, team.owner)

    # Same words with different spacing and a different title still count as a duplicate.
    spaced = ARTICLE.replace(" ", "   ").replace("\n\n", "\n \n")
    duplicate = await client.post(
        team.knowledge, json={"title": "Another title", "body": spaced}, headers=team.agent.headers
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "duplicate_knowledge"
    assert original["id"] in duplicate.json()["error"]["message"]

    archived = await client.post(
        f"{team.knowledge}/{original['id']}/archive", headers=team.owner.headers
    )
    assert archived.json()["status"] == "archived"
    again = await client.post(
        team.knowledge,
        json={"title": "Password reset", "body": ARTICLE},
        headers=team.owner.headers,
    )
    assert again.status_code == 201


async def test_database_blocks_live_duplicates(engine: AsyncEngine, client: AsyncClient) -> None:
    """The partial unique indexes back up the service checks when two requests race."""
    team = await _team(client)
    workspace_id = uuid.UUID(team.workspace_id)
    digest = content_hash("document", None, "Same text")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def row(status: str) -> KnowledgeSource:
        return KnowledgeSource(
            workspace_id=workspace_id,
            kind="document",
            status=status,
            title="T",
            body="Same text",
            content_hash=digest,
            version=1,
            created_by=uuid.UUID(team.owner.id),
        )

    # Rejected and archived rows sit outside the partial index, so these three share a
    # hash without conflict. Only the approved one counts as live.
    async with factory() as session:
        session.add_all([row("rejected"), row("archived"), row("approved")])
        await session.commit()

    # A second live row with the same hash bypasses the service, so only the index can stop it.

    async with factory() as session:
        session.add(row("pending_review"))
        with pytest.raises(IntegrityError, match="uq_knowledge_sources_live_content"):
            await session.commit()


# Editing


async def test_agent_edits_own_rejected_draft_back_into_review(client: AsyncClient) -> None:
    team = await _team(client)
    doc = await _doc(client, team, team.agent)
    url = f"{team.knowledge}/{doc['id']}"
    await client.post(f"{url}/reject", json={"note": "Too vague"}, headers=team.owner.headers)

    edited = await client.patch(url, json={"body": "Clearer steps."}, headers=team.agent.headers)
    assert edited.status_code == 200
    body = edited.json()
    assert body["status"] == "pending_review"
    assert body["version"] == 2
    assert body["review_note"] is None
    assert body["reviewed_by"] is None


async def test_agents_cannot_edit_approved_or_others_knowledge(client: AsyncClient) -> None:
    team = await _team(client)
    approved = await _doc(client, team, team.owner)
    other_agent = await signup(client, "agent2@acme.test", "Agent Two")
    await add_member(client, team.workspace_id, team.owner, other_agent, role="agent")
    draft = await _doc(client, team, other_agent, body="Draft from agent two")

    for source in (approved, draft):
        response = await client.patch(
            f"{team.knowledge}/{source['id']}", json={"title": "Mine"}, headers=team.agent.headers
        )
        assert response.status_code == 403


async def test_owner_edit_keeps_approved_and_bumps_version(client: AsyncClient) -> None:
    team = await _team(client)
    doc = await _doc(client, team, team.agent)
    url = f"{team.knowledge}/{doc['id']}"
    await client.post(f"{url}/approve", headers=team.owner.headers)

    edited = await client.patch(url, json={"body": "Updated steps."}, headers=team.owner.headers)
    assert edited.status_code == 200
    assert edited.json()["status"] == "approved"
    assert edited.json()["version"] == 2
    assert edited.json()["reviewed_by"] == team.owner.id

    same = await client.patch(url, json={"body": "Updated steps."}, headers=team.owner.headers)
    assert same.status_code == 400
    assert same.json()["error"]["code"] == "empty_update"


async def test_documents_have_no_question(client: AsyncClient) -> None:
    team = await _team(client)
    doc = await _doc(client, team, team.owner)
    response = await client.patch(
        f"{team.knowledge}/{doc['id']}", json={"question": "Why?"}, headers=team.owner.headers
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_field"


async def test_status_rules(client: AsyncClient) -> None:
    team = await _team(client)
    doc = await _doc(client, team, team.owner)
    url = f"{team.knowledge}/{doc['id']}"

    approve_again = await client.post(f"{url}/approve", headers=team.owner.headers)
    assert approve_again.status_code == 409

    await client.post(f"{url}/archive", headers=team.owner.headers)
    edit_archived = await client.patch(url, json={"title": "New"}, headers=team.owner.headers)
    assert edit_archived.status_code == 409
    archive_again = await client.post(f"{url}/archive", headers=team.owner.headers)
    assert archive_again.status_code == 409


# Listing and isolation


async def test_list_filters_and_leaves_out_bodies(client: AsyncClient) -> None:
    team = await _team(client)
    await _doc(client, team, team.owner)
    pending = await _doc(client, team, team.agent, body="Pending article")

    response = await client.get(
        team.knowledge, params={"status": "pending_review"}, headers=team.agent.headers
    )
    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 1
    assert page["items"][0]["id"] == pending["id"]
    assert "body" not in page["items"][0]


async def test_outsiders_get_404_on_knowledge(client: AsyncClient) -> None:
    team = await _team(client)
    doc = await _doc(client, team, team.owner)
    outsider = await signup(client, "outsider@globex.test", "Outsider")
    await create_workspace(client, outsider, slug="globex")
    ticket_id = await _resolved_ticket(client, team)
    url = f"{team.knowledge}/{doc['id']}"
    for method, target, body in [
        ("GET", team.knowledge, None),
        ("POST", team.knowledge, {"title": "T", "body": "B"}),
        ("GET", url, None),
        ("PATCH", url, {"title": "Hacked"}),
        ("POST", f"{url}/approve", None),
        ("POST", f"{url}/reject", {"note": "no"}),
        ("POST", f"{url}/archive", None),
        ("POST", team.ticket_knowledge(ticket_id), _answer()),
    ]:
        response = await client.request(method, target, json=body, headers=outsider.headers)
        assert response.status_code == 404, f"{method} {target}"


async def test_source_id_from_another_workspace_is_404(client: AsyncClient) -> None:
    team = await _team(client)
    doc = await _doc(client, team, team.owner)
    other = await signup(client, "other@globex.test", "Other")
    globex = await create_workspace(client, other, slug="globex")
    response = await client.get(
        f"/api/v1/workspaces/{globex}/knowledge/{doc['id']}", headers=other.headers
    )
    assert response.status_code == 404
