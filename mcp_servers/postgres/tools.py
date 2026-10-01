"""PostgreSQL MCP tools (S07).

Reads use the reader role inside ``BEGIN READ ONLY`` with a statement timeout; the role itself is
read-only by default and holds SELECT grants only. Writes use the writer role in a transaction that
is rolled back when more rows change than the caller allowed. Allowlisted DDL runs with
``SET LOCAL ROLE dw_owner`` (the writer is a NOINHERIT member), so ordinary DML never has owner
rights.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import asyncpg

from mcp_servers.common import duplicates
from mcp_servers.common.errors import ToolFailed, ToolRejected, sanitized_db_error
from mcp_servers.common.identifiers import check_identifier, qualified, split_table
from mcp_servers.common.pii import PiiPolicy
from mcp_servers.common.results import dumps, table_result
from mcp_servers.common.settings import PostgresServerSettings
from mcp_servers.common.sql import render, validate_ddl, validate_dml, validate_select

logger = logging.getLogger(__name__)

T = TypeVar("T")
DDL_OWNER_ROLE = "dw_owner"
Conn = Any  # asyncpg Connection or pool proxy; both expose the same query API


class _OverCeiling(Exception):  # control flow only: never leaves this module
    def __init__(self, affected: int) -> None:
        super().__init__(affected)
        self.affected = affected


class PostgresTools:
    name = "postgres"
    supports_partitions = True
    supports_ddl = True

    def __init__(self, settings: PostgresServerSettings) -> None:
        self._settings = settings
        self._schemas = settings.mcp_postgres_schemas
        self._pii = PiiPolicy(settings.mcp_pii_columns)
        self._pools: dict[str, asyncpg.Pool] = {}
        self._lock = asyncio.Lock()

    @property
    def has_writer(self) -> bool:
        return self._settings.mcp_postgres_writer_dsn is not None

    async def _pool(self, role: str) -> asyncpg.Pool:
        dsn = (
            self._settings.mcp_postgres_reader_dsn
            if role == "reader"
            else self._settings.mcp_postgres_writer_dsn
        )
        if dsn is None:
            raise ToolFailed(f"no {role} credentials configured")
        async with self._lock:
            if role not in self._pools:
                try:
                    self._pools[role] = await asyncpg.create_pool(
                        dsn.get_secret_value(),
                        min_size=0,
                        max_size=4,
                        timeout=5,
                        server_settings={
                            "application_name": f"mcp-postgres-{role}",
                            # Regenerated SQL assumes standard string literals.
                            "standard_conforming_strings": "on",
                        },
                    )
                except Exception as exc:
                    raise sanitized_db_error(exc) from None
        return self._pools[role]

    async def close(self) -> None:
        async with self._lock:
            pools, self._pools = list(self._pools.values()), {}
        for pool in pools:
            await pool.close()

    async def _run(self, role: str, fn: Callable[[Conn], Awaitable[T]], *, readonly: bool) -> T:
        pool = await self._pool(role)
        timeout_ms = int(self._settings.mcp_statement_timeout_ms)
        try:
            async with pool.acquire(timeout=5) as conn, conn.transaction(readonly=readonly):
                await conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
                return await fn(conn)
        except (ToolRejected, ToolFailed):
            raise
        except asyncpg.QueryCanceledError:
            raise ToolFailed("statement timed out") from None
        except Exception as exc:
            logger.warning("postgres tool failed", extra={"error": type(exc).__name__})
            raise sanitized_db_error(exc) from None

    async def _read(self, fn: Callable[[Conn], Awaitable[T]]) -> T:
        return await self._run("reader", fn, readonly=True)

    # --- catalog helpers ------------------------------------------------------------------

    async def _require_table(self, conn: Conn, value: str) -> tuple[str, str]:
        schema, table = split_table(value.lower(), self._schemas)
        found = await conn.fetchval(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = $1 AND table_name = $2",
            schema,
            table,
        )
        if not found:
            raise ToolRejected(f"table {schema}.{table} does not exist")
        return schema, table

    async def _columns(self, conn: Conn, schema: str, table: str) -> list[Any]:
        rows: list[Any] = await conn.fetch(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
            " WHERE table_schema = $1 AND table_name = $2 ORDER BY ordinal_position",
            schema,
            table,
        )
        return rows

    # --- read tools -----------------------------------------------------------------------

    async def ping(self) -> bool:
        try:
            return bool(await self._read(lambda conn: conn.fetchval("SELECT 1")))
        except Exception:
            return False

    async def list_schemas(self) -> str:
        async def run(conn: Conn) -> str:
            rows = await conn.fetch(
                "SELECT schema_name FROM information_schema.schemata"
                " WHERE schema_name = ANY($1::text[]) ORDER BY 1",
                self._schemas,
            )
            return dumps({"schemas": [r["schema_name"] for r in rows]})

        return await self._read(run)

    async def list_tables(self, schema: str) -> str:
        name = check_identifier(schema, "schema").lower()
        if name not in self._schemas:
            raise ToolRejected(f"schema {schema!r} is not available on this server")

        async def run(conn: Conn) -> str:
            rows = await conn.fetch(
                "SELECT table_name, table_type FROM information_schema.tables"
                " WHERE table_schema = $1 ORDER BY 1",
                name,
            )
            return dumps({"tables": [{"name": r[0], "type": r[1]} for r in rows]})

        return await self._read(run)

    async def describe_table(self, table: str) -> str:
        async def run(conn: Conn) -> str:
            schema, name = await self._require_table(conn, table)
            columns = await self._columns(conn, schema, name)
            keys = await conn.fetch(
                "SELECT tc.constraint_type, kcu.column_name, tc.constraint_name"
                " FROM information_schema.table_constraints tc"
                " JOIN information_schema.key_column_usage kcu"
                "   ON kcu.constraint_name = tc.constraint_name"
                "  AND kcu.table_schema = tc.table_schema AND kcu.table_name = tc.table_name"
                " WHERE tc.table_schema = $1 AND tc.table_name = $2"
                "   AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')"
                " ORDER BY tc.constraint_name, kcu.ordinal_position",
                schema,
                name,
            )
            primary = [k[1] for k in keys if k[0] == "PRIMARY KEY"]
            unique: dict[str, list[str]] = {}
            for k in keys:
                if k[0] == "UNIQUE":
                    unique.setdefault(k[2], []).append(k[1])
            return dumps(
                {
                    "table": f"{schema}.{name}",
                    "columns": [
                        {
                            "name": c[0],
                            "type": c[1],
                            "nullable": c[2] == "YES",
                            "pii": self._pii.is_pii(schema, name, c[0]),
                        }
                        for c in columns
                    ],
                    "primary_key": primary,
                    "unique": list(unique.values()),
                }
            )

        return await self._read(run)

    async def run_select(self, sql: str) -> str:
        tree = validate_select(sql, "postgres", self._schemas)
        self._pii.check_select(tree)
        statement = render(tree, "postgres")  # run exactly what was validated
        limit = self._settings.mcp_row_limit

        async def run(conn: Conn) -> str:
            prepared = await conn.prepare(statement)
            columns = [a.name for a in prepared.get_attributes()]
            cursor = await prepared.cursor()
            rows = await cursor.fetch(limit + 1)
            return table_result(columns, [tuple(r) for r in rows], limit)

        return await self._read(run)

    async def get_row_count(self, table: str) -> str:
        async def run(conn: Conn) -> str:
            schema, name = await self._require_table(conn, table)
            source = qualified(schema, name, "postgres")  # validated, quoted identifiers
            count = await conn.fetchval(f"SELECT count(*) FROM {source}")  # noqa: S608
            return dumps({"table": f"{schema}.{name}", "row_count": int(count)})

        return await self._read(run)

    async def find_duplicates(self, table: str, key_columns: list[str]) -> str:
        keys = duplicates.check_key_columns(key_columns, lower=True)

        async def run(conn: Conn) -> str:
            schema, name = await self._require_table(conn, table)
            existing = {c[0] for c in await self._columns(conn, schema, name)}
            if missing := [k for k in keys if k not in existing]:
                raise ToolRejected(f"unknown column(s): {', '.join(missing)}")
            summary_sql, sample_sql = duplicates.queries(
                keys, qualified(schema, name, "postgres"), "postgres"
            )
            summary = await conn.fetchrow(summary_sql)
            if summary is None:
                raise ToolFailed("duplicate summary returned no row")
            sample = await conn.fetch(sample_sql)
            masked = {k for k in keys if self._pii.is_pii(schema, name, k)}
            return duplicates.result(f"{schema}.{name}", keys, tuple(summary), sample, masked)

        return await self._read(run)

    async def list_partitions(self, parent_table: str) -> str:
        async def run(conn: Conn) -> str:
            schema, name = await self._require_table(conn, parent_table)
            rows = await conn.fetch(
                "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid)"
                " FROM pg_inherits i"
                " JOIN pg_class c ON c.oid = i.inhrelid"
                " JOIN pg_class p ON p.oid = i.inhparent"
                " JOIN pg_namespace n ON n.oid = p.relnamespace"
                " WHERE n.nspname = $1 AND p.relname = $2 ORDER BY 1",
                schema,
                name,
            )
            return dumps(
                {
                    "parent": f"{schema}.{name}",
                    "partitions": [{"name": r[0], "bound": r[1]} for r in rows],
                }
            )

        return await self._read(run)

    # --- write tools ----------------------------------------------------------------------

    async def execute_dml(self, sql: str, max_affected_rows: int) -> str:
        ceiling = self._settings.mcp_max_affected_rows
        if not isinstance(max_affected_rows, int) or not 1 <= max_affected_rows <= ceiling:
            raise ToolRejected(f"max_affected_rows must be between 1 and {ceiling}")
        statement = render(validate_dml(sql, "postgres", self._schemas), "postgres")
        pool = await self._pool("writer")
        timeout_ms = int(self._settings.mcp_statement_timeout_ms)
        try:
            async with pool.acquire(timeout=5) as conn:
                try:
                    # Commit happens on leaving the block; _OverCeiling rolls it back.
                    async with conn.transaction():
                        await conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
                        status = await conn.execute(statement)
                        last = status.split()[-1] if status else ""
                        affected = int(last) if last.isdigit() else 0
                        if affected > max_affected_rows:
                            raise _OverCeiling(affected)
                except _OverCeiling as over:
                    return dumps(
                        {
                            "status": "aborted",
                            "affected_rows": over.affected,
                            "max_affected_rows": max_affected_rows,
                        }
                    )
                return dumps({"status": "committed", "affected_rows": affected})
        except asyncpg.QueryCanceledError:
            raise ToolFailed("statement timed out; nothing was changed") from None
        except (ToolRejected, ToolFailed):
            raise
        except Exception as exc:
            raise sanitized_db_error(exc) from None

    async def execute_ddl(self, sql: str) -> str:
        statement = render(validate_ddl(sql, self._schemas), "postgres")

        async def run(conn: Conn) -> str:
            await conn.execute(f"SET LOCAL ROLE {DDL_OWNER_ROLE}")
            await conn.execute(statement)
            return dumps({"status": "committed"})

        return await self._run("writer", run, readonly=False)
