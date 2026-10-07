from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.common import Message

router = APIRouter()


@router.get("/health", response_model=Message, tags=["health"])
async def health() -> Message:
    return Message(status="ok")


@router.get("/ready", response_model=Message, tags=["health"])
async def ready(session: AsyncSession = Depends(get_db)) -> Message:
    await session.execute(text("SELECT 1"))
    return Message(status="ok")
