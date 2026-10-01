"""MCP errors (S06). Messages never echo call arguments or server stack traces."""

from __future__ import annotations


class ToolsetError(Exception):
    """An execution toolset cannot be built for the requested target."""


class InvalidTarget(ToolsetError):
    pass


class TargetUnavailable(ToolsetError):
    pass


class NoWriteTools(ToolsetError):
    pass


class ToolError(Exception):
    status = "TOOL_ERROR"


class ToolTimeout(ToolError):
    status = "TIMEOUT"


class ToolUnavailable(ToolError):
    """The server could not be reached (transport failure)."""

    status = "UNAVAILABLE"


class ToolCallError(ToolError):
    """The server reported an error for this call (``isError``).

    ``detail`` is the server's (redacted, truncated) message, for the agent to read as untrusted
    tool output (S08). It is never part of ``str(exc)``, so it does not reach logs by accident.
    """

    status = "TOOL_ERROR"

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail
