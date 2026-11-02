import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import rate_limit_default, rate_limit_writes, require_api_key
from app.core.cache import RECORD_TTL_SECONDS, get_text, set_text
from app.db.session import get_db
from app.schemas.common import Page
from app.schemas.workspace import MemberCreate, MemberRead, WorkspaceCreate, WorkspaceRead
from app.services import workspaces as workspace_service

router = APIRouter(dependencies=[Depends(require_api_key), Depends(rate_limit_default)])


@router.post(
    "",
    response_model=WorkspaceRead,
    status_code=201,
    dependencies=[Depends(rate_limit_writes)],
)
async def create_workspace(
    payload: WorkspaceCreate,
    session: AsyncSession = Depends(get_db),
) -> WorkspaceRead:
    workspace = await workspace_service.create_workspace(session, payload)
    return WorkspaceRead.model_validate(workspace)


@router.get("/{workspace_id}", response_model=WorkspaceRead)
async def get_workspace(
    workspace_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> WorkspaceRead:
    key = f"workspace:{workspace_id}"
    cached = await get_text(key)
    if cached is not None:
        return WorkspaceRead.model_validate_json(cached)
    workspace = await workspace_service.get_workspace(session, workspace_id)
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
    workspace_id: uuid.UUID,
    payload: MemberCreate,
    session: AsyncSession = Depends(get_db),
) -> MemberRead:
    member = await workspace_service.add_member(session, workspace_id, payload)
    return MemberRead.model_validate(member)


@router.get("/{workspace_id}/members", response_model=Page[MemberRead])
async def list_members(
    workspace_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> Page[MemberRead]:
    items, total = await workspace_service.list_members(
        session, workspace_id, limit=limit, offset=offset
    )
    return Page(
        items=[MemberRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )
