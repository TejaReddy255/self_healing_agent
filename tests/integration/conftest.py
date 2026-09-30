"""Integration-test harness (S02): real PostgreSQL from ``deploy/docker-compose.dev.yml``.

Each test that needs a database gets its own throwaway database (created and dropped here),
so tests are independent and never touch the developer's data. Connection settings come from
the repository ``.env`` (or PG_* variables in CI). When the server is unreachable the tests are
skipped locally, but **fail** when ``REQUIRE_INTEGRATION_DB=1`` (CI), so a missing database can
never masquerade as a pass.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import asyncpg
import pytest

from backend.db.migrations import discover, migrate_up
from backend.db.settings import DEFAULT_MIGRATIONS_DIR, DatabaseSettings

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_HINT = (
    "start it with: docker compose --env-file .env -f deploy/docker-compose.dev.yml up -d"
)


def _skip_or_fail(message: str) -> None:
    if os.environ.get("REQUIRE_INTEGRATION_DB") == "1":
        pytest.fail(message)
    pytest.skip(message)


async def _admin(settings: DatabaseSettings, statement: str) -> None:
    conn = await asyncpg.connect(**settings.connect_kwargs(application_name="tests"))
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def base_db_settings() -> DatabaseSettings:
    env_file = REPO_ROOT / ".env"
    settings = DatabaseSettings(_env_file=env_file if env_file.exists() else None)
    if not settings.configured:
        _skip_or_fail("PG_PASSWORD is not set (in .env or the environment)")
    try:
        asyncio.run(_admin(settings, "SELECT 1"))
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        _skip_or_fail(f"PostgreSQL unreachable at {settings.describe()} ({exc!r}); {COMPOSE_HINT}")
    return settings


@pytest.fixture
def make_database(base_db_settings: DatabaseSettings) -> Iterator[Callable[[], DatabaseSettings]]:
    """Factory creating empty, uniquely named databases; all are dropped after the test."""
    created: list[str] = []

    def factory() -> DatabaseSettings:
        name = f"sh_test_{uuid.uuid4().hex[:12]}"
        asyncio.run(_admin(base_db_settings, f'CREATE DATABASE "{name}"'))
        created.append(name)
        return base_db_settings.model_copy(update={"pg_database": name})

    yield factory
    for name in created:
        asyncio.run(_admin(base_db_settings, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def empty_db(make_database: Callable[[], DatabaseSettings]) -> DatabaseSettings:
    return make_database()


@pytest.fixture
def migrated_db(make_database: Callable[[], DatabaseSettings]) -> DatabaseSettings:
    settings = make_database()

    async def migrate() -> None:
        conn = await asyncpg.connect(**settings.connect_kwargs(application_name="tests"))
        try:
            await migrate_up(conn, discover(DEFAULT_MIGRATIONS_DIR))
        finally:
            await conn.close()

    asyncio.run(migrate())
    return settings


async def connect(settings: DatabaseSettings) -> asyncpg.Connection[asyncpg.Record]:
    return await asyncpg.connect(**settings.connect_kwargs(application_name="tests"))
