import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_api_key
from app.db.session import get_db
from app.schemas.user import UserCreate, UserRead
from app.services import users as user_service

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("", response_model=UserRead, status_code=201)
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
    user = await user_service.get_user(session, user_id)
    return UserRead.model_validate(user)
