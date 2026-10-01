from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from backend.domain.audit import AuditEvent
from backend.domain.common import SystemName
from backend.mcp.errors import (
    TargetUnavailable,
    ToolCallError,
    ToolNotPermitted,
    ToolOutcomeUnknown,
    ToolTimeout,
    ToolUnavailable,
)
from backend.mcp.invoke import ToolHandle, cap_output
from backend.mcp.manager import MCPManager
from backend.mcp.models import InvocationContext
from backend.mcp.toolsets import diagnostic_toolset, execution_toolset
from tests.fakes.mcp import FakeMCPConnector, FakeServer, entry, mcp_settings, registry, tool

PG = SystemName.POSTGRESQL
CTX = InvocationContext(phase="diagnosis", request_id="req-1", pipeline_id="customer_daily_load")
SELECT = tool("run_select")
DML = tool("execute_dml", hints={"destructive_hint": True})


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
    tmp_path: Path, fake: FakeServer, **settings: Any
) -> tuple[MCPManager, FakeMCPConnector]:
    entries = [
        entry("postgresql", SELECT, "read", timeout_s=0.05),
        entry("postgresql", DML, "write"),
    ]
    fake.tools = fake.tools or [SELECT, DML]
    connector = FakeMCPConnector({PG: fake})
    manager = MCPManager(
        mcp_settings(tmp_path, **settings), registry(tmp_path, entries), connector, env="test"
    )
    await manager.discover()
    return manager, connector


def read_handle(manager: MCPManager, audit: Any) -> ToolHandle:
    (handle,) = diagnostic_toolset(manager, audit)
    return handle


def write_handle(manager: MCPManager, audit: Any) -> ToolHandle:
    return next(
        h for h in execution_toolset(manager, audit, "postgresql") if h.name.endswith("dml")
    )


# --- AC8: audit, timeout, retries ----------------------------------------------------------------


@pytest.mark.spec("S06-AC8")
async def test_start_row_exists_before_the_server_is_called(tmp_path: Path) -> None:
    audit = RecordingAudit()
    seen_at_call: list[list[str]] = []

    def respond(arguments: Any) -> str:
        seen_at_call.append([e.status for e in audit.events])
        return "42 rows"

    manager, _ = await setup(tmp_path, FakeServer(results={"run_select": respond}))
    output = await read_handle(manager, audit).invoke({"sql": "SELECT 1", "password": "x"}, CTX)
    assert output.content == "42 rows" and not output.truncated
    assert seen_at_call == [["STARTED"]]
    start, end = audit.events
    assert (start.status, end.status) == ("STARTED", "OK")
    assert start.duration_ms is None and end.duration_ms is not None
    assert (end.request_id, end.pipeline_id, end.phase) == (
        "req-1",
        "customer_daily_load",
        "diagnosis",
    )


@pytest.mark.spec("S06-AC8")
async def test_timeouts_are_audited_and_not_retried(tmp_path: Path) -> None:
    manager, connector = await setup(tmp_path, FakeServer(delay_s=1.0))
    audit = RecordingAudit()
    with pytest.raises(ToolTimeout):
        await read_handle(manager, audit).invoke({}, CTX)
    assert [e.status for e in audit.events] == ["STARTED", "TIMEOUT"]
    assert len(connector.calls) == 1


@pytest.mark.spec("S06-AC8")
@pytest.mark.parametrize(
    "failure", [ToolUnavailable("down"), ToolOutcomeUnknown("lost")], ids=["unavailable", "unknown"]
)
async def test_read_tools_retry_transport_failures(tmp_path: Path, failure: Exception) -> None:
    calls = 0

    def flaky(arguments: Any) -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise failure
        return "ok"

    manager, _ = await setup(tmp_path, FakeServer(results={"run_select": flaky}))
    assert (await read_handle(manager, RecordingAudit()).invoke({}, CTX)).content == "ok"
    assert calls == 3


@pytest.mark.spec("S06-AC8")
async def test_write_tools_are_attempted_exactly_once(tmp_path: Path) -> None:
    manager, connector = await setup(tmp_path, FakeServer(unavailable_times=5))
    audit = RecordingAudit()
    with pytest.raises(ToolUnavailable):
        await write_handle(manager, audit).invoke({"sql": "DELETE …"}, CTX)
    assert len(connector.calls) == 1
    assert [e.status for e in audit.events] == ["STARTED", "UNAVAILABLE"]
    assert audit.events[0].operation == "write"


