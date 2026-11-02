import logging
import uuid

from redis.exceptions import RedisError

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

RECORD_TTL_SECONDS = 60
TICKET_LIST_TTL_SECONDS = 30


async def get_text(key: str) -> str | None:
    redis = get_redis()
    if redis is None:
        return None
    try:
        value = await redis.get(key)
    except RedisError:
        logger.warning("redis read failed for %s", key)
        return None
    if value is None:
        return None
    return str(value)


async def set_text(key: str, value: str, ttl_seconds: int) -> None:
    redis = get_redis()
    if redis is None:
        return
    try:
        await redis.set(key, value, ex=ttl_seconds)
    except RedisError:
        logger.warning("redis write failed for %s", key)


async def ticket_list_version(workspace_id: uuid.UUID) -> str:
    return await get_text(f"tickets:ver:{workspace_id}") or "0"


async def bump_ticket_list_version(workspace_id: uuid.UUID) -> None:
    redis = get_redis()
    if redis is None:
        return
    try:
        await redis.incr(f"tickets:ver:{workspace_id}")
    except RedisError:
        logger.warning("redis version bump failed for workspace %s", workspace_id)
