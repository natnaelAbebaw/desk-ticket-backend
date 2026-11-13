from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentUser,
    Membership,
    rate_limit_default,
    rate_limit_writes,
    require_role,
)
from app.core.cache import RECORD_TTL_SECONDS, get_text, set_text
from app.db.session import get_db
from app.models.workspace import WorkspaceMember, WorkspaceRole
from app.schemas.common import Page
from app.schemas.workspace import MemberCreate, MemberRead, WorkspaceCreate, WorkspaceRead
from app.services import workspaces as workspace_service

router = APIRouter(dependencies=[Depends(rate_limit_default)])

MemberManager = Annotated[
    WorkspaceMember,
    Depends(require_role(WorkspaceRole.owner, WorkspaceRole.admin)),
]


@router.post(
    "",
    response_model=WorkspaceRead,
    status_code=201,
    dependencies=[Depends(rate_limit_writes)],
)
async def create_workspace(
    payload: WorkspaceCreate,
    user: CurrentUser,
    session: AsyncSession = Depends(get_db),
) -> WorkspaceRead:
    """Create a workspace. The caller becomes its owner."""
    workspace = await workspace_service.create_workspace(session, payload, owner_id=user.id)
    return WorkspaceRead.model_validate(workspace)


@router.get("", response_model=Page[WorkspaceRead])
async def list_my_workspaces(
    user: CurrentUser,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> Page[WorkspaceRead]:
    items, total = await workspace_service.list_user_workspaces(
        session, user.id, limit=limit, offset=offset
    )
    return Page(
        items=[WorkspaceRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{workspace_id}", response_model=WorkspaceRead)
async def get_workspace(
    member: Membership,
    session: AsyncSession = Depends(get_db),
) -> WorkspaceRead:
    key = f"workspace:{member.workspace_id}"
    cached = await get_text(key)
    if cached is not None:
        return WorkspaceRead.model_validate_json(cached)
    workspace = await workspace_service.get_workspace(session, member.workspace_id)
    body = WorkspaceRead.model_validate(workspace)
    await set_text(key, body.model_dump_json(), RECORD_TTL_SECONDS)
    return body


@router.post(
    "/{workspace_id}/members",
    response_model=MemberRead,
    status_code=201,
    dependencies=[Depends(rate_limit_writes)],
)
async def add_member(
    payload: MemberCreate,
    manager: MemberManager,
    session: AsyncSession = Depends(get_db),
) -> MemberRead:
    """Owners and admins add members. Only owners can add owners or admins."""
    member = await workspace_service.add_member(
        session, manager.workspace_id, payload, actor_role=manager.role
    )
    return MemberRead.model_validate(member)


@router.get("/{workspace_id}/members", response_model=Page[MemberRead])
async def list_members(
    member: Membership,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> Page[MemberRead]:
    items, total = await workspace_service.list_members(
        session, member.workspace_id, limit=limit, offset=offset
    )
    return Page(
        items=[MemberRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )
