"""Mounts every v1 router. app/main.py includes `api_router` under the /api/v1 prefix.

A path parameter in a prefix, like {workspace_id}, is shared by every route in that
router. FastAPI hands it to any dependency that declares a `workspace_id` argument,
which is how `get_membership` in app/api/deps.py checks workspace access without each
route passing it along.
"""

from fastapi import APIRouter

from app.api.v1 import auth, knowledge, tickets, users, workspaces

api_router = APIRouter()
# `tags` only group routes in the OpenAPI docs at /docs. They do not affect routing.
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(workspaces.router, prefix="/workspaces", tags=["workspaces"])
api_router.include_router(
    tickets.router,
    prefix="/workspaces/{workspace_id}/tickets",
    tags=["tickets"],
)
# Two routers can share a prefix. This one adds POST .../tickets/{ticket_id}/knowledge
# next to the ticket routes, but its code lives with the rest of the knowledge feature.
api_router.include_router(
    knowledge.ticket_router,
    prefix="/workspaces/{workspace_id}/tickets",
    tags=["knowledge"],
)
api_router.include_router(
    knowledge.router,
    prefix="/workspaces/{workspace_id}/knowledge",
    tags=["knowledge"],
)
