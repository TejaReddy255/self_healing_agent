from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.db.settings import DatabaseSettings
from tests.conftest import auth
from tests.fakes.airflow import PASSWORD, canonical_scenario, fake_airflow, fake_settings
from tests.integration.conftest import connect

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

DAG = "customer_daily_load"


def airflow_audit_rows(db: DatabaseSettings) -> list[dict[str, Any]]:
    async def fetch() -> list[dict[str, Any]]:
        conn = await connect(db)
        try:
            rows = await conn.fetch(
                "SELECT event_type, status, actor_subject, actor_role, dag_id, run_id, duration_ms,"
                " input, output, details FROM operation_audit WHERE phase = 'airflow' ORDER BY id"
            )
        finally:
            await conn.close()
        return [dict(r) for r in rows]

    return asyncio.run(fetch())


@pytest.mark.spec("S05-AC6")
def test_every_operator_action_attempt_is_audited(
    migrated_db: DatabaseSettings, make_settings: Callable[..., Settings]
) -> None:
    scenario = canonical_scenario()
    with fake_airflow(scenario):
        app = create_app(
            make_settings(),
            db_settings=migrated_db,
            airflow_settings=fake_settings(airflow_get_retries=0),
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            created = client.post(f"/airflow/dags/{DAG}/runs", json={}, headers=auth("operator"))
            assert created.status_code == 201
            assert (
                client.patch(
                    f"/airflow/dags/{DAG}", json={"is_paused": True}, headers=auth("operator")
                ).status_code
                == 200
            )
            assert (
                client.patch(
                    f"/airflow/dags/{DAG}", json={"is_paused": False}, headers=auth("admin")
                ).status_code
                == 200
            )
            scenario.fail_next("POST", "/dagRuns", 503)
            failed = client.post(f"/airflow/dags/{DAG}/runs", json={}, headers=auth("operator"))
            assert failed.status_code == 503
            # A role denial (403) happens before the service: no audit row (S23 adds a log).
            assert (
                client.post(
                    f"/airflow/dags/{DAG}/runs", json={}, headers=auth("viewer")
                ).status_code
                == 403
            )
            assert (
                client.post(
                    "/airflow/dags/unregistered/runs", json={}, headers=auth("operator")
                ).status_code
                == 404
            )

    rows = airflow_audit_rows(migrated_db)
    assert [(r["event_type"], r["status"], r["actor_subject"], r["actor_role"]) for r in rows] == [
        ("AIRFLOW_TRIGGER", "OK", "operator@test", "operator"),
        ("AIRFLOW_PAUSE", "OK", "operator@test", "operator"),
        ("AIRFLOW_RESUME", "OK", "admin@test", "admin"),
        ("AIRFLOW_TRIGGER", "HTTP_503", "operator@test", "operator"),
        # Unregistered DAG: refused before any Airflow call, but the attempt is recorded.
        ("AIRFLOW_TRIGGER", "DENIED", "operator@test", "operator"),
    ]
    assert [r["dag_id"] for r in rows] == [DAG, DAG, DAG, DAG, "unregistered"]
    assert all(r["duration_ms"] is not None for r in rows)
    assert rows[0]["run_id"] == created.json()["dag_run_id"]
    assert rows[3]["run_id"].startswith("manual__console__")
    assert all(PASSWORD not in str(r) for r in rows)
