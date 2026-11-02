from fastapi import APIRouter, Depends
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.redis import get_redis
from app.db.session import get_db
from app.schemas.common import Message

router = APIRouter()


@router.get("/health", response_model=Message, tags=["health"])
async def health() -> Message:
    return Message(status="ok")


@router.get("/ready", response_model=Message, tags=["health"])
async def ready(session: AsyncSession = Depends(get_db)) -> Message:
    await session.execute(text("SELECT 1"))
    redis = get_redis()
    if redis is not None:
        try:
            await redis.ping()
        except RedisError as exc:
            raise AppError("unavailable", "Redis is unavailable", status_code=503) from exc
    return Message(status="ok")
