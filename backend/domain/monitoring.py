"""Monitoring contracts (src §21.3 and §22.2, merged per architecture §18 C6)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

from backend.domain.common import (
    Contract,
    DagId,
    EventId,
    JsonObject,
    NonEmptyStr,
    PipelineId,
    RuleId,
    RunId,
    Severity,
    ShortText,
    TaskId,
    UtcDatetime,
)

# Upper snake case keeps event types stable inputs to incident fingerprints (src §21.6).
EventType = Annotated[str, StringConstraints(max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")]


class MonitoringSource(StrEnum):
    AIRFLOW = "AIRFLOW"
    DQ = "DQ"
    SLA = "SLA"
    DATABASE = "DATABASE"
    MCP = "MCP"


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class MonitorRunStatus(StrEnum):
    """Outcome of one monitor in a cycle (S14)."""

    OK = "ok"
    SKIPPED = "skipped"
    ERROR = "error"
    TIMEOUT = "timeout"


class MonitoringEvent(Contract):
    """A deterministic signal from a monitor or an Airflow/DQ callback.

    ``event_id`` is assigned on persistence. ``idempotency_key`` is caller-supplied or derived at
    ingest (S13). ``resource`` and ``rule_id`` (or ``metric`` for checks without a rule) feed the
    incident fingerprint (src §24.2).
    """

    event_id: EventId | None = None
    idempotency_key: ShortText | None = None
    pipeline_id: PipelineId
    dag_id: DagId | None = None
    run_id: RunId | None = None
    task_id: TaskId | None = None
    source: MonitoringSource
    event_type: EventType
    severity: Severity
    resource: ShortText | None = None
    rule_id: RuleId | None = None
    metric: ShortText | None = None
    expected: NonEmptyStr | None = None
    actual: NonEmptyStr | None = None
    evidence: JsonObject = Field(default_factory=dict)
    detected_at: UtcDatetime


class PipelineHealthResult(Contract):
    pipeline_id: PipelineId
    overall_status: HealthStatus
    events: tuple[MonitoringEvent, ...] = ()
    monitor_status: dict[ShortText, MonitorRunStatus] = Field(default_factory=dict)
    evaluated_at: UtcDatetime
