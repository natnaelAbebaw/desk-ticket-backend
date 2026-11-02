import json

from httpx import AsyncClient
from redis.exceptions import RedisError

from app.core.redis import set_redis


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.values[key] = value
        return True

    async def incr(self, key: str) -> int:
        current = int(self.values.get(key, "0")) + 1
        self.values[key] = str(current)
        return current

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def ping(self) -> bool:
        return True


class DownRedis(FakeRedis):
    async def ping(self) -> bool:
        raise RedisError("down")


async def test_write_rate_limit(client: AsyncClient) -> None:
    set_redis(FakeRedis())
    try:
        statuses = []
        for index in range(11):
            response = await client.post(
                "/api/v1/users",
                json={
                    "email": f"user{index}@example.com",
                    "name": "User",
                    "password": "password123",
                },
            )
            statuses.append(response.status_code)
        assert statuses[:10] == [201] * 10
        assert statuses[10] == 429
        assert response.json()["error"]["code"] == "rate_limited"
    finally:
        set_redis(None)


async def test_user_cache_serves_stored_record(client: AsyncClient) -> None:
    fake = FakeRedis()
    set_redis(fake)
    try:
        created = await client.post(
            "/api/v1/users",
            json={"email": "ada@example.com", "name": "Ada", "password": "password123"},
        )
        assert created.status_code == 201
        user_id = created.json()["id"]
        fetched = await client.get(f"/api/v1/users/{user_id}")
        assert fetched.status_code == 200
        key = f"user:{user_id}"
        stored = json.loads(fake.values[key])
        stored["name"] = "Cached Ada"
        fake.values[key] = json.dumps(stored)
        cached = await client.get(f"/api/v1/users/{user_id}")
        assert cached.status_code == 200
        assert cached.json()["name"] == "Cached Ada"
    finally:
        set_redis(None)


async def test_ticket_list_cache_refreshes_after_create(client: AsyncClient) -> None:
    set_redis(FakeRedis())
    try:
        created_user = await client.post(
            "/api/v1/users",
            json={"email": "ada@example.com", "name": "Ada", "password": "password123"},
        )
        owner_id = created_user.json()["id"]
        created_workspace = await client.post(
            "/api/v1/workspaces",
            json={"name": "Acme", "slug": "acme", "owner_id": owner_id},
        )
        workspace_id = created_workspace.json()["id"]
        ticket_url = f"/api/v1/workspaces/{workspace_id}/tickets"

        first = await client.post(
            ticket_url,
            json={"subject": "One", "created_by": owner_id},
        )
        assert first.status_code == 201
        listed = await client.get(ticket_url)
        assert listed.json()["total"] == 1

        second = await client.post(
            ticket_url,
            json={"subject": "Two", "created_by": owner_id},
        )
        assert second.status_code == 201
        listed_again = await client.get(ticket_url)
        assert listed_again.status_code == 200
        assert listed_again.json()["total"] == 2
    finally:
        set_redis(None)


async def test_ready_pings_redis(client: AsyncClient) -> None:
    set_redis(FakeRedis())
    try:
        response = await client.get("/ready")
        assert response.status_code == 200
    finally:
        set_redis(None)


async def test_ready_fails_when_redis_is_down(client: AsyncClient) -> None:
    set_redis(DownRedis())
    try:
        response = await client.get("/ready")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "unavailable"
    finally:
        set_redis(None)
