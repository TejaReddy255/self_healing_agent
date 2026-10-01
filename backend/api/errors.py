"""Uniform error envelope ``{error_code, message, request_id}`` for every API error (S01).

An optional ``details`` object carries structured context, e.g. a rejected metadata reload's
validation report (S04). It is emitted only when set.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.observability.context import current_request_id


class ApiError(Exception):
    """Domain-level API error rendered with the standard envelope."""

    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        headers: dict[str, str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.headers = headers
        self.details = details


def error_response(
    status_code: int,
    error_code: str,
    message: str,
    headers: dict[str, str] | None = None,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    content: dict[str, Any] = {
        "error_code": error_code,
        "message": message,
        "request_id": current_request_id(),
    }
    if details is not None:
        content["details"] = details
    return JSONResponse(status_code=status_code, content=content, headers=headers)


def _status_error_code(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).name
    except ValueError:
        return f"HTTP_{status_code}"


async def _api_error_handler(_: Request, exc: Exception) -> JSONResponse:
    err = cast(ApiError, exc)
    return error_response(err.status_code, err.error_code, err.message, err.headers, err.details)


async def _http_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    err = cast(StarletteHTTPException, exc)
    message = err.detail if isinstance(err.detail, str) else HTTPStatus(err.status_code).phrase
    return error_response(
        err.status_code, _status_error_code(err.status_code), message, dict(err.headers or {})
    )


async def _validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    err = cast(RequestValidationError, exc)
    fields = sorted({".".join(str(p) for p in e.get("loc", ())) for e in err.errors()})
    return error_response(
        422, "VALIDATION_ERROR", f"Request validation failed: {', '.join(fields)}"
    )


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
