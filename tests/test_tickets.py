import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.models.ticket import Ticket


async def _user(client: AsyncClient, email: str, name: str) -> str:
    response = await client.post(
        "/api/v1/users",
        json={"email": email, "name": name, "password": "password123"},
    )
    assert response.status_code == 201
    return response.json()["id"]


async def _workspace(client: AsyncClient, owner_id: str, slug: str = "acme") -> str:
    response = await client.post(
        "/api/v1/workspaces",
        json={"name": "Acme", "slug": slug, "owner_id": owner_id},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["slug"] == slug
    return body["id"]


def test_ticket_workspace_created_index() -> None:
    indexes = {
        index.name: [column.name for column in index.columns] for index in Ticket.__table__.indexes
    }
    assert indexes["ix_tickets_workspace_created_at"] == ["workspace_id", "created_at"]


async def test_ticket_crud(client: AsyncClient) -> None:
    owner_id = await _user(client, "ada@example.com", "Ada")
    agent_id = await _user(client, "grace@example.com", "Grace")
    workspace_id = await _workspace(client, owner_id)

    member = await client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"user_id": agent_id, "role": "agent"},
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
            "created_by": owner_id,
            "assigned_to": agent_id,
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["subject"] == "Printer jam"
    assert body["status"] == "open"
    assert body["priority"] == "urgent"
    assert body["category"] == "technical"
    assert body["assigned_to"] == agent_id
    assert body["closed_at"] is None
    ticket_id = body["id"]

    listed = await client.get(
        f"/api/v1/workspaces/{workspace_id}/tickets",
        params={"priority": "urgent", "category": "technical"},
    )
    assert listed.status_code == 200
    page = listed.json()
    assert page["total"] == 1
    assert page["items"][0]["id"] == ticket_id

    fetched = await client.get(f"/api/v1/workspaces/{workspace_id}/tickets/{ticket_id}")
    assert fetched.status_code == 200

    updated = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/tickets/{ticket_id}",
        json={"status": "closed", "actor_id": agent_id},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "closed"
    assert updated.json()["closed_at"] is not None

    reopened = await client.patch(
        f"/api/v1/workspaces/{workspace_id}/tickets/{ticket_id}",
        json={"status": "open", "actor_id": owner_id},
    )
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "open"
    assert reopened.json()["closed_at"] is None

    message = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tickets/{ticket_id}/messages",
        json={"author_id": agent_id, "body": "Looking at the printer.", "message_type": "reply"},
    )
    assert message.status_code == 201
    assert message.json()["message_type"] == "reply"

    note = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tickets/{ticket_id}/messages",
        json={
            "author_id": agent_id,
            "body": "Needs a new roller.",
            "message_type": "internal_note",
        },
    )
    assert note.status_code == 201

    events = await client.get(f"/api/v1/workspaces/{workspace_id}/tickets/{ticket_id}/events")
    assert events.status_code == 200
    items = events.json()["items"]
    event_types = [item["event_type"] for item in items]
    assert event_types[0] == "created"
    assert "closed" in event_types
    assert "reopened" in event_types
    added = [item for item in items if item["event_type"] == "message_added"]
    assert {item["metadata"]["message_type"] for item in added} == {"reply", "internal_note"}


async def test_outsider_cannot_open_ticket(client: AsyncClient) -> None:
    owner_id = await _user(client, "owner@example.com", "Owner")
    outsider_id = await _user(client, "outsider@example.com", "Outsider")
    workspace_id = await _workspace(client, owner_id, slug="acme-2")
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tickets",
        json={"subject": "Nope", "created_by": outsider_id},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_a_member"


async def test_missing_ticket(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/workspaces/00000000-0000-0000-0000-000000000000/tickets/"
        "00000000-0000-0000-0000-000000000000"
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_validation_error(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/users",
        json={"email": "ada@example.com", "name": "", "password": "short"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_api_key_required(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEY", "test-key-value-123")
    get_settings.cache_clear()
    payload = {"email": "locked@example.com", "name": "Locked", "password": "password123"}
    try:
        denied = await client.post("/api/v1/users", json=payload)
        assert denied.status_code == 401

        allowed = await client.post(
            "/api/v1/users",
            json=payload,
            headers={"Authorization": "Bearer test-key-value-123"},
        )
        assert allowed.status_code == 201
    finally:
        get_settings.cache_clear()


def test_production_settings_reject_weak_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "change-me")
    monkeypatch.setenv("API_KEY", "a" * 16)
    with pytest.raises(ValidationError):
        Settings()
