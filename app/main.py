import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

import app.models  # noqa: F401
from app.api.router import api_router
from app.api.v1 import health
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging, request_id_ctx
from app.core.middleware import RequestContextMiddleware
from app.db.session import engine

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    docs_url = None if settings.environment == "production" else "/docs"
    redoc_url = None if settings.environment == "production" else "/redoc"

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info("starting %s (%s)", settings.app_name, settings.environment)
        yield
        await engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=None if settings.environment == "production" else "/openapi.json",
        lifespan=lifespan,
    )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return _error(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(
            422,
            "validation_error",
            "Request validation failed",
            details=jsonable_encoder(exc.errors()),
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s", request.url.path)
        return _error(500, "internal_error", "Internal server error")

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"service": settings.app_name, "environment": settings.environment}

    return app


def _error(
    status_code: int,
    code: str,
    message: str,
    details: list[object] | None = None,
) -> JSONResponse:
    error: dict[str, object] = {
        "code": code,
        "message": message,
        "request_id": request_id_ctx.get(),
    }
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"error": error})


app = create_app()
