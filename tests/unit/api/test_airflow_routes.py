from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from tests.conftest import auth
from tests.fakes.airflow import FakeAirflow, canonical_scenario, fake_airflow, fake_settings

DAG = "customer_daily_load"
RUN = "scheduled__2026-10-01T02:00:00+00:00"


@pytest.fixture
def scenario() -> FakeAirflow:
    fake = canonical_scenario()
    fake.add_dag("unregistered_dag").add_run("unregistered_dag", "manual__1")
    return fake


@pytest.fixture
def client(make_settings: Callable[..., Settings], scenario: FakeAirflow) -> Iterator[TestClient]:
    with fake_airflow(scenario):
        app = create_app(make_settings(), airflow_settings=fake_settings(airflow_get_retries=0))
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client


# --- reads -------------------------------------------------------------------------------------


@pytest.mark.spec("S05-AC6")
def test_viewer_reads_registered_dags_runs_and_tasks(client: TestClient) -> None:
    dags = client.get("/airflow/dags", headers=auth("viewer")).json()
    assert [d["dag_id"] for d in dags["items"]] == [DAG] and dags["missing"] == []
    runs = client.get(f"/airflow/dags/{DAG}/runs", params={"limit": 1}, headers=auth("viewer"))
    assert [r["dag_run_id"] for r in runs.json()["items"]] == [RUN]
    tasks = client.get(f"/airflow/dags/{DAG}/runs/{RUN}/tasks", headers=auth("viewer"))
    assert len(tasks.json()["items"]) == 3
    missing = client.get(f"/airflow/dags/{DAG}/runs/manual__nope/tasks", headers=auth("viewer"))
    assert missing.status_code == 404 and missing.json()["error_code"] == "AIRFLOW_NOT_FOUND"


# --- AC9: registered DAGs only --------------------------------------------------------------------


@pytest.mark.spec("S05-AC9")
@pytest.mark.parametrize(
    ("method", "path", "role", "body"),
    [
        ("GET", "/airflow/dags/unregistered_dag/runs", "viewer", None),
        ("GET", "/airflow/dags/unregistered_dag/runs/manual__1/tasks", "viewer", None),
        ("POST", "/airflow/dags/unregistered_dag/runs", "operator", {}),
        ("PATCH", "/airflow/dags/unregistered_dag", "operator", {"is_paused": True}),
    ],
)
def test_unregistered_dags_are_404_without_calling_airflow(
    client: TestClient,
    scenario: FakeAirflow,
    method: str,
    path: str,
    role: str,
    body: dict[str, object] | None,
) -> None:
    before = len(scenario.calls)
    response = client.request(method, path, json=body, headers=auth(role))
    assert response.status_code == 404
    assert response.json()["error_code"] == "AIRFLOW_DAG_NOT_REGISTERED"
    assert [c for c in scenario.calls[before:] if "unregistered_dag" in c.raw_path] == []


@pytest.mark.spec("S05-AC9")
def test_dag_list_shows_only_registered_dags(client: TestClient, scenario: FakeAirflow) -> None:
    items = client.get("/airflow/dags", headers=auth("viewer")).json()["items"]
    assert "unregistered_dag" not in {d["dag_id"] for d in items}
    assert not scenario.calls_to("GET", "/dags/unregistered_dag")


# --- AC6: operator actions -------------------------------------------------------------------


@pytest.mark.spec("S05-AC6")
def test_viewer_cannot_trigger_or_pause(client: TestClient, scenario: FakeAirflow) -> None:
    trigger = client.post(f"/airflow/dags/{DAG}/runs", json={}, headers=auth("viewer"))
    pause = client.patch(f"/airflow/dags/{DAG}", json={"is_paused": True}, headers=auth("viewer"))
    assert (trigger.status_code, pause.status_code) == (403, 403)
    assert not scenario.calls_to("POST") and not scenario.calls_to("PATCH")


@pytest.mark.spec("S05-AC6")
def test_operator_triggers_without_conf_and_pauses(
    client: TestClient, scenario: FakeAirflow
) -> None:
    created = client.post(f"/airflow/dags/{DAG}/runs", json={}, headers=auth("operator"))
    assert created.status_code == 201
    run = created.json()
    assert run["dag_run_id"].startswith("manual__console__")
    (call,) = scenario.calls_to("POST", "/dagRuns")
    assert call.body == {"dag_run_id": run["dag_run_id"], "conf": {}}

    paused = client.patch(
        f"/airflow/dags/{DAG}", json={"is_paused": True}, headers=auth("operator")
    )
    assert paused.status_code == 200 and paused.json()["is_paused"] is True
    assert scenario.calls_to("PATCH")[0].params == {"update_mask": "is_paused"}


@pytest.mark.spec("S05-AC6")
@pytest.mark.parametrize(
    "body",
    [{"conf": {"target": "prod"}}, {"dag_run_id": "chosen"}, {"logical_date": "2026-10-01"}],
)
def test_trigger_rejects_conf_and_any_other_field(
    client: TestClient, scenario: FakeAirflow, body: dict[str, object]
) -> None:
    response = client.post(f"/airflow/dags/{DAG}/runs", json=body, headers=auth("operator"))
    assert response.status_code == 422
    assert not scenario.calls_to("POST")


