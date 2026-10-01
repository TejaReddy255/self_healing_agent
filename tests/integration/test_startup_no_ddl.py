from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.db.migrations import discover
from backend.db.settings import DEFAULT_MIGRATIONS_DIR, DatabaseSettings
from tests.conftest import auth
from tests.integration.conftest import connect

pytestmark = [pytest.mark.integration, pytest.mark.spec("S02-AC4")]

# An event trigger fires for every DDL command (CREATE/ALTER/DROP/COMMENT/GRANT...) in the
# database, so any schema change the app makes is recorded, not just new relations.
# Event triggers require a superuser; the dev and CI Postgres containers run tests as one.
DDL_RECORDER = """
CREATE TABLE ddl_log (id serial PRIMARY KEY, tag text NOT NULL, app text);
CREATE FUNCTION record_ddl() RETURNS event_trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO ddl_log (tag, app) VALUES (tg_tag, current_setting('application_name'));
END;
$$;
CREATE EVENT TRIGGER record_all_ddl ON ddl_command_start EXECUTE FUNCTION record_ddl();
"""


def run_sql(settings: DatabaseSettings, sql: str) -> list[dict[str, object]]:
    async def run() -> list[dict[str, object]]:
        conn = await connect(settings)
        try:
            if sql.lstrip().upper().startswith("SELECT"):
                return [dict(r) for r in await conn.fetch(sql)]
            await conn.execute(sql)
            return []
        finally:
            await conn.close()

    return asyncio.run(run())


def readiness(settings: Settings, db: DatabaseSettings) -> tuple[int, dict[str, object]]:
    with TestClient(create_app(settings, db_settings=db)) as client:
        response = client.get("/health/ready", headers=auth("viewer"))
    return response.status_code, response.json()


def recorded_ddl(db: DatabaseSettings) -> list[dict[str, object]]:
    return run_sql(db, "SELECT tag, app FROM ddl_log ORDER BY id")


def test_startup_runs_no_ddl_on_an_empty_database(
    empty_db: DatabaseSettings, make_settings: Callable[..., Settings]
) -> None:
    run_sql(empty_db, DDL_RECORDER)
    status, body = readiness(make_settings(), empty_db)

    assert recorded_ddl(empty_db) == [], (
        "the app must not create anything, not even schema_migrations"
    )
    assert status == 503
    components = body["components"]
    assert components["db"] == {"status": "ok", "detail": None}
    assert components["migrations"] == {
        "status": "down",
        "detail": "not initialised: run python -m backend.db.migrate up",
    }
    # Later slices add components (S04: metadata); none may mask the database ones.
    assert set(components) >= {"db", "migrations"}


def test_ready_when_all_migrations_applied(
    migrated_db: DatabaseSettings, make_settings: Callable[..., Settings]
) -> None:
    run_sql(migrated_db, DDL_RECORDER)
    status, body = readiness(make_settings(), migrated_db)

    assert recorded_ddl(migrated_db) == []
    assert status == 200
    # Airflow is unconfigured here, so overall readiness is "degraded" (S05), never "down".
    assert body["status"] in ("ok", "degraded")
    assert body["components"]["db"] == {"status": "ok", "detail": None}
    expected = f"{len(discover(DEFAULT_MIGRATIONS_DIR))} applied"
    assert body["components"]["migrations"] == {"status": "ok", "detail": expected}


def test_recorder_really_detects_ddl(empty_db: DatabaseSettings) -> None:
    """Guards the guard: prove the event trigger records a column addition."""
    run_sql(empty_db, DDL_RECORDER)
    run_sql(empty_db, "ALTER TABLE ddl_log ADD COLUMN extra int")
    assert [row["tag"] for row in recorded_ddl(empty_db)] == ["ALTER TABLE"]


def test_pending_migration_makes_service_not_ready(
    migrated_db: DatabaseSettings, make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    migrations = tmp_path / "migrations"
    shutil.copytree(DEFAULT_MIGRATIONS_DIR, migrations)
    next_version = max(m.version for m in discover(DEFAULT_MIGRATIONS_DIR)) + 1
    (migrations / f"{next_version:04d}_future.sql").write_text(
        "CREATE TABLE future (id int);", encoding="utf-8"
    )
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
