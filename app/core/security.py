import uuid
from datetime import timedelta

import jwt

from app.core.config import get_settings
from app.core.errors import AppError
from app.models.timestamps import utcnow

ALGORITHM = "HS256"
_TOKEN_TYPE = "access"


def unauthorized(message: str = "Invalid or expired token") -> AppError:
    return AppError(
        "unauthorized",
        message,
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


def create_access_token(user_id: uuid.UUID) -> tuple[str, int]:
    settings = get_settings()
    ttl = timedelta(minutes=settings.access_token_ttl_minutes)
    now = utcnow()
    claims = {
        "sub": str(user_id),
        "type": _TOKEN_TYPE,
        "iat": now,
        "exp": now + ttl,
    }
    token = jwt.encode(claims, settings.secret_key, algorithm=ALGORITHM)
    return token, int(ttl.total_seconds())


def decode_access_token(token: str) -> uuid.UUID:
    try:
        claims = jwt.decode(
            token,
            get_settings().secret_key,
            algorithms=[ALGORITHM],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise unauthorized() from exc
    if claims.get("type") != _TOKEN_TYPE:
        raise unauthorized()
    try:
        return uuid.UUID(claims["sub"])
    except (TypeError, ValueError) as exc:
        raise unauthorized() from exc
