import logging
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_client: Any | None = None


async def init_redis(url: str) -> None:
    global _client
    if not url:
        _client = None
        return
    _client = Redis.from_url(url, decode_responses=True)
    logger.info("redis configured")


def get_redis() -> Any | None:
    return _client


def set_redis(client: Any | None) -> None:
    global _client
    _client = client


async def close_redis() -> None:
    global _client
    client = _client
    _client = None
    aclose = getattr(client, "aclose", None)
    if aclose is not None:
        await aclose()
