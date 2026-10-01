from __future__ import annotations

import pytest

from backend.airflow.client import encode_path
from backend.airflow.errors import (
    AirflowAuthError,
    AirflowConflict,
    AirflowNotFound,
    AirflowProtocolError,
    AirflowRequestError,
    AirflowUnavailable,
)
from backend.airflow.models import AirflowDag
from tests.fakes.airflow import FakeAirflow, canonical_scenario, fake_airflow, make_client

DAG = "customer_daily_load"
RUN = "scheduled__2026-10-01T02:00:00+00:00"

pytestmark = pytest.mark.timeout(30)


# --- AC2: retries for GETs only, error mapping ---------------------------------------------------


@pytest.mark.spec("S05-AC2")
@pytest.mark.parametrize("kind", ["timeout", "connect", 500, 503, 429])
async def test_transient_get_failures_are_retried_then_succeed(kind: str | int) -> None:
    with fake_airflow(canonical_scenario()) as fake:
        fake.fail_next("GET", f"/dags/{DAG}", kind, times=2)
        client, sleeps = make_client()
        dag = await client.get_dag(DAG)
        assert isinstance(dag, AirflowDag) and dag.dag_id == DAG
        assert len(fake.calls_to("GET", f"/dags/{DAG}")) == 3
        assert len(sleeps) == 2 and sleeps[1] >= sleeps[0] * 0.5
        await client.aclose()


@pytest.mark.spec("S05-AC2")
async def test_get_gives_up_after_configured_retries() -> None:
    with fake_airflow(canonical_scenario()) as fake:
        fake.fail_next("GET", f"/dags/{DAG}", 503, times=10)
        client, sleeps = make_client(airflow_get_retries=3)
        with pytest.raises(AirflowUnavailable) as caught:
            await client.get_dag(DAG)
        assert caught.value.category == "http_503"
        assert len(fake.calls_to("GET", f"/dags/{DAG}")) == 4  # 1 attempt + 3 retries
        assert len(sleeps) == 3
        await client.aclose()


@pytest.mark.spec("S05-AC2")
async def test_retry_after_is_honoured_and_capped() -> None:
    with fake_airflow(canonical_scenario()) as fake:
        fake.fail_next("GET", f"/dags/{DAG}", 429, retry_after="7")
        fake.fail_next("GET", f"/dags/{DAG}", 429, retry_after="9999")
        client, sleeps = make_client()
        await client.get_dag(DAG)
        assert sleeps == [7.0, 30.0]
        await client.aclose()


@pytest.mark.spec("S05-AC2")
@pytest.mark.parametrize("kind", ["timeout", 503])
async def test_writes_are_attempted_exactly_once(kind: str | int) -> None:
    with fake_airflow(canonical_scenario()) as fake:
        fake.fail_next("POST", "/dagRuns", kind)
        fake.fail_next("PATCH", f"/dags/{DAG}", kind)
        client, sleeps = make_client()
        with pytest.raises(AirflowUnavailable):
            await client.trigger_dag_run(DAG, "manual__x", None)
        with pytest.raises(AirflowUnavailable):
            await client.patch_dag(DAG, is_paused=True)
        assert len(fake.calls_to("POST")) == 1
        assert len(fake.calls_to("PATCH")) == 1
        assert sleeps == []
        await client.aclose()


@pytest.mark.spec("S05-AC2")
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (404, AirflowNotFound),
        (409, AirflowConflict),
        (401, AirflowAuthError),
        (403, AirflowAuthError),
        (400, AirflowRequestError),
    ],
)
async def test_client_errors_map_without_retry(status: int, error: type[Exception]) -> None:
    with fake_airflow(canonical_scenario()) as fake:
        fake.fail_next("GET", f"/dags/{DAG}", status)
        client, sleeps = make_client()
        with pytest.raises(error) as caught:
            await client.get_dag(DAG)
        assert len(fake.calls_to("GET")) == 1 and sleeps == []
        assert "do-not-leak" not in str(caught.value)  # response bodies are never echoed
        await client.aclose()


@pytest.mark.spec("S05-AC2")
async def test_wrong_credentials_are_an_auth_error() -> None:
    with fake_airflow(canonical_scenario()):
        client, _ = make_client(airflow_password="wrong")
        with pytest.raises(AirflowAuthError):
            await client.get_dag(DAG)
        await client.aclose()


# --- AC10: path encoding and redirects --------------------------------------------------------


@pytest.mark.spec("S05-AC10")
async def test_run_ids_reach_airflow_as_one_encoded_segment() -> None:
    with fake_airflow(canonical_scenario()) as fake:
        client, _ = make_client()
        run = await client.get_dag_run(DAG, RUN)
        assert run.dag_run_id == RUN
        (call,) = fake.calls_to("GET", "/dagRuns/")
        assert call.raw_path == (
            f"/api/v1/dags/{DAG}/dagRuns/scheduled__2026-10-01T02%3A00%3A00%2B00%3A00"
        )
        await client.aclose()


