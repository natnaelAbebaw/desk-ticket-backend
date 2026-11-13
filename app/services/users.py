import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import AppError
from app.models.user import User
from app.models.workspace import WorkspaceMember
from app.schemas.user import UserCreate
from app.services.passwords import DUMMY_HASH, hash_password, verify_password


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def create_user(session: AsyncSession, payload: UserCreate) -> User:
    email = normalize_email(payload.email)
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


async def authenticate(session: AsyncSession, email: str, password: str) -> User:
    user = await session.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None:
        verify_password(password, DUMMY_HASH)
        raise AppError("invalid_credentials", "Email or password is incorrect", status_code=401)
    if not verify_password(password, user.password_hash):
        raise AppError("invalid_credentials", "Email or password is incorrect", status_code=401)
    return user


async def get_visible_user(session: AsyncSession, viewer_id: uuid.UUID, user_id: uuid.UUID) -> User:
    """Return a user the viewer may see: themselves, or someone who shares a workspace."""
    if viewer_id != user_id:
        viewer = aliased(WorkspaceMember)
        target = aliased(WorkspaceMember)
        shared = await session.scalar(
            select(
                exists().where(
                    viewer.user_id == viewer_id,
                    target.user_id == user_id,
                    viewer.workspace_id == target.workspace_id,
                )
            )
        )
        if not shared:
            raise AppError("not_found", "User not found", status_code=404)
    return await get_user(session, user_id)
