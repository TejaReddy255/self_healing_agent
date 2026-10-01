from __future__ import annotations

from typing import Any

import pytest

from backend.airflow.errors import AirflowProtocolError
from backend.airflow.models import AirflowDag, AirflowDagRun
from backend.domain.audit import AuditEvent
from backend.metadata.service import MetadataService
from backend.metadata.settings import DEFAULT_METADATA_DIR
from backend.services.airflow_actions import ActorRef, AirflowActionService, DagNotRegistered
from tests.fakes.metadata_trees import settings_for

DAG = "customer_daily_load"
OPERATOR = ActorRef("operator@test", "operator")


class RecordingAudit:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[AuditEvent] = []
        self.fail = fail

    async def record(self, event: AuditEvent, **_: Any) -> int:
        if self.fail:
            raise ConnectionError("audit database down")
        self.events.append(event)
        return len(self.events)


class StubClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.triggers = 0

    async def trigger_dag_run(
        self, dag_id: str, dag_run_id: str, conf: Any = None
    ) -> AirflowDagRun:
        self.triggers += 1
        if self.error is not None:
            raise self.error
        return AirflowDagRun(dag_id=dag_id, dag_run_id=dag_run_id, state="queued")

    async def patch_dag(self, dag_id: str, *, is_paused: bool) -> AirflowDag:
        if self.error is not None:
            raise self.error
        return AirflowDag(dag_id=dag_id, is_paused=is_paused)


def service(client: StubClient, audit: RecordingAudit) -> AirflowActionService:
    metadata = MetadataService(settings_for(DEFAULT_METADATA_DIR))
    metadata.load_initial()
    return AirflowActionService(client, metadata, audit)  # type: ignore[arg-type]


@pytest.mark.spec("S05-AC6")
async def test_denied_attempts_are_audited_without_calling_airflow() -> None:
    client, audit = StubClient(), RecordingAudit()
    with pytest.raises(DagNotRegistered):
        await service(client, audit).trigger("not_registered", OPERATOR)
    assert client.triggers == 0
    assert [(e.event_type, e.status, e.dag_id) for e in audit.events] == [
        ("AIRFLOW_TRIGGER", "DENIED", "not_registered")
    ]


@pytest.mark.spec("S05-AC6")
@pytest.mark.parametrize(
    ("error", "status"),
    [(AirflowProtocolError("bad json"), "BAD_RESPONSE"), (RuntimeError("bug"), "OUTCOME_UNKNOWN")],
)
async def test_unexpected_outcomes_are_still_audited(error: Exception, status: str) -> None:
    audit = RecordingAudit()
    with pytest.raises(type(error)):
        await service(StubClient(error), audit).trigger(DAG, OPERATOR)
    assert [(e.event_type, e.status) for e in audit.events] == [("AIRFLOW_TRIGGER", status)]
    assert audit.events[0].run_id is not None and audit.events[0].actor_subject == "operator@test"


@pytest.mark.spec("S05-AC6")
async def test_audit_failure_never_hides_a_successful_trigger(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = StubClient()
    run = await service(client, RecordingAudit(fail=True)).trigger(DAG, OPERATOR)
    assert run.dag_run_id.startswith("manual__console__")
    assert client.triggers == 1
    assert "airflow action audit failed" in caplog.text


async def test_pause_and_resume_are_audited_separately() -> None:
    audit = RecordingAudit()
    actions = service(StubClient(), audit)
    await actions.set_paused(DAG, True, OPERATOR)
    await actions.set_paused(DAG, False, OPERATOR)
    assert [(e.event_type, e.status) for e in audit.events] == [
        ("AIRFLOW_PAUSE", "OK"),
        ("AIRFLOW_RESUME", "OK"),
    ]
