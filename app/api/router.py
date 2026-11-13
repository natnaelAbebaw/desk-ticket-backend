from fastapi import APIRouter

from app.api.v1 import auth, tickets, users, workspaces

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(workspaces.router, prefix="/workspaces", tags=["workspaces"])
api_router.include_router(
    tickets.router,
    prefix="/workspaces/{workspace_id}/tickets",
    tags=["tickets"],
)
