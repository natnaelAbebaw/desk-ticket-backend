import logging
import time
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import request_id_ctx

logger = logging.getLogger("app.access")
_MAX_REQUEST_ID_LENGTH = 64


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id(scope)
        token = request_id_ctx.set(request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_with_headers(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["x-request-id"] = request_id
                headers["x-content-type-options"] = "nosniff"
                headers["x-frame-options"] = "DENY"
                headers["referrer-policy"] = "no-referrer"
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            method = scope.get("method", "-")
            path = scope.get("path", "-")
            logger.info("%s %s %s %.1fms", method, path, status_code, duration_ms)
            request_id_ctx.reset(token)


def _request_id(scope: Scope) -> str:
    for key, value in scope.get("headers", []):
        if key.decode("latin-1").lower() == "x-request-id":
            incoming = value.decode("latin-1").strip()
            if incoming and len(incoming) <= _MAX_REQUEST_ID_LENGTH:
                return incoming
            break
    return uuid4().hex
