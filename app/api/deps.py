import hmac
from typing import Annotated

from fastapi import Header

from app.core.config import get_settings
from app.core.errors import AppError


async def require_api_key(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    api_key = get_settings().api_key
    if not api_key:
        return
    provided = ""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not hmac.compare_digest(provided, api_key):
        raise AppError("unauthorized", "Invalid API key", status_code=401)
