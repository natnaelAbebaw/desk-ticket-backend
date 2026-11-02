import hashlib
import hmac
import logging
from typing import Annotated

from fastapi import Header, Request
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

API_LIMIT_PER_MINUTE = 60
WRITE_LIMIT_PER_MINUTE = 10
_WINDOW_SECONDS = 60


async def require_api_key(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    api_key = get_settings().api_key
    if not api_key:
        return
    provided = ""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not hmac.compare_digest(provided, api_key):
        raise AppError("unauthorized", "Invalid API key", status_code=401)


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
