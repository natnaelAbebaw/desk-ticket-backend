import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.user import User
from app.schemas.user import UserCreate
from app.services.passwords import hash_password


async def create_user(session: AsyncSession, payload: UserCreate) -> User:
    email = payload.email.strip().lower()
    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise AppError("email_taken", "Email is already registered", status_code=409)
    user = User(
        email=email,
        name=payload.name.strip(),
        password_hash=hash_password(payload.password),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError("not_found", "User not found", status_code=404)
    return user
