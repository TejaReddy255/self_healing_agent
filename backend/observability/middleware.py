"""Request correlation middleware (pure ASGI).

Pure ASGI (not ``BaseHTTPMiddleware``) so the request-ID context variable is visible to the
exception handlers and routes running inside it, and so unhandled exceptions can be turned into
the standard error envelope here instead of leaking a stack trace.
"""

from __future__ import annotations

import logging
import time

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.api.errors import error_response
from backend.observability.context import (
    REQUEST_ID_HEADER,
    accept_or_generate_request_id,
    request_id_var,
)

logger = logging.getLogger("backend.request")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = accept_or_generate_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        token = request_id_var.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        # None until a response starts; stays None when the client disconnects/cancels first.
        status_code: int | None = None
        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception(
                "unhandled error", extra={"method": scope["method"], "path": scope["path"]}
            )
            if response_started:
                raise
            response = error_response(500, "INTERNAL_ERROR", "An unexpected error occurred.")
            await response(scope, receive, send_with_request_id)
        finally:
            logger.info(
                "request completed",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status_code": status_code if status_code is not None else "no_response",
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            request_id_var.reset(token)
