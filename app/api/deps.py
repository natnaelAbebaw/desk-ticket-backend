import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.redis import get_redis
from app.core.security import decode_access_token, unauthorized
from app.db.session import get_db
from app.models.user import User
from app.models.workspace import WorkspaceMember, WorkspaceRole

logger = logging.getLogger(__name__)

API_LIMIT_PER_MINUTE = 60
WRITE_LIMIT_PER_MINUTE = 10
_WINDOW_SECONDS = 60

_bearer = HTTPBearer(auto_error=False, description="Access token from POST /api/v1/auth/token")


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    if credentials is None:
        raise unauthorized("Missing bearer token")
    user_id = decode_access_token(credentials.credentials)
    user = await session.get(User, user_id)
    if user is None:
        raise unauthorized()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_membership(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> WorkspaceMember:
    member = await session.get(WorkspaceMember, (workspace_id, user.id))
    if member is None:
        # Same answer as a missing workspace, so outsiders cannot probe which ids exist.
        raise AppError("not_found", "Workspace not found", status_code=404)
    return member


Membership = Annotated[WorkspaceMember, Depends(get_membership)]


def require_role(*roles: WorkspaceRole) -> Callable[..., Awaitable[WorkspaceMember]]:
    allowed = {role.value for role in roles}

    async def dependency(member: Membership) -> WorkspaceMember:
        if member.role not in allowed:
            raise AppError("forbidden", "Your workspace role cannot do this", status_code=403)
        return member

    return dependency


def _identity(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            digest = hashlib.sha256(token.encode()).hexdigest()[:16]
            return f"key:{digest}"
    host = request.client.host if request.client is not None else "unknown"
    return f"ip:{host}"


async def enforce_rate_limit(request: Request, *, limit: int, scope: str) -> None:
    redis = get_redis()
    if redis is None:
        return
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    key = f"rl:{scope}:{_identity(request)}:{request.method}:{path}"
    try:
        count = int(await redis.incr(key))
        if count == 1:
            await redis.expire(key, _WINDOW_SECONDS)
    except RedisError:
        logger.warning("rate limit skipped; redis unavailable")
        return
    if count > limit:
        raise AppError("rate_limited", "Too many requests", status_code=429)


async def rate_limit_default(request: Request) -> None:
    await enforce_rate_limit(request, limit=API_LIMIT_PER_MINUTE, scope="api")


async def rate_limit_writes(request: Request) -> None:
    await enforce_rate_limit(request, limit=WRITE_LIMIT_PER_MINUTE, scope="write")
