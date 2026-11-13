import uuid
from datetime import timedelta

import jwt
from httpx import AsyncClient

from app.core.config import get_settings
from app.core.security import ALGORITHM
from app.models.timestamps import utcnow

from helpers import PASSWORD, signup


def _token(claims: dict, secret: str | None = None) -> dict[str, str]:
    token = jwt.encode(claims, secret or get_settings().secret_key, algorithm=ALGORITHM)
    return {"Authorization": f"Bearer {token}"}


async def test_login_returns_token_for_me(client: AsyncClient) -> None:
    ada = await signup(client, "Ada@Example.com", "Ada")
    me = await client.get("/api/v1/users/me", headers=ada.headers)
    assert me.status_code == 200
    assert me.json()["id"] == ada.id
    assert me.json()["email"] == "ada@example.com"


async def test_login_reports_expiry(client: AsyncClient) -> None:
    await signup(client, "ada@example.com")
    response = await client.post(
        "/api/v1/auth/token", json={"email": "ada@example.com", "password": PASSWORD}
    )
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == get_settings().access_token_ttl_minutes * 60


async def test_wrong_password_and_unknown_email_look_the_same(client: AsyncClient) -> None:
    await signup(client, "ada@example.com")
    wrong = await client.post(
        "/api/v1/auth/token", json={"email": "ada@example.com", "password": "wrong-password"}
    )
    unknown = await client.post(
        "/api/v1/auth/token", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["code"] == unknown.json()["error"]["code"]
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


async def test_missing_token_is_401(client: AsyncClient) -> None:
    response = await client.get("/api/v1/users/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_bad_tokens_are_401(client: AsyncClient) -> None:
    ada = await signup(client, "ada@example.com")
    now = utcnow()
    good = {"sub": ada.id, "type": "access", "iat": now, "exp": now + timedelta(minutes=5)}
    cases = {
        "garbage": {"Authorization": "Bearer not-a-jwt"},
        "expired": _token(
            {**good, "iat": now - timedelta(hours=2), "exp": now - timedelta(hours=1)}
        ),
        "wrong secret": _token(good, secret="another-secret-that-is-at-least-32-chars"),
        "wrong type": _token({**good, "type": "refresh"}),
        "no expiry": _token({k: v for k, v in good.items() if k != "exp"}),
        "unknown user": _token({**good, "sub": str(uuid.uuid4())}),
        "unsigned": {
            "Authorization": "Bearer "
            + jwt.encode(
                {**good, "exp": int((now + timedelta(minutes=5)).timestamp())},
                None,
                algorithm="none",
            )
        },
    }
    for name, headers in cases.items():
        response = await client.get("/api/v1/users/me", headers=headers)
        assert response.status_code == 401, name


async def test_signup_does_not_need_a_token(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/users",
        json={"email": "new@example.com", "name": "New", "password": PASSWORD},
    )
    assert response.status_code == 201
