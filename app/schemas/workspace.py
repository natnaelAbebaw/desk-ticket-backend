import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.workspace import WorkspaceRole


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    owner_id: uuid.UUID


class WorkspaceRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MemberCreate(BaseModel):
    user_id: uuid.UUID
    role: WorkspaceRole = WorkspaceRole.member


class MemberRead(BaseModel):
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    role: WorkspaceRole
    joined_at: datetime

    model_config = {"from_attributes": True}
