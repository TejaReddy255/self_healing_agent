from __future__ import annotations

from uuid import UUID

import pytest

from backend.airflow.errors import AirflowConflict, AirflowUnavailable
from backend.airflow.models import AirflowDagRun
from backend.airflow.recovery import recovery_run_id, trigger_recovery_run, wait_for_terminal
from tests.fakes.airflow import canonical_scenario, fake_airflow, make_client

DAG = "customer_daily_load"
REVIEW = UUID("6f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f")
FAILED_RUN = "scheduled__2026-10-01T02:00:00+00:00"


# --- AC4: idempotent trigger ----------------------------------------------------------------


@pytest.mark.spec("S05-AC4")
async def test_trigger_is_idempotent_per_review_revision() -> None:
    with fake_airflow(canonical_scenario()) as fake:
        client, _ = make_client()
        first = await trigger_recovery_run(client, DAG, REVIEW, 2, FAILED_RUN)
        second = await trigger_recovery_run(client, DAG, REVIEW, 2, FAILED_RUN)
        assert first.dag_run_id == second.dag_run_id == f"recovery__{REVIEW}__r2"
        assert len([r for r in fake.runs[DAG] if r.startswith("recovery__")]) == 1
        assert first.conf == {
            "self_healing": {"review_id": str(REVIEW), "revision": 2, "recovery_of": FAILED_RUN}
        }
        await client.aclose()


@pytest.mark.spec("S05-AC4")
async def test_new_revision_gets_its_own_run() -> None:
    with fake_airflow(canonical_scenario()) as fake:
        client, _ = make_client()
        await trigger_recovery_run(client, DAG, REVIEW, 1, FAILED_RUN)
        await trigger_recovery_run(client, DAG, REVIEW, 2, FAILED_RUN)
        assert {r for r in fake.runs[DAG] if r.startswith("recovery__")} == {
            recovery_run_id(REVIEW, 1),
            recovery_run_id(REVIEW, 2),
        }
        await client.aclose()


@pytest.mark.spec("S05-AC4")
async def test_conflict_with_a_foreign_run_is_not_adopted() -> None:
    fake = canonical_scenario()
    fake.add_run(DAG, recovery_run_id(REVIEW, 1), state="running", conf={"someone": "else"})
    with fake_airflow(fake):
        client, _ = make_client()
        with pytest.raises(AirflowConflict, match="not created for this review"):
            await trigger_recovery_run(client, DAG, REVIEW, 1, FAILED_RUN)
        await client.aclose()


@pytest.mark.spec("S05-AC4")
async def test_conflict_without_the_run_is_reported() -> None:
    with fake_airflow(canonical_scenario()) as fake:
        fake.fail_next("POST", "/dagRuns", 409)  # e.g. a logical-date collision
        client, _ = make_client()
        with pytest.raises(AirflowConflict, match="does not exist"):
            await trigger_recovery_run(client, DAG, REVIEW, 3, FAILED_RUN)
        assert len(fake.calls_to("POST")) == 1  # never retried
        await client.aclose()


# --- AC5: terminal wait with a fake clock ------------------------------------------------------


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedClient:
    """Returns run states (or raises) in order, then repeats the last one."""

    def __init__(self, script: list[str | Exception]) -> None:
        self.script = script
        self.calls = 0

    async def get_dag_run(self, dag_id: str, dag_run_id: str) -> AirflowDagRun:
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(step, Exception):
            raise step
        return AirflowDagRun(dag_id=dag_id, dag_run_id=dag_run_id, state=step)


@pytest.mark.spec("S05-AC5")
@pytest.mark.parametrize(
    ("script", "status", "polls"),
    [
        (["queued", "running", "success"], "success", 3),
        (["running", "failed"], "failed", 2),
        (["running"], "timeout", 4),
        ([AirflowUnavailable("down"), "running", "success"], "success", 3),
        (
            [AirflowUnavailable("down")],
            "timeout",
            4,
        ),
    ],
    ids=["success", "failed", "timeout", "transient-outage", "outage-until-timeout"],
)
async def test_wait_for_terminal(script: list[str | Exception], status: str, polls: int) -> None:
    time = FakeTime()
    client = ScriptedClient(script)
    outcome = await wait_for_terminal(
        client,  # type: ignore[arg-type]
        DAG,
        "recovery__x__r1",
        timeout_s=90,
        poll_s=30,
        clock=time.clock,
        sleep=time.sleep,
    )
    assert outcome.status == status
    assert client.calls == polls
    assert all(s <= 30 for s in time.sleeps)


@pytest.mark.spec("S05-AC5")
async def test_disappearing_run_is_failed() -> None:
    with fake_airflow(canonical_scenario()):
        client, _ = make_client()
        time = FakeTime()
        outcome = await wait_for_terminal(
            client, DAG, "recovery__gone__r1", timeout_s=60, poll_s=30, clock=time.clock,
            sleep=time.sleep,
        )  # fmt: skip
        assert (outcome.status, outcome.reason) == ("failed", "run disappeared")
        await client.aclose()


@pytest.mark.spec("S05-AC5")
async def test_wait_resumes_on_an_existing_run_after_restart() -> None:
    fake = canonical_scenario()
    fake.add_run(DAG, recovery_run_id(REVIEW, 1), state="success", conf={})
    with fake_airflow(fake) as fake_api:
        client, _ = make_client()
        time = FakeTime()
        outcome = await wait_for_terminal(
            client, DAG, recovery_run_id(REVIEW, 1), timeout_s=60, poll_s=30, clock=time.clock,
            sleep=time.sleep,
        )  # fmt: skip
        assert outcome.status == "success"
        assert fake_api.calls_to("POST") == []  # waiting never triggers
        await client.aclose()
