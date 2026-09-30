from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.db.settings import DEFAULT_MIGRATIONS_DIR, DatabaseSettings
from tests.conftest import auth
from tests.integration.conftest import connect

pytestmark = [pytest.mark.integration, pytest.mark.spec("S02-AC4")]

# Every relation (table, index, sequence, view...) outside the system schemas.
SNAPSHOT_SQL = """
SELECT count(*) AS relations,
       coalesce(md5(string_agg(n.nspname || '.' || c.relname || ':' || c.relkind::text, ','
                               ORDER BY n.nspname, c.relname)), '') AS digest
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'
"""


def catalog_snapshot(settings: DatabaseSettings) -> tuple[int, str]:
    async def run() -> tuple[int, str]:
        conn = await connect(settings)
        try:
            row = await conn.fetchrow(SNAPSHOT_SQL)
            assert row is not None
            return int(row["relations"]), str(row["digest"])
        finally:
            await conn.close()

    return asyncio.run(run())


def readiness(settings: Settings, db: DatabaseSettings) -> tuple[int, dict[str, object]]:
    with TestClient(create_app(settings, db_settings=db)) as client:
        response = client.get("/health/ready", headers=auth("viewer"))
    return response.status_code, response.json()


def test_startup_runs_no_ddl_and_reports_uninitialised_schema(
    empty_db: DatabaseSettings, make_settings: Callable[..., Settings]
) -> None:
    before = catalog_snapshot(empty_db)
    status, body = readiness(make_settings(), empty_db)
    after = catalog_snapshot(empty_db)

    assert before == after == (0, ""), (
        "the app must not create anything, not even schema_migrations"
    )
    assert status == 503
    assert body["components"] == {
        "db": {"status": "ok", "detail": None},
        "migrations": {
            "status": "down",
            "detail": "not initialised: run python -m backend.db.migrate up",
        },
    }


def test_ready_when_all_migrations_applied(
    migrated_db: DatabaseSettings, make_settings: Callable[..., Settings]
) -> None:
    before = catalog_snapshot(migrated_db)
    status, body = readiness(make_settings(), migrated_db)
    assert catalog_snapshot(migrated_db) == before
    assert status == 200
    assert body["status"] == "ok"
    assert body["components"]["migrations"] == {"status": "ok", "detail": "1 applied"}


def test_pending_migration_makes_service_not_ready(
    migrated_db: DatabaseSettings, make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    migrations = tmp_path / "migrations"
    shutil.copytree(DEFAULT_MIGRATIONS_DIR, migrations)
    (migrations / "0002_future.sql").write_text("CREATE TABLE future (id int);", encoding="utf-8")
    status, body = readiness(
        make_settings(), migrated_db.model_copy(update={"migrations_dir": migrations})
    )
    assert status == 503
    assert body["components"]["migrations"] == {
        "status": "down",
        "detail": "1 pending migration(s)",
    }


def test_database_down_makes_service_not_ready(
    base_db_settings: DatabaseSettings, make_settings: Callable[..., Settings]
) -> None:
    # Port 1 on localhost refuses connections immediately: the API must still start.
    unreachable = base_db_settings.model_copy(update={"pg_port": 1, "pg_connect_timeout_s": 2.0})
    status, body = readiness(make_settings(), unreachable)
    assert status == 503
    components = body["components"]
    assert isinstance(components, dict)
    assert components["db"]["status"] == "down"
    assert components["db"]["detail"].startswith("unreachable: ")
    assert components["migrations"]["status"] == "down"
    assert base_db_settings.pg_password is not None
    assert base_db_settings.pg_password.get_secret_value() not in str(body)
