from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from backend.domain.audit import AuditEvent
from backend.domain.common import SystemName
from backend.mcp.errors import ToolCallError, ToolTimeout, ToolUnavailable
from backend.mcp.invoke import cap_output
from backend.mcp.manager import MCPManager
from backend.mcp.models import InvocationContext
from backend.mcp.toolsets import diagnostic_toolset
from tests.fakes.mcp import FakeMCPConnector, FakeServer, mcp_settings, registry, tool

PG = SystemName.POSTGRESQL
CTX = InvocationContext(phase="diagnosis", request_id="req-1", pipeline_id="customer_daily_load")


class RecordingAudit:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[AuditEvent] = []
        self.fail = fail

    async def record(self, event: AuditEvent, **_: Any) -> int:
        if self.fail:
            raise ConnectionError("audit db down")
        self.events.append(event)
        return len(self.events)


async def setup(
    tmp_path: Path, fake: FakeServer, *, write: bool = False, **settings: Any
) -> tuple[MCPManager, FakeMCPConnector]:
    entries = [
        {"server": "postgresql", "tool": "run_select", "access": "read", "timeout_s": 0.05},
        {"server": "postgresql", "tool": "execute_dml", "access": "write"},
    ]
    connector = FakeMCPConnector({PG: fake})
    manager = MCPManager(
        mcp_settings(tmp_path, **settings), registry(tmp_path, entries), connector, env="test"
    )
    await manager.discover()
    return manager, connector


def handle(manager: MCPManager, audit: RecordingAudit, name: str = "postgresql.run_select") -> Any:
    from backend.mcp.invoke import ToolHandle

    tool_ = manager.snapshot().servers[PG].tools[name.split(".", 1)[1]]
    return ToolHandle(tool_, manager.server_config(PG), manager.connector, audit, 2)


# --- AC8: audit, timeout, retries ----------------------------------------------------------------


@pytest.mark.spec("S06-AC8")
async def test_invocation_writes_start_before_and_end_after(tmp_path: Path) -> None:
    fake = FakeServer(tools=[tool("run_select")], results={"run_select": "42 rows"})
    manager, _ = await setup(tmp_path, fake)
    audit = RecordingAudit()
    (read,) = diagnostic_toolset(manager, audit)
    output = await read.invoke({"sql": "SELECT 1", "password": "hunter2"}, CTX)
    assert output.content == "42 rows" and not output.truncated
    assert [(e.status, e.event_type, e.tool_name) for e in audit.events] == [
        ("STARTED", "TOOL_CALL", "postgresql.run_select"),
        ("OK", "TOOL_CALL", "postgresql.run_select"),
    ]
    start, end = audit.events
    assert start.duration_ms is None and end.duration_ms is not None
    assert (end.request_id, end.pipeline_id, end.phase) == (
        "req-1",
        "customer_daily_load",
        "diagnosis",
    )
    assert end.output == {"text": "42 rows"}


@pytest.mark.spec("S06-AC8")
async def test_timeouts_are_audited(tmp_path: Path) -> None:
    fake = FakeServer(tools=[tool("run_select")], delay_s=1.0)
    manager, _ = await setup(tmp_path, fake)
    audit = RecordingAudit()
    with pytest.raises(ToolTimeout):
        await handle(manager, audit).invoke({"sql": "SELECT pg_sleep(9)"}, CTX)
    assert [e.status for e in audit.events] == ["STARTED", "TIMEOUT"]


@pytest.mark.spec("S06-AC8")
async def test_read_tools_retry_transport_errors(tmp_path: Path) -> None:
    fake = FakeServer(tools=[tool("run_select")], unavailable_times=2)
    manager, connector = await setup(tmp_path, fake)
    output = await handle(manager, RecordingAudit()).invoke({}, CTX)
    assert output.content == "run_select ok"
    assert len(connector.calls) == 3


@pytest.mark.spec("S06-AC8")
async def test_write_tools_are_attempted_exactly_once(tmp_path: Path) -> None:
    fake = FakeServer(tools=[tool("execute_dml")], unavailable_times=5)
    manager, connector = await setup(tmp_path, fake)
    audit = RecordingAudit()
    with pytest.raises(ToolUnavailable):
        await handle(manager, audit, "postgresql.execute_dml").invoke({"sql": "DELETE …"}, CTX)
    assert len(connector.calls) == 1
    assert [e.status for e in audit.events] == ["STARTED", "UNAVAILABLE"]
    assert audit.events[0].operation == "write"


@pytest.mark.spec("S06-AC8")
async def test_server_errors_are_audited_and_detail_is_kept_out_of_messages(tmp_path: Path) -> None:
    error = ToolCallError("postgresql.run_select reported an error", detail="relation missing")
    fake = FakeServer(tools=[tool("run_select")], results={"run_select": error})
    manager, _ = await setup(tmp_path, fake)
    audit = RecordingAudit()
    with pytest.raises(ToolCallError) as caught:
        await handle(manager, audit).invoke({"sql": "SELECT"}, CTX)
    assert caught.value.detail == "relation missing"
    assert "relation missing" not in str(caught.value)
    assert [e.status for e in audit.events] == ["STARTED", "TOOL_ERROR"]


async def test_audit_outage_never_hides_a_completed_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeServer(tools=[tool("execute_dml")])
    manager, connector = await setup(tmp_path, fake)
    with caplog.at_level(logging.ERROR):
        output = await handle(manager, RecordingAudit(fail=True), "postgresql.execute_dml").invoke(
            {}, CTX
        )
    assert output.content == "execute_dml ok" and len(connector.calls) == 1
    assert "mcp tool audit failed" in caplog.text


# --- AC11: output caps and no leakage ------------------------------------------------------------


@pytest.mark.spec("S06-AC11")
def test_output_is_capped_at_a_utf8_boundary() -> None:
    text = "é" * 10  # 2 bytes each
    capped, truncated, size = cap_output(text, 5)
    assert truncated and size == 20
    assert capped.startswith("éé") and "truncated 16 bytes" in capped
    capped.encode("utf-8")  # still valid UTF-8


@pytest.mark.spec("S06-AC11")
async def test_large_outputs_are_truncated_and_audited_as_such(tmp_path: Path) -> None:
    fake = FakeServer(tools=[tool("run_select")], results={"run_select": "x" * 20_000})
    manager, _ = await setup(tmp_path, fake, mcp_max_output_bytes=1024)
    audit = RecordingAudit()
    output = await handle(manager, audit).invoke({}, CTX)
    assert output.truncated and output.original_bytes == 20_000
    assert len(output.content.encode()) < 1024 + 64
    assert audit.events[-1].status == "OUTPUT_TRUNCATED"


@pytest.mark.spec("S06-AC11")
async def test_errors_and_logs_never_contain_arguments(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeServer(tools=[tool("run_select")], unavailable_times=10)
    manager, _ = await setup(tmp_path, fake)
    with caplog.at_level(logging.DEBUG), pytest.raises(ToolUnavailable) as caught:
        await handle(manager, RecordingAudit()).invoke({"sql": "SELECT 'secret-arg-123'"}, CTX)
    assert "secret-arg-123" not in str(caught.value)
    assert "secret-arg-123" not in caplog.text
