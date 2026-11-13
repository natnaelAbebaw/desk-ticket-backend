import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from app.schemas.workspace import MemberCreate, WorkspaceCreate


async def create_workspace(
    session: AsyncSession, payload: WorkspaceCreate, *, owner_id: uuid.UUID
) -> Workspace:
    slug = payload.slug.strip().lower()
    existing = await session.scalar(select(Workspace).where(Workspace.slug == slug))
    if existing is not None:
        raise AppError("slug_taken", "Workspace slug is already in use", status_code=409)
    workspace = Workspace(name=payload.name.strip(), slug=slug)
    session.add(workspace)
    await session.flush()
    session.add(
        WorkspaceMember(
            workspace_id=workspace.id,
            user_id=owner_id,
            role=WorkspaceRole.owner.value,
        )
    )
    await session.commit()
    await session.refresh(workspace)
    return workspace


async def get_workspace(session: AsyncSession, workspace_id: uuid.UUID) -> Workspace:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise AppError("not_found", "Workspace not found", status_code=404)
    return workspace


_PRIVILEGED_ROLES = {WorkspaceRole.owner.value, WorkspaceRole.admin.value}


async def add_member(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    payload: MemberCreate,
    *,
    actor_role: str,
) -> WorkspaceMember:
    await get_workspace(session, workspace_id)
    if payload.role.value in _PRIVILEGED_ROLES and actor_role != WorkspaceRole.owner.value:
        raise AppError(
            "forbidden",
            "Only an owner can add owners or admins",
            status_code=403,
        )
    user = await session.get(User, payload.user_id)
    if user is None:
        raise AppError("not_found", "User not found", status_code=404)
    existing = await session.get(WorkspaceMember, (workspace_id, payload.user_id))
    if existing is not None:
        raise AppError(
            "already_member",
            "User is already a member of this workspace",
            status_code=409,
        )
    member = WorkspaceMember(
        workspace_id=workspace_id,
        user_id=payload.user_id,
        role=payload.role.value,
    )
    session.add(member)
    await session.commit()
    await session.refresh(member)
    return member


async def list_members(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[WorkspaceMember], int]:
    await get_workspace(session, workspace_id)
    filters = WorkspaceMember.workspace_id == workspace_id
    total = await session.scalar(select(func.count()).select_from(WorkspaceMember).where(filters))
    result = await session.scalars(
        select(WorkspaceMember)
        .where(filters)
        .order_by(WorkspaceMember.joined_at.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.all()), int(total or 0)


async def require_member(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> WorkspaceMember:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError("not_found", "User not found", status_code=404)
    member = await session.get(WorkspaceMember, (workspace_id, user_id))
    if member is None:
        raise AppError("not_a_member", "User is not a member of this workspace", status_code=403)
    return member


async def list_user_workspaces(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[Workspace], int]:
    filters = WorkspaceMember.user_id == user_id
    total = await session.scalar(select(func.count()).select_from(WorkspaceMember).where(filters))
    result = await session.scalars(
        select(Workspace)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(filters)
        .order_by(Workspace.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.all()), int(total or 0)
