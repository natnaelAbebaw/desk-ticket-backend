from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import rate_limit_writes
from app.core.security import create_access_token
from app.db.session import get_db
from app.schemas.auth import TokenRequest, TokenResponse
from app.services import users as user_service

router = APIRouter()


@router.post(
    "/token",
    response_model=TokenResponse,
    dependencies=[Depends(rate_limit_writes)],
)
async def issue_token(
    payload: TokenRequest,
    session: AsyncSession = Depends(get_db),
) -> TokenResponse:
    user = await user_service.authenticate(session, payload.email, payload.password)
    token, expires_in = create_access_token(user.id)
    return TokenResponse(access_token=token, expires_in=expires_in)
