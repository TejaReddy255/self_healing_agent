"""Async PostgreSQL access (S02, asyncpg per amended ADR-002).

The pool is created lazily (``min_size=0``) so the API starts even while the database is down;
readiness then reports ``db: down`` instead of the process crashing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from asyncpg.pool import PoolConnectionProxy

from backend.db.settings import DatabaseSettings
from backend.observability.readiness import ComponentStatus

logger = logging.getLogger(__name__)

# Repositories accept either a direct connection (migration CLI) or a pooled one (API).
# A lazily evaluated `type` alias: asyncpg's classes are generic only in their type stubs.
type Connection = asyncpg.Connection[asyncpg.Record] | PoolConnectionProxy[asyncpg.Record]


def _encode_json(value: object) -> str:
    # PostgreSQL JSON rejects NaN/Infinity: fail here with a clear error instead of in the server.
    return json.dumps(value, allow_nan=False)


async def _init_connection(conn: asyncpg.Connection[asyncpg.Record]) -> None:
    for type_name in ("json", "jsonb"):
        await conn.set_type_codec(
            type_name, encoder=_encode_json, decoder=json.loads, schema="pg_catalog"
        )


class DatabaseUnavailableError(RuntimeError):
    """The pool is not open or the database cannot be reached (maps to HTTP 503)."""


# Failures while *obtaining* a connection: server down, refused, auth, starting up, pool closed.
_ACQUIRE_ERRORS = (OSError, TimeoutError, asyncpg.InterfaceError, asyncpg.PostgresError)


class Database:
    def __init__(
        self, settings: DatabaseSettings, *, application_name: str = "self-healing"
    ) -> None:
        self.settings = settings
        self._application_name = application_name
        self._pool: asyncpg.Pool[asyncpg.Record] | None = None

    async def open(self) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            min_size=0,
            max_size=self.settings.pg_pool_max,
            init=_init_connection,
            **self.settings.connect_kwargs(application_name=self._application_name),
        )
        logger.info("database pool opened", extra={"database": self.settings.describe()})

    async def close(self) -> None:
        if self._pool is None:
            return
        pool, self._pool = self._pool, None
        try:
            await asyncio.wait_for(pool.close(), timeout=10)
        except TimeoutError:
            logger.warning("database pool did not close in time; terminating connections")
            pool.terminate()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Connection]:
        """Borrow a pooled connection. Failures to *obtain* one become DatabaseUnavailableError;
        errors raised by the caller's queries propagate unchanged."""
        pool = self._pool
        if pool is None:
            raise DatabaseUnavailableError("database pool is not open")
        try:
            conn = await pool.acquire(timeout=self.settings.pg_acquire_timeout_s)
        except _ACQUIRE_ERRORS as exc:
            raise DatabaseUnavailableError(
                f"cannot obtain a database connection: {type(exc).__name__}"
            ) from exc
        try:
            yield conn
        finally:
            await pool.release(conn)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Connection]:
        async with self.connection() as conn, conn.transaction():
            yield conn

    async def check(self) -> ComponentStatus:
        """Readiness check: a round trip to the server."""
        try:
            async with self.connection() as conn:
                await conn.fetchval("SELECT 1")
        except DatabaseUnavailableError as exc:
            cause = type(exc.__cause__).__name__ if exc.__cause__ else "PoolClosed"
            return ComponentStatus(status="down", detail=f"unreachable: {cause}")
        except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
            return ComponentStatus(status="down", detail=f"unreachable: {type(exc).__name__}")
        return ComponentStatus(status="ok")
