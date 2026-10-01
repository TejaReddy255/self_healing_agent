"""MCP server factory shared by the database servers (S07).

Both servers expose the same tool shapes, so the factory registers them from a backend object.
Read tools carry ``readOnlyHint=true``; write tools ``destructiveHint=true`` and are registered
only when the backend has write credentials (S06 then classifies them via the pinned registry).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any, Protocol

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_servers.common.errors import ToolFailed, ToolRejected

logger = logging.getLogger(__name__)
# Below the S06 probe timeout (3 s), so a slow database reads as "db down", not "server down".
HEALTH_DB_TIMEOUT_S = 1.5

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)


class DatabaseTools(Protocol):
    name: str
    supports_partitions: bool
    supports_ddl: bool

    @property
    def has_writer(self) -> bool: ...

    async def ping(self) -> bool: ...
    async def close(self) -> None: ...
    async def list_schemas(self) -> str: ...
    async def list_tables(self, schema: str) -> str: ...
    async def describe_table(self, table: str) -> str: ...
    async def run_select(self, sql: str) -> str: ...
    async def get_row_count(self, table: str) -> str: ...
    async def find_duplicates(self, table: str, key_columns: list[str]) -> str: ...
    async def list_partitions(self, parent_table: str) -> str: ...
    async def execute_dml(self, sql: str, max_affected_rows: int) -> str: ...
    async def execute_ddl(self, sql: str) -> str: ...


async def _guard(call: Awaitable[str]) -> str:
    """Expected errors pass through; anything else becomes a bare category, never its text."""
    try:
        return await call
    except (ToolRejected, ToolFailed):
        raise
    except Exception as exc:
        logger.error("unexpected tool failure", extra={"error": type(exc).__name__})
        raise ToolFailed(f"internal error: {type(exc).__name__}") from None


def build_server(tools: DatabaseTools) -> MCPServer[Any]:
    server: MCPServer[Any] = MCPServer(tools.name)

    @server.tool(annotations=READ_ONLY)
    async def list_schemas() -> str:
        """List the schemas this server may read."""
        return await _guard(tools.list_schemas())

    @server.tool(annotations=READ_ONLY)
    async def list_tables(schema: str) -> str:
        """List tables in an allowed schema."""
        return await _guard(tools.list_tables(schema))

    @server.tool(annotations=READ_ONLY)
    async def describe_table(table: str) -> str:
        """Columns, types, nullability, keys and PII flags of schema.table."""
        return await _guard(tools.describe_table(table))

    @server.tool(annotations=READ_ONLY)
    async def run_select(sql: str) -> str:
        """Run one read-only SELECT over allowed schemas (row-limited; PII columns refused)."""
        return await _guard(tools.run_select(sql))

    @server.tool(annotations=READ_ONLY)
    async def get_row_count(table: str) -> str:
        """Exact row count of schema.table."""
        return await _guard(tools.get_row_count(table))

    @server.tool(annotations=READ_ONLY)
    async def find_duplicates(table: str, key_columns: list[str]) -> str:
        """Duplicate key groups in schema.table for the given key columns (counts and sample)."""
        return await _guard(tools.find_duplicates(table, key_columns))

    if tools.supports_partitions:

        @server.tool(annotations=READ_ONLY)
        async def list_partitions(parent_table: str) -> str:
            """Partitions of a partitioned schema.table with their bounds."""
            return await _guard(tools.list_partitions(parent_table))

    if tools.has_writer:

        @server.tool(annotations=DESTRUCTIVE)
        async def execute_dml(sql: str, max_affected_rows: int) -> str:
            """Run one INSERT/UPDATE/DELETE; rolled back if more rows change than allowed."""
            return await _guard(tools.execute_dml(sql, max_affected_rows))

        if tools.supports_ddl:

            @server.tool(annotations=DESTRUCTIVE)
            async def execute_ddl(sql: str) -> str:
                """Run allowlisted DDL: CREATE TABLE … PARTITION OF …, or CREATE INDEX."""
                return await _guard(tools.execute_ddl(sql))

    @server.custom_route("/health", methods=["GET"])  # type: ignore[untyped-decorator]
    async def health(_: Request) -> JSONResponse:
        try:
            db_ok = await asyncio.wait_for(tools.ping(), HEALTH_DB_TIMEOUT_S)
        except TimeoutError:
            db_ok = False
        # 200 either way: the S06 probe can tell "server up, database down" from "down".
        return JSONResponse({"status": "ok", "server": tools.name, "db": "ok" if db_ok else "down"})

    return server
