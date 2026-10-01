from __future__ import annotations

import logging
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from tests.conftest import auth
from tests.fakes.airflow import (
    PASSWORD,
    FakeAirflow,
    canonical_scenario,
    fake_airflow,
    fake_settings,
)

FAILED_RUN = "scheduled__2026-10-01T02:00:00+00:00"
OLDER_RUN = "scheduled__2026-09-30T02:00:00+00:00"


@pytest.fixture
def scenario() -> FakeAirflow:
    return canonical_scenario()


@pytest.fixture
def client(make_settings: Callable[..., Settings], scenario: FakeAirflow) -> Iterator[TestClient]:
    with fake_airflow(scenario):
        app = create_app(make_settings(), airflow_settings=fake_settings(airflow_get_retries=0))
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client


def context(client: TestClient, **params: str) -> dict[str, object]:
    response = client.get(
        "/pipelines/customer_daily_load/context", params=params, headers=auth("viewer")
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


@pytest.mark.spec("S05-AC1")
def test_context_combines_static_metadata_and_live_airflow(client: TestClient) -> None:
    body = context(client)
    assert body["static_metadata"]["airflow"]["dag_id"] == "customer_daily_load"  # type: ignore[index]
    assert body["metadata_version"]
    airflow = body["airflow"]
    assert isinstance(airflow, dict)
    assert airflow["source"] == "airflow" and airflow["fetched_at"]
    assert airflow["unavailable"] is False
    assert [r["dag_run_id"] for r in airflow["runs"]] == [FAILED_RUN, OLDER_RUN]
    assert airflow["selected_run"]["dag_run_id"] == FAILED_RUN
    assert airflow["dag"]["next_dagrun"]
    assert {t["task_id"]: t["state"] for t in airflow["task_instances"]} == {
        "dq_target_check": "upstream_failed",
        "extract_customers": "success",
        "load_customers": "failed",
    }
    assert [t["task_id"] for t in airflow["failed_tasks"]] == ["dq_target_check", "load_customers"]


@pytest.mark.spec("S05-AC1")
def test_run_id_selects_a_run_and_unknown_run_is_reported(client: TestClient) -> None:
    older = context(client, run_id=OLDER_RUN)["airflow"]
    assert older["selected_run"]["dag_run_id"] == OLDER_RUN  # type: ignore[index]
    missing = context(client, run_id="manual__nope")["airflow"]
    assert missing["selected_run"] is None  # type: ignore[index]
    assert missing["error"] == "run not found"  # type: ignore[index]


@pytest.mark.spec("S05-AC2")
def test_airflow_outage_still_returns_metadata(client: TestClient, scenario: FakeAirflow) -> None:
    scenario.fail_next("GET", "/dags/customer_daily_load", 503)
    body = context(client)
    airflow = body["airflow"]
    assert body["static_metadata"]
    assert airflow["unavailable"] is True and airflow["error"] == "http_503"  # type: ignore[index]
    assert "detail" not in str(airflow)


@pytest.mark.spec("S05-AC2")
def test_unconfigured_airflow_is_reported_not_raised(
    make_settings: Callable[..., Settings],
) -> None:
    with TestClient(create_app(make_settings())) as client:
        body = context(client)
        assert body["airflow"]["unavailable"] is True  # type: ignore[index]
        assert body["airflow"]["error"] == "not configured"  # type: ignore[index]


@pytest.mark.spec("S05-AC8")
def test_unknown_pipeline_is_404(client: TestClient) -> None:
    response = client.get("/pipelines/nope/context", headers=auth("viewer"))
    assert response.status_code == 404
    assert response.json()["error_code"] == "METADATA_NOT_FOUND"
    assert client.get("/pipelines/customer_daily_load/context").status_code == 401


@pytest.mark.spec("S05-AC8")
def test_credentials_never_appear_in_responses_or_logs(
    client: TestClient, scenario: FakeAirflow, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    scenario.fail_next("GET", "/dags/customer_daily_load", 500)  # error body echoes the password
    bodies = [
        client.get("/pipelines/customer_daily_load/context", headers=auth("viewer")).text,
        client.get("/airflow/dags", headers=auth("viewer")).text,
        client.get("/health/ready", headers=auth("viewer")).text,
    ]
    scenario.fail_next("POST", "/dagRuns", 500)
    bodies.append(
        client.post(
            "/airflow/dags/customer_daily_load/runs", json={}, headers=auth("operator")
        ).text
    )
    basic = "c3ZjLXNlbGZoZWFsaW5nOmFpcmZsb3ctcGFzcy1kby1ub3QtbGVhay03NzMx"  # base64(user:pass)
    for text in [*bodies, caplog.text]:
        assert PASSWORD not in text
        assert basic not in text
