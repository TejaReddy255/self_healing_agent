"""MCP errors (S06). Messages never echo call arguments or server stack traces.

For callers deciding whether a call may have taken effect:
- ``ToolUnavailable``: the request never reached the server (connect failure). Safe to retry.
- ``ToolOutcomeUnknown`` (incl. ``ToolTimeout``): the request may have run. A WRITE in this state
  must never be retried automatically; it needs verification or an operator (NFR-011, S11).
- ``ToolCallError``: the server answered with an error.
- ``ToolNotPermitted``: refused locally before contacting the server.
"""

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


class ToolUnavailable(ToolError):
    """The server could not be reached: the request was never sent."""

    status = "UNAVAILABLE"


class ToolOutcomeUnknown(ToolError):
    """The request was sent (or may have been) but no result arrived."""

    status = "OUTCOME_UNKNOWN"


class ToolTimeout(ToolOutcomeUnknown):
    status = "TIMEOUT"


class ToolCallError(ToolError):
    """The server reported an error for this call.

    ``detail`` is the server's (redacted, truncated) message, for the agent to read as untrusted
    tool output (S08). It is never part of ``str(exc)``, so it does not reach logs by accident.
    """

    status = "TOOL_ERROR"

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class ToolNotPermitted(ToolError):
    """Refused before contacting the server (no longer classified as usable, or no audit)."""

    status = "NOT_PERMITTED"
