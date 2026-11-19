"""A user must never read or change another workspace's data.

Outsiders get 404 rather than 403, so they cannot tell whether a workspace or ticket exists.
"""

from httpx import AsyncClient

from helpers import Account, add_member, create_ticket, create_workspace, signup


async def _two_tenants(client: AsyncClient) -> tuple[Account, str, str, Account, str]:
    alice = await signup(client, "alice@acme.test", "Alice")
    bob = await signup(client, "bob@globex.test", "Bob")
    acme = await create_workspace(client, alice, slug="acme")
    globex = await create_workspace(client, bob, slug="globex")
    acme_ticket = await create_ticket(client, acme, alice, subject="Acme secret")
    return alice, acme, acme_ticket, bob, globex


async def test_outsider_gets_404_on_every_workspace_route(client: AsyncClient) -> None:
    _, acme, ticket, bob, _ = await _two_tenants(client)
    base = f"/api/v1/workspaces/{acme}"
    requests = [
        ("GET", base, None),
        ("GET", f"{base}/members", None),
        ("POST", f"{base}/members", {"user_id": bob.id, "role": "owner"}),
        ("GET", f"{base}/tickets", None),
        ("POST", f"{base}/tickets", {"subject": "Injected"}),
        ("GET", f"{base}/tickets/{ticket}", None),
        ("PATCH", f"{base}/tickets/{ticket}", {"status": "closed"}),
        ("GET", f"{base}/tickets/{ticket}/messages", None),
        ("POST", f"{base}/tickets/{ticket}/messages", {"body": "Injected"}),
        ("GET", f"{base}/tickets/{ticket}/events", None),
    ]
    for method, url, body in requests:
        response = await client.request(method, url, json=body, headers=bob.headers)
        assert response.status_code == 404, f"{method} {url} -> {response.status_code}"
        assert response.json()["error"]["code"] == "not_found"


async def test_outsider_response_matches_missing_workspace(client: AsyncClient) -> None:
    _, acme, _, bob, _ = await _two_tenants(client)
    real = await client.get(f"/api/v1/workspaces/{acme}/tickets", headers=bob.headers)
    fake = await client.get(
        "/api/v1/workspaces/00000000-0000-0000-0000-000000000000/tickets",
        headers=bob.headers,
    )
    assert real.status_code == fake.status_code == 404
    assert real.json()["error"]["message"] == fake.json()["error"]["message"]


async def test_ticket_id_from_another_workspace_is_404(client: AsyncClient) -> None:
    _, _, acme_ticket, bob, globex = await _two_tenants(client)
    response = await client.get(
        f"/api/v1/workspaces/{globex}/tickets/{acme_ticket}", headers=bob.headers
    )
    assert response.status_code == 404


async def test_outsider_changes_are_not_applied(client: AsyncClient) -> None:
    alice, acme, ticket, bob, _ = await _two_tenants(client)
    await client.patch(
        f"/api/v1/workspaces/{acme}/tickets/{ticket}",
        json={"status": "closed"},
        headers=bob.headers,
    )
    fetched = await client.get(f"/api/v1/workspaces/{acme}/tickets/{ticket}", headers=alice.headers)
    assert fetched.json()["status"] == "open"
    members = await client.get(f"/api/v1/workspaces/{acme}/members", headers=alice.headers)
    assert [m["user_id"] for m in members.json()["items"]] == [alice.id]


async def test_workspace_list_shows_only_my_workspaces(client: AsyncClient) -> None:
    _, _, _, bob, globex = await _two_tenants(client)
    response = await client.get("/api/v1/workspaces", headers=bob.headers)
    assert response.status_code == 200
    assert [w["id"] for w in response.json()["items"]] == [globex]


async def test_user_profiles_are_visible_only_to_coworkers(client: AsyncClient) -> None:
    alice, acme, _, bob, _ = await _two_tenants(client)
    hidden = await client.get(f"/api/v1/users/{alice.id}", headers=bob.headers)
    assert hidden.status_code == 404

    carol = await signup(client, "carol@acme.test", "Carol")
    await add_member(client, acme, alice, carol)
    visible = await client.get(f"/api/v1/users/{alice.id}", headers=carol.headers)
    assert visible.status_code == 200
    assert visible.json()["email"] == "alice@acme.test"


async def test_identity_comes_from_token_not_body(client: AsyncClient) -> None:
    alice = await signup(client, "alice@acme.test", "Alice")
    mallory = await signup(client, "mallory@acme.test", "Mallory")
    acme = await create_workspace(client, alice)
    await add_member(client, acme, alice, mallory)

    ticket = await client.post(
        f"/api/v1/workspaces/{acme}/tickets",
        json={"subject": "Spoof", "created_by": alice.id},
        headers=mallory.headers,
    )
    assert ticket.status_code == 201
    assert ticket.json()["created_by"] == mallory.id

    message = await client.post(
        f"/api/v1/workspaces/{acme}/tickets/{ticket.json()['id']}/messages",
        json={"body": "Spoof", "author_id": alice.id},
        headers=mallory.headers,
    )
    assert message.status_code == 201
    assert message.json()["author_id"] == mallory.id


async def test_callers_cannot_post_system_messages(client: AsyncClient) -> None:
    alice = await signup(client, "alice@acme.test", "Alice")
    acme = await create_workspace(client, alice)
    ticket = await create_ticket(client, acme, alice)
    response = await client.post(
        f"/api/v1/workspaces/{acme}/tickets/{ticket}/messages",
        json={"body": "Ticket escalated", "message_type": "system"},
        headers=alice.headers,
    )
    assert response.status_code == 422


async def test_only_owners_and_admins_manage_members(client: AsyncClient) -> None:
    alice = await signup(client, "alice@acme.test", "Alice")
    agent = await signup(client, "agent@acme.test", "Agent")
    admin = await signup(client, "admin@acme.test", "Admin")
    newcomer = await signup(client, "new@acme.test", "New")
    acme = await create_workspace(client, alice)
    await add_member(client, acme, alice, agent, role="agent")
    await add_member(client, acme, alice, admin, role="admin")
    url = f"/api/v1/workspaces/{acme}/members"

    by_agent = await client.post(
        url, json={"user_id": newcomer.id, "role": "member"}, headers=agent.headers
    )
    assert by_agent.status_code == 403
    assert by_agent.json()["error"]["code"] == "forbidden"

    admin_promotes = await client.post(
        url, json={"user_id": newcomer.id, "role": "owner"}, headers=admin.headers
    )
    assert admin_promotes.status_code == 403

    admin_adds_agent = await client.post(
        url, json={"user_id": newcomer.id, "role": "agent"}, headers=admin.headers
    )
    assert admin_adds_agent.status_code == 201
