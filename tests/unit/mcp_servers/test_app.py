"""Server factory: tool registration, annotations, error shaping and /health (S07)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from backend.mcp.connector import _annotations
from backend.mcp.models import ToolSpec
from backend.mcp.permissions import definition_sha256
from mcp_servers.common.app import build_server
from mcp_servers.common.errors import ToolFailed, ToolRejected, sanitized_db_error

REGISTRY = Path(__file__).resolve().parents[3] / "config" / "tool_permissions.yaml"
READS = {"list_schemas", "list_tables", "describe_table", "run_select", "get_row_count"}
READS |= {"find_duplicates"}


class FakeTools:
    name = "fake"

    def __init__(
        self, *, writer: bool, partitions: bool, ddl: bool, error: Exception | None = None
    ) -> None:
        self._writer = writer
        self.supports_partitions = partitions
        self.supports_ddl = ddl
        self.error = error
        self.healthy = True

    @property
    def has_writer(self) -> bool:
        return self._writer

    async def _answer(self, *args: Any) -> str:
        if self.error:
            raise self.error
        return "ok"

    async def ping(self) -> bool:
        return self.healthy

    async def close(self) -> None:
        return None

    async def list_schemas(self) -> str:
        return await self._answer()

    async def list_tables(self, schema: str) -> str:
        return await self._answer(schema)

    async def describe_table(self, table: str) -> str:
        return await self._answer(table)

    async def run_select(self, sql: str) -> str:
        return await self._answer(sql)

    async def get_row_count(self, table: str) -> str:
        return await self._answer(table)

    async def find_duplicates(self, table: str, key_columns: list[str]) -> str:
        return await self._answer(table, key_columns)

    async def list_partitions(self, parent_table: str) -> str:
        return await self._answer(parent_table)

    async def execute_dml(self, sql: str, max_affected_rows: int) -> str:
        return await self._answer(sql, max_affected_rows)

    async def execute_ddl(self, sql: str) -> str:
        return await self._answer(sql)


async def tool_map(tools: FakeTools) -> dict[str, Any]:
    return {t.name: t for t in await build_server(tools).list_tools()}


@pytest.mark.spec("S07-AC6")
async def test_postgres_shape_registers_reads_partitions_and_writes() -> None:
    found = await tool_map(FakeTools(writer=True, partitions=True, ddl=True))
    assert set(found) == READS | {"list_partitions", "execute_dml", "execute_ddl"}
    for name in READS | {"list_partitions"}:
        assert found[name].annotations.read_only_hint is True
        assert found[name].annotations.destructive_hint is False
    for name in ("execute_dml", "execute_ddl"):
        assert found[name].annotations.read_only_hint is False
        assert found[name].annotations.destructive_hint is True


@pytest.mark.spec("S07-AC6")
async def test_mysql_shape_has_no_partitions_or_ddl() -> None:
    found = await tool_map(FakeTools(writer=True, partitions=False, ddl=False))
    assert set(found) == READS | {"execute_dml"}


@pytest.mark.spec("S07-AC11")
@pytest.mark.parametrize("ddl", [True, False])
async def test_no_writer_means_no_write_tools(ddl: bool) -> None:
    found = await tool_map(FakeTools(writer=False, partitions=True, ddl=ddl))
    assert not {n for n in found if n.startswith("execute_")}


@pytest.mark.spec("S07-AC10")
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ToolRejected("only SELECT queries are allowed"), "only SELECT queries are allowed"),
        (ToolFailed("statement timed out"), "statement timed out"),
        (KeyError("postgresql://u:secret@host/db"), "internal error: KeyError"),
    ],
)
async def test_errors_reach_the_caller_as_safe_text(error: Exception, expected: str) -> None:
    server = build_server(FakeTools(writer=False, partitions=False, ddl=False, error=error))
    with pytest.raises(ToolError) as info:
        await server.call_tool("run_select", {"sql": "SELECT 1"})
    assert str(info.value) == f"Error executing tool run_select: {expected}"
    assert "secret" not in str(info.value)


@pytest.mark.spec("S07-AC6", "S07-AC10")
def test_health_reports_server_and_database_without_details() -> None:
    tools = FakeTools(writer=True, partitions=False, ddl=False)
    with TestClient(build_server(tools).streamable_http_app()) as client:
        assert client.get("/health").json() == {"status": "ok", "server": "fake", "db": "ok"}
        tools.healthy = False
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "server": "fake", "db": "down"}


class FakePgError(Exception):
    sqlstate = "42501"


@pytest.mark.spec("S07-AC10")
def test_driver_errors_are_reduced_to_class_and_code() -> None:
    pg = sanitized_db_error(FakePgError('permission denied for table "customer" password=x'))
    assert str(pg) == "database error: FakePgError (42501)"
    my = sanitized_db_error(Exception(1045, "Access denied for user 'root'@'1.2.3.4'"))
    assert str(my) == "database error: Exception (1045)"
    other = sanitized_db_error(OSError("connect to secret-host:5432 failed"))
    assert str(other) == "database error: OSError"


@pytest.mark.spec("S07-AC6")
@pytest.mark.parametrize(
    ("server", "shape"),
    [
        ("postgresql", {"writer": True, "partitions": True, "ddl": True}),
        ("mysql", {"writer": True, "partitions": False, "ddl": False}),
    ],
)
async def test_tool_definitions_match_the_committed_pins(
    server: str, shape: dict[str, bool]
) -> None:
    # A changed docstring, argument or annotation must fail here, not only in integration.
    registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    pinned = {e["tool"]: e["definition_sha256"] for e in registry["tools"] if e["server"] == server}
    live = {
        t.name: definition_sha256(
            ToolSpec(
                name=t.name,
                description=t.description or "",
                input_schema=dict(t.input_schema or {}),
                annotations=_annotations(t),
            )
        )
        for t in await build_server(FakeTools(**shape)).list_tools()
    }
    assert live == pinned


@pytest.mark.spec("S07-AC6")
def test_slow_database_reads_as_db_down_within_the_probe_budget() -> None:
    class SlowTools(FakeTools):
        async def ping(self) -> bool:
            await asyncio.sleep(10)
            return True

    started = time.monotonic()
    with TestClient(
        build_server(SlowTools(writer=False, partitions=False, ddl=False)).streamable_http_app()
    ) as client:
        body = client.get("/health").json()
    assert body["db"] == "down"
    assert time.monotonic() - started < 3
