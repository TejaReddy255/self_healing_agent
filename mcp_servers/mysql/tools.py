"""MySQL MCP tools (S07), using aiomysql (pure Python: no native DLLs on Windows).

Reads use the SELECT-only reader user inside ``START TRANSACTION READ ONLY`` with
``MAX_EXECUTION_TIME``; writes use the writer user in a transaction rolled back when more rows
match than the caller allowed. MySQL has no server-side timeout for DML, so a write is bounded on
the client side and an over-long statement is stopped with ``KILL QUERY``.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar
from urllib.parse import unquote, urlsplit

import aiomysql  # type: ignore[import-untyped]
from pymysql.constants import CLIENT  # type: ignore[import-untyped]

from mcp_servers.common import duplicates
from mcp_servers.common.errors import ToolFailed, ToolRejected, sanitized_db_error
from mcp_servers.common.identifiers import check_identifier, qualified, split_table
from mcp_servers.common.pii import PiiPolicy
from mcp_servers.common.results import dumps, table_result
from mcp_servers.common.settings import MySQLServerSettings
from mcp_servers.common.sql import cap_rows, render, validate_dml, validate_select

logger = logging.getLogger(__name__)

T = TypeVar("T")
ER_QUERY_TIMEOUT = 3024
# Pinned per connection: regenerated SQL assumes backslash escapes and backtick-only identifiers
# (no ANSI_QUOTES, no NO_BACKSLASH_ESCAPES), whatever the server default is.
SQL_MODE = (
    "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
    "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION"
)
COMMIT_TIMEOUT_S = 10.0
COM_SET_OPTION = 0x1B
MULTI_STATEMENTS_OFF = 1


def _connect_args(dsn: str) -> dict[str, Any]:
    parts = urlsplit(dsn)
    if parts.scheme not in ("mysql", "mysql+aiomysql") or not parts.hostname:
        raise ToolFailed("invalid MySQL DSN")
    return {
        "host": parts.hostname,
        "port": parts.port or 3306,
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        # No default database: every table must be schema-qualified, so an unqualified name the
        # validator missed fails in MySQL instead of resolving to sales.<name>.
        "db": None,
    }


async def _single_statement_only(conn: Any) -> None:
    """Turn the protocol's multi-statement support off for this connection.

    aiomysql always negotiates CLIENT.MULTI_STATEMENTS; validation already allows exactly one
    statement, and this makes the server refuse a second one as well. aiomysql has no public call
    for COM_SET_OPTION, hence the private methods (pinned by the integration tests).
    """
    await conn._execute_command(COM_SET_OPTION, struct.pack("<H", MULTI_STATEMENTS_OFF))
    packet = await conn._read_packet()
    if not (packet.is_ok_packet() or packet.is_eof_packet()):
        raise ToolFailed("could not disable multi-statements")


class MySQLTools:
    name = "mysql"
    supports_partitions = False
    supports_ddl = False

    def __init__(self, settings: MySQLServerSettings) -> None:
        self._settings = settings
        self._schemas = settings.mcp_mysql_schemas
        self._pii = PiiPolicy(settings.mcp_pii_columns)
        self._pools: dict[str, aiomysql.Pool] = {}
        self._lock = asyncio.Lock()

    @property
    def has_writer(self) -> bool:
        return self._settings.mcp_mysql_writer_dsn is not None

    @property
    def _timeout_s(self) -> float:
        return self._settings.mcp_statement_timeout_ms / 1000 + 5

    async def _pool(self, role: str) -> aiomysql.Pool:
        dsn = (
            self._settings.mcp_mysql_reader_dsn
            if role == "reader"
            else self._settings.mcp_mysql_writer_dsn
        )
        if dsn is None:
            raise ToolFailed(f"no {role} credentials configured")
        async with self._lock:
            if role not in self._pools:
                try:
                    self._pools[role] = await aiomysql.create_pool(
                        minsize=0,
                        maxsize=4,
                        autocommit=False,
                        connect_timeout=5,
                        charset="utf8mb4",
                        local_infile=False,
                        # rowcount = rows matched, not just changed: the ceiling stays conservative.
                        client_flag=CLIENT.FOUND_ROWS,
                        **_connect_args(dsn.get_secret_value()),
                    )
                except ToolFailed:
                    raise
                except Exception as exc:
                    raise sanitized_db_error(exc) from None
        return self._pools[role]

    async def close(self) -> None:
        async with self._lock:
            pools, self._pools = list(self._pools.values()), {}
        for pool in pools:
            pool.close()
            await pool.wait_closed()

    @asynccontextmanager
    async def _connection(self, role: str) -> AsyncIterator[Any]:
        """A pooled connection that is closed, never reused, after any error or cancellation.

        MySQL's START TRANSACTION commits an open transaction implicitly, so a connection left in
        an unknown state (a timed-out statement, a cancelled call) must not go back to the pool.
        Closing it makes the server roll back whatever was uncommitted.
        """
        pool = await self._pool(role)
        try:
            conn = await asyncio.wait_for(pool.acquire(), 5)
        except TimeoutError:
            raise ToolFailed(f"no {role} connection available") from None
        clean = False
        try:
            if not getattr(conn, "_mcp_prepared", False):
                await _single_statement_only(conn)
                async with conn.cursor() as cur:
                    await cur.execute("SET SESSION sql_mode = %s", (SQL_MODE,))
                await conn.commit()
                conn._mcp_prepared = True
            yield conn
            clean = True
        finally:
            if not clean:
                conn.close()
            pool.release(conn)

    def _db_error(self, exc: BaseException, timeout_message: str) -> ToolFailed:
        if isinstance(exc, TimeoutError):
            return ToolFailed(timeout_message)
        if getattr(exc, "args", (None,))[:1] == (ER_QUERY_TIMEOUT,):
            return ToolFailed(timeout_message)
        logger.warning("mysql tool failed", extra={"error": type(exc).__name__})
        return sanitized_db_error(exc)

    async def _read(self, fn: Callable[[Any], Awaitable[T]]) -> T:
        async def run() -> T:
            async with self._connection("reader") as conn, conn.cursor() as cur:
                await cur.execute(
                    "SET SESSION MAX_EXECUTION_TIME = %s",
                    (self._settings.mcp_statement_timeout_ms,),
                )
                await cur.execute("START TRANSACTION READ ONLY")
                result = await fn(cur)
                await conn.rollback()  # reads never commit anything
                return result

        try:
            return await asyncio.wait_for(run(), self._timeout_s)
        except (ToolRejected, ToolFailed):
            raise
        except Exception as exc:
            raise self._db_error(exc, "statement timed out") from None

    async def _require_table(self, cur: Any, value: str) -> tuple[str, str]:
        schema, table = split_table(value, self._schemas)
        await cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
            (schema, table),
        )
        if not await cur.fetchone():
            raise ToolRejected(f"table {schema}.{table} does not exist")
        return schema, table

    async def _columns(self, cur: Any, schema: str, table: str) -> list[tuple[Any, ...]]:
        await cur.execute(
            "SELECT column_name, data_type, is_nullable, column_key FROM information_schema.columns"
            " WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, table),
        )
        return list(await cur.fetchall())

    # --- read tools -----------------------------------------------------------------------

    async def ping(self) -> bool:
        async def run(cur: Any) -> bool:
            await cur.execute("SELECT 1")
            return bool(await cur.fetchone())

        try:
            return await self._read(run)
        except Exception:
            return False

    async def list_schemas(self) -> str:
        async def run(cur: Any) -> str:
            await cur.execute("SELECT schema_name FROM information_schema.schemata ORDER BY 1")
            names = [r[0] for r in await cur.fetchall() if str(r[0]).lower() in self._schemas]
            return dumps({"schemas": names})

        return await self._read(run)

    async def list_tables(self, schema: str) -> str:
        name = check_identifier(schema, "schema")
        if name.lower() not in self._schemas:
            raise ToolRejected(f"schema {schema!r} is not available on this server")

        async def run(cur: Any) -> str:
            await cur.execute(
                "SELECT table_name, table_type FROM information_schema.tables"
                " WHERE table_schema = %s ORDER BY 1",
                (name,),
            )
            return dumps({"tables": [{"name": r[0], "type": r[1]} for r in await cur.fetchall()]})

        return await self._read(run)

    async def _unique_keys(self, cur: Any, schema: str, table: str) -> list[list[str]]:
        await cur.execute(
            "SELECT index_name, column_name FROM information_schema.statistics"
            " WHERE table_schema = %s AND table_name = %s AND non_unique = 0"
            " AND index_name <> 'PRIMARY' ORDER BY index_name, seq_in_index",
            (schema, table),
        )
        keys: dict[str, list[str]] = {}
        for index_name, column in await cur.fetchall():
            keys.setdefault(index_name, []).append(column)
        return list(keys.values())

    async def describe_table(self, table: str) -> str:
        async def run(cur: Any) -> str:
            schema, name = await self._require_table(cur, table)
            columns = await self._columns(cur, schema, name)
            unique = await self._unique_keys(cur, schema, name)
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
                    "primary_key": [c[0] for c in columns if c[3] == "PRI"],
                    "unique": unique,
                }
            )

        return await self._read(run)

    async def run_select(self, sql: str) -> str:
        tree = validate_select(sql, "mysql", self._schemas)
        self._pii.check_select(tree)
        limit = self._settings.mcp_row_limit
        statement = render(cap_rows(tree, limit + 1), "mysql")  # +1 detects truncation

        async def run(cur: Any) -> str:
            await cur.execute(statement)
            columns = [d[0] for d in cur.description or ()]
            rows = await cur.fetchmany(limit + 1)
            return table_result(columns, list(rows), limit)

        return await self._read(run)

    async def get_row_count(self, table: str) -> str:
        async def run(cur: Any) -> str:
            schema, name = await self._require_table(cur, table)
            source = qualified(schema, name, "mysql")  # validated, quoted identifiers
            await cur.execute(f"SELECT count(*) FROM {source}")  # noqa: S608
            (count,) = await cur.fetchone()
            return dumps({"table": f"{schema}.{name}", "row_count": int(count)})

        return await self._read(run)

    async def find_duplicates(self, table: str, key_columns: list[str]) -> str:
        keys = duplicates.check_key_columns(key_columns, lower=False)

        async def run(cur: Any) -> str:
            schema, name = await self._require_table(cur, table)
            existing = {str(c[0]).lower() for c in await self._columns(cur, schema, name)}
            if missing := [k for k in keys if k.lower() not in existing]:
                raise ToolRejected(f"unknown column(s): {', '.join(missing)}")
            summary_sql, sample_sql = duplicates.queries(
                keys, qualified(schema, name, "mysql"), "mysql"
            )
            await cur.execute(summary_sql)
            summary = await cur.fetchone()
            await cur.execute(sample_sql)
            sample = await cur.fetchall()
            masked = {k for k in keys if self._pii.is_pii(schema, name, k)}
            return duplicates.result(f"{schema}.{name}", keys, summary, sample, masked)

        return await self._read(run)

    async def list_partitions(self, parent_table: str) -> str:
        raise ToolRejected("partition listing is not supported on MySQL")

    # --- write tools ----------------------------------------------------------------------

    async def execute_dml(self, sql: str, max_affected_rows: int) -> str:
        ceiling = self._settings.mcp_max_affected_rows
        if not isinstance(max_affected_rows, int) or not 1 <= max_affected_rows <= ceiling:
            raise ToolRejected(f"max_affected_rows must be between 1 and {ceiling}")
        statement = render(validate_dml(sql, "mysql", self._schemas), "mysql")

        timeout_s = self._settings.mcp_statement_timeout_ms / 1000

        try:
            async with self._connection("writer") as conn, conn.cursor() as cur:
                await cur.execute("SET SESSION innodb_lock_wait_timeout = 10")
                await cur.execute("START TRANSACTION")
                try:
                    await asyncio.wait_for(cur.execute(statement), timeout_s)
                except TimeoutError:
                    # Closing the socket does not stop the statement; KILL QUERY does. Nothing was
                    # committed, and the closed connection rolls the transaction back.
                    await self._kill_query(conn.thread_id())
                    raise ToolFailed("statement timed out; nothing was committed") from None
                affected = max(int(cur.rowcount), 0)
                if affected > max_affected_rows:
                    await conn.rollback()
                    return dumps(
                        {
                            "status": "aborted",
                            "affected_rows": affected,
                            "max_affected_rows": max_affected_rows,
                        }
                    )
                try:
                    await asyncio.wait_for(conn.commit(), COMMIT_TIMEOUT_S)
                except Exception:
                    # The server may have committed before the reply was lost (invariant 6).
                    raise ToolFailed(
                        "commit outcome unknown: check the data before retrying"
                    ) from None
                return dumps({"status": "committed", "affected_rows": affected})
        except (ToolRejected, ToolFailed):
            raise
        except Exception as exc:
            raise self._db_error(exc, "statement timed out; nothing was committed") from None

    async def _kill_query(self, thread_id: int) -> None:
        """Stop a running statement from a separate, short-lived connection (best effort)."""
        dsn = self._settings.mcp_mysql_writer_dsn
        if dsn is None:
            return
        try:
            conn = await aiomysql.connect(
                connect_timeout=5, autocommit=True, **_connect_args(dsn.get_secret_value())
            )
            try:
                async with conn.cursor() as cur:
                    await asyncio.wait_for(cur.execute("KILL QUERY %s", (int(thread_id),)), 5)
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("mysql kill query failed", extra={"error": type(exc).__name__})

    async def execute_ddl(self, sql: str) -> str:
        raise ToolRejected("DDL is not available on MySQL")
