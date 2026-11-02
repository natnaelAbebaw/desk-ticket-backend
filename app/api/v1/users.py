import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import rate_limit_default, rate_limit_writes, require_api_key
from app.core.cache import RECORD_TTL_SECONDS, get_text, set_text
from app.db.session import get_db
from app.schemas.user import UserCreate, UserRead
from app.services import users as user_service

router = APIRouter(dependencies=[Depends(require_api_key), Depends(rate_limit_default)])


@router.post(
    "",
    response_model=UserRead,
    status_code=201,
    dependencies=[Depends(rate_limit_writes)],
)
async def create_user(
    payload: UserCreate,
    session: AsyncSession = Depends(get_db),
) -> UserRead:
    user = await user_service.create_user(session, payload)
    return UserRead.model_validate(user)


@router.get("/{user_id}", response_model=UserRead)
async def get_user(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> UserRead:
    key = f"user:{user_id}"
    cached = await get_text(key)
    if cached is not None:
        return UserRead.model_validate_json(cached)
    user = await user_service.get_user(session, user_id)
    body = UserRead.model_validate(user)
    await set_text(key, body.model_dump_json(), RECORD_TTL_SECONDS)
    return body