@pytest.mark.spec("S05-AC6")
@pytest.mark.parametrize(
    ("status", "code", "http"),
    [
        (503, "AIRFLOW_UNAVAILABLE", 503),
        (409, "AIRFLOW_CONFLICT", 409),
        (401, "AIRFLOW_AUTH_FAILED", 502),
    ],
)
def test_airflow_errors_map_to_stable_codes(
    client: TestClient, scenario: FakeAirflow, status: int, code: str, http: int
) -> None:
    scenario.fail_next("POST", "/dagRuns", status)
    response = client.post(f"/airflow/dags/{DAG}/runs", json={}, headers=auth("operator"))
    assert (response.status_code, response.json()["error_code"]) == (http, code)


# --- AC11 / AC12: configuration and readiness -----------------------------------------------------


@pytest.mark.spec("S05-AC11")
def test_unconfigured_airflow_routes_answer_503(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings()), raise_server_exceptions=False) as client:
        response = client.get("/airflow/dags", headers=auth("viewer"))
        assert response.status_code == 503
        assert response.json()["error_code"] == "AIRFLOW_UNAVAILABLE"
        ready = client.get("/health/ready", headers=auth("viewer")).json()
        assert ready["components"]["airflow"] == {"status": "degraded", "detail": "not configured"}


@pytest.mark.spec("S05-AC12")
def test_readiness_is_ok_when_airflow_is_healthy(client: TestClient) -> None:
    ready = client.get("/health/ready", headers=auth("viewer"))
    assert ready.json()["components"]["airflow"] == {"status": "ok", "detail": None}


@pytest.mark.spec("S05-AC12")
@pytest.mark.parametrize(
    ("setup", "detail"),
    [
        (lambda f: f.health.update(scheduler={"status": "unhealthy"}), "scheduler unhealthy"),
        (lambda f: f.fail_next("GET", "/health", "connect"), "unreachable"),
        (lambda f: f.fail_next("GET", "/health", "timeout"), "timeout"),
    ],
    ids=["scheduler-unhealthy", "unreachable", "timeout"],
)
def test_readiness_degrades_but_never_goes_down(
    client: TestClient,
    scenario: FakeAirflow,
    setup: Callable[[FakeAirflow], object],
    detail: str,
) -> None:
    setup(scenario)
    ready = client.get("/health/ready", headers=auth("viewer"))
    assert ready.status_code == 200
    component = ready.json()["components"]["airflow"]
    assert component["status"] == "degraded" and detail in component["detail"]


@pytest.mark.spec("S05-AC10")
@pytest.mark.parametrize("run_id", ["..", "~", "a%2Fb", "a b"])
def test_routes_reject_hostile_run_ids(
    client: TestClient, scenario: FakeAirflow, run_id: str
) -> None:
    before = len(scenario.calls)
    response = client.get(f"/airflow/dags/{DAG}/runs/{run_id}/tasks", headers=auth("viewer"))
    assert response.status_code in (404, 422)
    context = client.get(
        f"/pipelines/{DAG}/context", params={"run_id": run_id}, headers=auth("viewer")
    )
    assert context.status_code == 422
    assert [c for c in scenario.calls[before:] if "dagRuns/" in c.raw_path] == []


def test_run_conf_is_redacted_for_viewers(client: TestClient, scenario: FakeAirflow) -> None:
    scenario.add_run(DAG, "manual__secret", conf={"db_password": "hunter2", "batch": 7})
    items = client.get(f"/airflow/dags/{DAG}/runs", headers=auth("viewer")).json()["items"]
    conf = next(r for r in items if r["dag_run_id"] == "manual__secret")["conf"]
    assert conf["batch"] == 7 and "hunter2" not in str(conf)


class SlowHealth:
    """An Airflow that never answers its health endpoint in time."""

    calls = 0

    async def health(self, *, retry: bool = True) -> object:
        import asyncio

        SlowHealth.calls += 1
        await asyncio.sleep(30)
        raise AssertionError("unreachable")


@pytest.mark.spec("S05-AC12")
def test_slow_airflow_degrades_readiness_instead_of_failing_it(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    import backend.app as app_module

    monkeypatch.setattr(app_module, "AIRFLOW_READINESS_TIMEOUT_S", 0.2)
    SlowHealth.calls = 0
    app = create_app(make_settings(), airflow_client=SlowHealth())  # type: ignore[arg-type]
    with TestClient(app) as client:
        first = client.get("/health/ready", headers=auth("viewer"))
        assert first.status_code == 200
        assert first.json()["components"]["airflow"] == {"status": "degraded", "detail": "timeout"}
        client.get("/health/ready", headers=auth("viewer"))
        assert SlowHealth.calls == 1  # cached: probes do not hammer Airflow