@pytest.mark.spec("S06-AC8")
async def test_a_write_without_a_recorded_start_row_is_refused(tmp_path: Path) -> None:
    manager, connector = await setup(tmp_path, FakeServer())
    with pytest.raises(ToolNotPermitted, match="start row"):
        await write_handle(manager, RecordingAudit(fail=True)).invoke({"sql": "DELETE …"}, CTX)
    assert connector.calls == []  # never contacted


async def test_reads_proceed_when_audit_is_down(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager, connector = await setup(tmp_path, FakeServer())
    with caplog.at_level(logging.ERROR):
        output = await read_handle(manager, RecordingAudit(fail=True)).invoke({}, CTX)
    assert output.content == "run_select ok" and len(connector.calls) == 1
    assert "mcp tool audit failed" in caplog.text


@pytest.mark.spec("S06-AC8")
async def test_cancellation_is_recorded(tmp_path: Path) -> None:
    manager, _ = await setup(tmp_path, FakeServer(delay_s=5.0))
    audit = RecordingAudit()
    handle = write_handle(manager, audit)
    task = asyncio.create_task(handle.invoke({"sql": "DELETE …"}, CTX))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [e.status for e in audit.events] == ["STARTED", "CANCELLED"]


@pytest.mark.spec("S06-AC8")
async def test_server_errors_are_audited_and_detail_stays_out_of_messages(tmp_path: Path) -> None:
    error = ToolCallError("postgresql.run_select reported an error", detail="relation missing")
    manager, _ = await setup(tmp_path, FakeServer(results={"run_select": error}))
    audit = RecordingAudit()
    with pytest.raises(ToolCallError) as caught:
        await read_handle(manager, audit).invoke({"sql": "SELECT"}, CTX)
    assert caught.value.detail == "relation missing"
    assert "relation missing" not in str(caught.value)
    assert [e.status for e in audit.events] == ["STARTED", "TOOL_ERROR"]


# --- live re-check: a handle cannot outlive its permission ---------------------------------------


async def test_handles_are_refused_after_the_tool_changes(tmp_path: Path) -> None:
    fake = FakeServer()
    manager, connector = await setup(tmp_path, fake)
    audit = RecordingAudit()
    handle = read_handle(manager, audit)
    fake.tools = [replace(SELECT, description="now it also deletes"), DML]  # rug pull
    await manager.discover()
    with pytest.raises(ToolNotPermitted):
        await handle.invoke({}, CTX)
    assert connector.calls == []
    assert [e.status for e in audit.events] == ["NOT_PERMITTED"]

    fake.healthy = False
    await manager.discover()
    with pytest.raises(TargetUnavailable):
        write_handle(manager, audit)  # the execution toolset refuses an unhealthy server


# --- AC11: output caps and no leakage ------------------------------------------------------------


@pytest.mark.spec("S06-AC11")
def test_output_is_capped_at_a_utf8_boundary() -> None:
    capped, truncated, size = cap_output("é" * 10, 5)
    assert truncated and size == 20
    assert capped.startswith("éé") and "truncated 16 bytes" in capped
    capped.encode("utf-8")


@pytest.mark.spec("S06-AC11")
async def test_large_outputs_are_truncated_and_audited_as_such(tmp_path: Path) -> None:
    manager, _ = await setup(
        tmp_path, FakeServer(results={"run_select": "x" * 20_000}), mcp_max_output_bytes=1024
    )
    audit = RecordingAudit()
    output = await read_handle(manager, audit).invoke({}, CTX)
    assert output.truncated and output.original_bytes == 20_000
    assert len(output.content.encode()) < 1024 + 64
    assert audit.events[-1].status == "OUTPUT_TRUNCATED"


@pytest.mark.spec("S06-AC11")
async def test_errors_and_logs_never_contain_arguments(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager, _ = await setup(tmp_path, FakeServer(unavailable_times=10))
    with caplog.at_level(logging.DEBUG), pytest.raises(ToolUnavailable) as caught:
        await read_handle(manager, RecordingAudit()).invoke({"sql": "SELECT 'secret-arg-123'"}, CTX)
    assert "secret-arg-123" not in str(caught.value)
    assert "secret-arg-123" not in caplog.text
