"""Tool invocation audit rows in the real operation_audit table (S06-AC8)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from backend.db.pool import Database
from backend.db.settings import DatabaseSettings
from backend.domain.common import SystemName
from backend.mcp.errors import ToolTimeout
from backend.mcp.manager import MCPManager
from backend.mcp.models import InvocationContext
from backend.mcp.toolsets import diagnostic_toolset
from backend.services.audit_service import AuditService
from tests.fakes.mcp import FakeMCPConnector, FakeServer, entry, mcp_settings, registry, tool
from tests.integration.conftest import connect

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

PG = SystemName.POSTGRESQL


@pytest.mark.spec("S06-AC8")
def test_invocations_write_start_and_end_rows(
    migrated_db: DatabaseSettings, tmp_path: Path
) -> None:
    async def scenario() -> list[dict[str, Any]]:
        database = Database(migrated_db, application_name="tests")
        await database.open()
        try:
            audit = AuditService(database, 4096)
            select, slow = tool("run_select"), tool("slow_select")
            fake = FakeServer(tools=[select, slow], results={"run_select": "x" * 9000})
            entries = [
                entry("postgresql", select, "read"),
                entry("postgresql", slow, "read", timeout_s=0.05),
            ]
            manager = MCPManager(
                mcp_settings(tmp_path), registry(tmp_path, entries), FakeMCPConnector({PG: fake}),
                env="test",
            )  # fmt: skip
            await manager.discover()
            tools = {h.name: h for h in diagnostic_toolset(manager, audit)}
            ctx = InvocationContext(
                phase="diagnosis", request_id="req-42", dag_id="customer_daily_load"
            )
            await tools["postgresql.run_select"].invoke(
                {"sql": "SELECT", "password": "hunter2"}, ctx
            )
            fake.delay_s = 1.0
            with pytest.raises(ToolTimeout):
                await tools["postgresql.slow_select"].invoke({"sql": "SELECT pg_sleep(5)"}, ctx)
        finally:
            await database.close()
        conn = await connect(migrated_db)
        try:
            rows = await conn.fetch(
                "SELECT tool_name, status, server, request_id, dag_id, duration_ms, input, output"
                " FROM operation_audit WHERE event_type = 'TOOL_CALL' ORDER BY id"
            )
        finally:
            await conn.close()
        return [dict(r) for r in rows]

    rows = asyncio.run(scenario())
    assert [(r["tool_name"], r["status"]) for r in rows] == [
        ("postgresql.run_select", "STARTED"),
        ("postgresql.run_select", "OUTPUT_TRUNCATED"),
        ("postgresql.slow_select", "STARTED"),
        ("postgresql.slow_select", "TIMEOUT"),
    ]
    assert all(r["server"] == "postgresql" and r["request_id"] == "req-42" for r in rows)
    assert rows[0]["duration_ms"] is None and rows[1]["duration_ms"] is not None
    assert "hunter2" not in str(rows[0]["input"])  # redacted by AuditService
