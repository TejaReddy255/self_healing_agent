"""Fixtures for the database MCP servers (S07) against the dev data containers.

Settings are read once per session from ``.env`` (or the CI environment), before the per-test
environment isolation runs. The canonical data is (re)seeded once per session; tests that write use
their own scratch tables, created and dropped here as the owning admin, so the canonical counts are
never disturbed. Without the containers the tests skip locally and fail in CI
(``REQUIRE_INTEGRATION_DB=1``).
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import warnings
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import unquote

import aiomysql  # type: ignore[import-untyped]
import asyncpg
import pytest
from pydantic import ValidationError

from mcp_servers.common.settings import MySQLServerSettings, PostgresServerSettings
from mcp_servers.mysql.tools import MySQLTools
from mcp_servers.postgres.tools import PostgresTools

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env"
HINT = (
    "start them with: docker compose --env-file .env -f deploy/docker-compose.dev.yml up -d"
    " sales-db warehouse-db"
)
SCRATCH_ROWS = 50


def skip_or_fail(message: str) -> None:
    if os.environ.get("REQUIRE_INTEGRATION_DB") == "1":
        pytest.fail(message)
    pytest.skip(message)


def _seed_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "canonical_seed", REPO_ROOT / "deploy/seed/canonical.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # pydantic resolves the deferred annotations through it
    spec.loader.exec_module(module)
    return module


SEED = _seed_module()


def _env_file() -> Path | None:
    return ENV_FILE if ENV_FILE.exists() else None


@pytest.fixture(scope="session")
def admin() -> Any:
    try:
        return SEED.SeedSettings(_env_file=_env_file())
    except ValidationError:
        skip_or_fail("WAREHOUSE_PG_PASSWORD / MYSQL_ROOT_PASSWORD are not set")


@pytest.fixture(scope="session")
def pg_settings(admin: Any) -> PostgresServerSettings:
    settings = PostgresServerSettings(_env_file=_env_file())
    if settings.mcp_postgres_reader_dsn is None or settings.mcp_postgres_writer_dsn is None:
        skip_or_fail("MCP_POSTGRES_READER_DSN / MCP_POSTGRES_WRITER_DSN are not set")
    return settings


@pytest.fixture(scope="session")
def my_settings(admin: Any) -> MySQLServerSettings:
    settings = MySQLServerSettings(_env_file=_env_file())
    if settings.mcp_mysql_reader_dsn is None or settings.mcp_mysql_writer_dsn is None:
        skip_or_fail("MCP_MYSQL_READER_DSN / MCP_MYSQL_WRITER_DSN are not set")
    return settings


@pytest.fixture(scope="session")
def seeded(
    admin: Any, pg_settings: PostgresServerSettings, my_settings: MySQLServerSettings
) -> None:
    """Run the real seed script once: proves it works and resets the canonical data."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv: this interpreter + repo script
            [sys.executable, str(REPO_ROOT / "deploy/seed/canonical.py")],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        skip_or_fail("seeding timed out")
        return
    if result.returncode != 0:
        # Only the exception class line: never echo connection details.
        last = (result.stderr.strip().splitlines() or ["?"])[-1].split(":")[0]
        skip_or_fail(f"seeding failed ({last}); {HINT}")


async def pg_admin(admin: Any) -> asyncpg.Connection:
    return await asyncpg.connect(
        host="127.0.0.1",
        port=admin.warehouse_pg_port,
        user="warehouse_admin",
        password=admin.warehouse_pg_password.get_secret_value(),
        database="warehouse",
        timeout=10,
    )


async def my_admin(admin: Any) -> Any:
    return await aiomysql.connect(
        host="127.0.0.1",
        port=admin.mysql_port,
        user="root",
        password=admin.mysql_root_password.get_secret_value(),
        db="sales",
        autocommit=True,
        connect_timeout=10,
    )


@pytest.fixture
def pg_scratch(admin: Any, seeded: None) -> Iterator[str]:
    """``dw.mcp_scratch`` owned by dw_owner (so the writer has DML grants), 50 rows."""

    async def setup() -> None:
        conn = await pg_admin(admin)
        try:
            await conn.execute("SET ROLE dw_owner")
            await conn.execute("DROP TABLE IF EXISTS dw.mcp_scratch")
            await conn.execute("CREATE TABLE dw.mcp_scratch (id INT PRIMARY KEY, note TEXT)")
            await conn.execute(
                "INSERT INTO dw.mcp_scratch SELECT g, 'n' || g FROM generate_series(1, $1::int) g",
                SCRATCH_ROWS,
            )
        finally:
            await conn.close()

    async def teardown() -> None:
        conn = await pg_admin(admin)
        try:
            await conn.execute("DROP TABLE IF EXISTS dw.mcp_scratch")
            await conn.execute("DROP TABLE IF EXISTS dw.sales_daily_2026_10")
        finally:
            await conn.close()

    asyncio.run(setup())
    yield "dw.mcp_scratch"
    asyncio.run(teardown())


@pytest.fixture
def my_scratch(admin: Any, seeded: None) -> Iterator[str]:
    async def run(statements: list[str]) -> None:
        conn = await my_admin(admin)
        try:
            async with conn.cursor() as cur:
                for statement in statements:
                    with warnings.catch_warnings():  # "Unknown table" on DROP IF EXISTS
                        warnings.simplefilter("ignore")
                        await cur.execute(statement)
        finally:
            conn.close()

    values = ", ".join(f"({i}, 'n{i}')" for i in range(1, SCRATCH_ROWS + 1))
    asyncio.run(
        run(
            [
                "DROP TABLE IF EXISTS sales.mcp_scratch",
                "CREATE TABLE sales.mcp_scratch (id INT PRIMARY KEY, note VARCHAR(20),"
                " UNIQUE KEY uq_note_id (note, id))",
                f"INSERT INTO sales.mcp_scratch VALUES {values}",  # noqa: S608
            ]
        )
    )
    yield "sales.mcp_scratch"
    asyncio.run(run(["DROP TABLE IF EXISTS sales.mcp_scratch"]))


@pytest.fixture
async def pg_tools(
    pg_settings: PostgresServerSettings, seeded: None
) -> AsyncIterator[PostgresTools]:
    tools = PostgresTools(pg_settings)
    yield tools
    await tools.close()


@pytest.fixture
async def my_tools(my_settings: MySQLServerSettings, seeded: None) -> AsyncIterator[MySQLTools]:
    tools = MySQLTools(my_settings)
    yield tools
    await tools.close()


def secrets_of(*settings: PostgresServerSettings | MySQLServerSettings) -> list[str]:
    """Every password and DSN a test must never see in output, errors or logs."""
    found: list[str] = []
    for item in settings:
        for value in item.model_dump().values():
            secret = getattr(value, "get_secret_value", None)
            if secret:
                dsn = secret()
                found.append(dsn)
                password = dsn.split("://", 1)[-1].split("@", 1)[0].split(":", 1)[-1]
                if password:
                    found.extend({password, unquote(password)})
    return found
