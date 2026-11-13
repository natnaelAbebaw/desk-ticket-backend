from dataclasses import dataclass

from httpx import AsyncClient

PASSWORD = "password123"


@dataclass
class Account:
    id: str
    email: str
    headers: dict[str, str]


async def signup(client: AsyncClient, email: str, name: str = "User") -> Account:
    created = await client.post(
        "/api/v1/users",
        json={"email": email, "name": name, "password": PASSWORD},
    )
    assert created.status_code == 201, created.text
    token = await client.post(
        "/api/v1/auth/token",
        json={"email": email, "password": PASSWORD},
    )
    assert token.status_code == 200, token.text
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    return Account(id=created.json()["id"], email=email, headers=headers)


async def create_workspace(client: AsyncClient, owner: Account, slug: str = "acme") -> str:
    response = await client.post(
        "/api/v1/workspaces",
        json={"name": slug.title(), "slug": slug},
        headers=owner.headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["slug"] == slug
    return response.json()["id"]


async def add_member(
    client: AsyncClient,
    workspace_id: str,
    manager: Account,
    member: Account,
    role: str = "agent",
) -> None:
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"user_id": member.id, "role": role},
        headers=manager.headers,
    )
    assert response.status_code == 201, response.text


async def create_ticket(
    client: AsyncClient, workspace_id: str, author: Account, subject: str = "Help"
) -> str:
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/tickets",
        json={"subject": subject},
        headers=author.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]
