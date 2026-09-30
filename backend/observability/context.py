"""Per-request correlation context shared by logging, middleware and error handlers."""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

REQUEST_ID_HEADER = "X-Request-ID"

# Inbound IDs are echoed into logs and headers, so accept only a conservative charset/length.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    return uuid.uuid4().hex


def accept_or_generate_request_id(inbound: str | None) -> str:
    """Return the inbound ID when it is safe to reuse, otherwise a fresh one."""
    if inbound and _VALID_REQUEST_ID.fullmatch(inbound):
        return inbound
    return new_request_id()


def current_request_id() -> str | None:
    return request_id_var.get()
