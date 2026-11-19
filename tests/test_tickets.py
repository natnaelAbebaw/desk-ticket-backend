import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.core.config import Settings
from app.models.ticket import Ticket

from helpers import create_workspace, signup


def test_ticket_workspace_created_index() -> None:
    indexes = {
        index.name: [column.name for column in index.columns] for index in Ticket.__table__.indexes
    }
    assert indexes["ix_tickets_workspace_created_at"] == ["workspace_id", "created_at"]


async def test_ticket_crud(client: AsyncClient) -> None:
    owner = await signup(client, "ada@example.com", "Ada")
    agent = await signup(client, "grace@example.com", "Grace")
    workspace_id = await create_workspace(client, owner)
    owner_id, agent_id = owner.id, agent.id

    member = await client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"user_id": agent_id, "role": "agent"},
        headers=owner.headers,
    )
    assert member.status_code == 201
    assert member.json()["role"] == "agent"

    created = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tickets",
        json={
            "subject": "Printer jam",
            "description": "Floor 3",
            "priority": "urgent",
            "category": "technical",
            "assigned_to": agent_id,
        },
        headers=owner.headers,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["subject"] == "Printer jam"
    assert body["status"] == "open"
    assert body["priority"] == "urgent"
    assert body["category"] == "technical"
    assert body["assigned_to"] == agent_id
    assert body["created_by"] == owner_id
    assert body["closed_at"] is None
    ticket_id = body["id"]

    listed = await client.get(
        f"/api/v1/workspaces/{workspace_id}/tickets",
        params={"priority": "urgent", "category": "technical"},
        headers=agent.headers,
    )
    assert listed.status_code == 200
    page = listed.json()
    assert page["total"] == 1
    assert page["items"][0]["id"] == ticket_id

    fetched = await client.get(
        f"/api/v1/workspaces/{workspace_id}/tickets/{ticket_id}", headers=agent.headers
    )
    assert fetched.status_code == 200

    updated = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/tickets/{ticket_id}",
        json={"status": "closed"},
        headers=agent.headers,
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "closed"
    assert updated.json()["closed_at"] is not None

    reopened = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/tickets/{ticket_id}",
        json={"status": "open"},
        headers=owner.headers,
    )
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "open"
    assert reopened.json()["closed_at"] is None

    message = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tickets/{ticket_id}/messages",
        json={"body": "Looking at the printer.", "message_type": "reply"},
        headers=agent.headers,
    )
    assert message.status_code == 201
    assert message.json()["message_type"] == "reply"
    assert message.json()["author_id"] == agent_id

    note = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tickets/{ticket_id}/messages",
        json={"body": "Needs a new roller.", "message_type": "internal_note"},
        headers=agent.headers,
    )
    assert note.status_code == 201

    events = await client.get(
        f"/api/v1/workspaces/{workspace_id}/tickets/{ticket_id}/events", headers=owner.headers
    )
    assert events.status_code == 200
    items = events.json()["items"]
    event_types = [item["event_type"] for item in items]
    assert event_types[0] == "created"
    assert items[0]["actor_id"] == owner_id
    assert "closed" in event_types
    assert "reopened" in event_types
    added = [item for item in items if item["event_type"] == "message_added"]
    assert {item["metadata"]["message_type"] for item in added} == {"reply", "internal_note"}


async def test_missing_ticket(client: AsyncClient) -> None:
    owner = await signup(client, "owner@example.com", "Owner")
    workspace_id = await create_workspace(client, owner)
    response = await client.get(
        f"/api/v1/workspaces/{workspace_id}/tickets/00000000-0000-0000-0000-000000000000",
        headers=owner.headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_assignee_must_be_member(client: AsyncClient) -> None:
    owner = await signup(client, "owner@example.com", "Owner")
    outsider = await signup(client, "outsider@example.com", "Outsider")
    workspace_id = await create_workspace(client, owner)
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tickets",
        json={"subject": "Nope", "assigned_to": outsider.id},
        headers=owner.headers,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_a_member"


async def test_validation_error(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/users",
        json={"email": "ada@example.com", "name": "", "password": "short"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_production_settings_reject_weak_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "change-me")
    with pytest.raises(ValidationError):
        Settings()
