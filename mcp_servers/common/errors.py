"""Errors returned to MCP callers (S07). Messages are safe to show: no DSNs, no server internals."""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError


class ToolRejected(ToolError):
    """The request was refused before touching the database (validation, PII, identifiers).

    Subclassing the SDK's ``ToolError`` makes the server return the message as an expected tool
    error (``isError``) rather than log a traceback.
    """


class ToolFailed(ToolError):
    """The database refused or failed the operation; the message is a sanitized category."""


def sanitized_db_error(exc: BaseException) -> ToolFailed:
    """Reduce a driver exception to its class and SQLSTATE: never connection details or values."""
    kind = type(exc).__name__
    sqlstate = getattr(exc, "sqlstate", None)  # a 5-character SQLSTATE (asyncpg, PyMySQL)
    if isinstance(sqlstate, str) and len(sqlstate) == 5 and sqlstate.isalnum():
        return ToolFailed(f"database error: {kind} ({sqlstate})")
    args = getattr(exc, "args", ())
    code = args[0] if args else None
    if isinstance(code, int):  # PyMySQL: a numeric error code (the message is never used)
        return ToolFailed(f"database error: {kind} ({code})")
    return ToolFailed(f"database error: {kind}")
