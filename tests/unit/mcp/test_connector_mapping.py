from __future__ import annotations

import httpx2
import pytest
from mcp.shared.exceptions import MCPError

from backend.mcp.connector import map_failure
from backend.mcp.errors import ToolCallError, ToolOutcomeUnknown, ToolTimeout, ToolUnavailable


@pytest.mark.spec("S06-AC8")
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ExceptionGroup("tg", [httpx2.ConnectError("refused")]), ToolUnavailable),
        (ExceptionGroup("tg", [httpx2.ReadTimeout("slow")]), ToolTimeout),
        (TimeoutError(), ToolTimeout),
        (
            ExceptionGroup("tg", [ExceptionGroup("inner", [httpx2.ReadError("reset")])]),
            ToolOutcomeUnknown,
        ),
        (RuntimeError("validate_tool_result failed"), ToolOutcomeUnknown),
        (
            ExceptionGroup("tg", [httpx2.ConnectError("a"), httpx2.ReadError("b")]),
            ToolOutcomeUnknown,
        ),
    ],
    ids=["connect", "read-timeout", "timeout", "read-error", "unexpected", "mixed"],
)
def test_sdk_failures_map_to_tool_errors(failure: BaseException, expected: type) -> None:
    mapped = map_failure(failure, "postgresql.run_select")
    assert type(mapped) is expected
    assert "refused" not in str(mapped) and "reset" not in str(mapped)


def test_protocol_errors_keep_detail_out_of_the_message() -> None:
    error = MCPError(-32602, "bad arg password=hunter2")
    mapped = map_failure(ExceptionGroup("tg", [error]), "postgresql.run_select")
    assert isinstance(mapped, ToolCallError)
    assert "hunter2" not in str(mapped) and "hunter2" not in mapped.detail