@pytest.mark.spec("S05-AC10")
@pytest.mark.parametrize(
    "hostile",
    ["../../dags/other", "..", ".", "~", "a/b", "a%2Fb", "a?b#c", "x y", "", "a" * 251],
)
async def test_hostile_identifiers_are_rejected_before_any_request(hostile: str) -> None:
    # Encoding alone is not enough: proxies in front of Airflow often decode %2F.
    with fake_airflow(canonical_scenario()) as fake:
        client, _ = make_client()
        with pytest.raises(AirflowRequestError, match="invalid Airflow identifier"):
            await client.get_dag_run(DAG, hostile)
        with pytest.raises(AirflowRequestError):
            await client.get_task_log(DAG, RUN, hostile, 1)
        assert fake.calls == []
        await client.aclose()


def test_encode_path_keeps_numbers_and_safe_ids() -> None:
    assert encode_path("dags", "a.b-c_d", 3) == "dags/a.b-c_d/3"
    assert encode_path("x", "manual__2026:01+00") == "x/manual__2026%3A01%2B00"


@pytest.mark.spec("S05-AC10")
async def test_answers_for_a_different_resource_are_rejected() -> None:
    fake = canonical_scenario()
    fake.dags["customer_daily_load"]["dag_id"] = "some_other_dag"
    with fake_airflow(fake):
        client, _ = make_client()
        with pytest.raises(AirflowProtocolError, match="different resource"):
            await client.get_dag(DAG)
        await client.aclose()


@pytest.mark.spec("S05-AC10")
async def test_redirects_are_not_followed() -> None:
    with fake_airflow(FakeAirflow()) as fake:
        fake.fail_next("GET", "/dags/elsewhere", 302)
        client, _ = make_client()
        with pytest.raises(AirflowRequestError, match="redirect"):
            await client.get_dag("elsewhere")
        assert len(fake.calls) == 1
        await client.aclose()


# --- reads ------------------------------------------------------------------------------------


async def test_reads_parse_airflow_shapes() -> None:
    with fake_airflow(canonical_scenario()):
        client, _ = make_client()
        dag = await client.get_dag(DAG)
        assert dag.tags == ("customer",)
        assert dag.next_dagrun is not None and dag.next_dagrun.tzinfo is not None
        runs = await client.list_dag_runs(DAG, 5)
        assert runs[0].dag_run_id == RUN  # newest first
        tasks = await client.list_task_instances(DAG, RUN)
        assert [t.task_id for t in tasks] == [
            "dq_target_check",
            "extract_customers",
            "load_customers",
        ]
        listing = await client.list_dags([DAG, "not_in_airflow"])
        assert [d.dag_id for d in listing.items] == [DAG]
        assert listing.missing == ("not_in_airflow",)
        health = await client.health()
        assert health.healthy
        await client.aclose()


async def test_task_instances_are_paginated() -> None:
    fake = FakeAirflow().add_dag(DAG).add_run(DAG, RUN)
    for i in range(250):
        fake.add_task(DAG, RUN, f"task_{i:03d}")
    with fake_airflow(fake):
        client, _ = make_client()
        tasks = await client.list_task_instances(DAG, RUN)
        assert len(tasks) == 250
        assert len(fake.calls_to("GET", "/taskInstances")) == 3
        await client.aclose()


async def test_runs_are_requested_newest_first() -> None:
    with fake_airflow(canonical_scenario()) as fake:
        client, _ = make_client()
        await client.list_dag_runs(DAG, 5)
        (call,) = fake.calls_to("GET", "/dagRuns")
        assert call.params == {"limit": "5", "order_by": "-execution_date"}
        await client.aclose()


async def test_pagination_survives_a_small_server_page_limit() -> None:
    fake = FakeAirflow(max_page_limit=50).add_dag(DAG).add_run(DAG, RUN)
    for i in range(120):
        fake.add_task(DAG, RUN, f"task_{i:03d}", state="failed" if i == 119 else "success")
    with fake_airflow(fake):
        client, _ = make_client()
        tasks = await client.list_task_instances(DAG, RUN)
        assert len(tasks) == 120
        assert [t.task_id for t in tasks if t.failed] == ["task_119"]
        await client.aclose()


async def test_health_tolerates_unknown_or_missing_statuses() -> None:
    fake = canonical_scenario()
    fake.health = {"metadatabase": {"status": None}, "scheduler": {"status": "starting"}}
    with fake_airflow(fake):
        client, _ = make_client()
        health = await client.health()
        assert (health.metadatabase, health.scheduler, health.healthy) == (
            "unknown",
            "unknown",
            False,
        )
        await client.aclose()


@pytest.mark.spec("S05-AC2")
async def test_non_json_write_response_is_a_protocol_error() -> None:
    import httpx
    import respx

    from backend.airflow.client import HttpAirflowClient
    from tests.fakes.airflow import fake_settings

    with respx.mock() as router:
        router.post(url__regex=r".*/dagRuns$").mock(return_value=httpx.Response(200, text="ok"))
        client = HttpAirflowClient(fake_settings())
        with pytest.raises(AirflowProtocolError):
            await client.trigger_dag_run(DAG, "manual__x", None)
        await client.aclose()


@pytest.mark.spec("S05-AC2")
async def test_log_retries_honour_retry_after() -> None:
    with fake_airflow(canonical_scenario()) as fake:
        fake.fail_next("GET", "/logs/", 429, retry_after="4")
        client, sleeps = make_client()
        log = await client.get_task_log(DAG, RUN, "load_customers", 2)
        assert "duplicate key" in log.text
        assert sleeps == [4.0]
        await client.aclose()
