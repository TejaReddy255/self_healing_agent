"""Platform-side data-quality contracts (src §23, architecture §18 C9).

The DQ service (ADR-013) has its own API models. Backend adapters map them onto these types, so
nothing here depends on how a DQ agent computes its result.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from pydantic import Field, NonNegativeInt, RootModel, field_validator, model_validator

from backend.domain.common import (
    Contract,
    DagId,
    NonEmptyStr,
    PipelineId,
    RuleId,
    RunId,
    ShortText,
    UtcDatetime,
)


class Checkpoint(StrEnum):
    SOURCE = "SOURCE"
    STAGING = "STAGING"
    TARGET = "TARGET"
    RECONCILIATION = "RECONCILIATION"
    SCHEDULED = "SCHEDULED"


class DQStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 - a DQ verdict, not a credential
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    ERROR = "ERROR"


class DQAgentName(StrEnum):
    """Logical DQ agent names (architecture §18 C9)."""

    CUSTOM_DQ = "custom_dq"
    RECON = "recon"
    PROFILING = "profiling"
    STATISTICAL = "statistical"
    ANOMALY = "anomaly"
    PREPROCESSING = "preprocessing"


class DQRuleRef(Contract):
    """Reference to a governed business rule (src §33.5), e.g. ``BR-CUST-002``."""

    rule_id: RuleId
    rule_set_id: RuleId | None = None


class DQProfileEntry(Contract):
    """One agent in a checkpoint's DQ profile (canonical shape, architecture §18 C9)."""

    agent: DQAgentName
    enabled: bool = True
    blocking: bool = False
    rules: list[RuleId] | None = None
    schedule: ShortText | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("agent", mode="before")
    @classmethod
    def _canonical_agent_name(cls, value: object) -> object:
        if value == "reconciliation":
            raise ValueError("use the logical agent name 'recon' (architecture §18 C9)")
        return value


class DQProfile(RootModel[dict[Checkpoint, list[DQProfileEntry]]]):
    """Per-checkpoint DQ agent selection, e.g. ``{"TARGET": [{"agent": "custom_dq", ...}]}``."""

    model_config = {"frozen": True}


class DQExecutionContext(Contract):
    """Input to ``DQAgentAdapter.run`` (src §23.1)."""

    pipeline_id: PipelineId
    dag_id: DagId | None = None
    run_id: RunId | None = None
    checkpoint: Checkpoint
    request_key: ShortText
    rule_ids: list[RuleId] | None = None


class DQAgentResult(Contract):
    """One agent/rule outcome at a checkpoint, as persisted in ``dq_results`` (S17)."""

    result_id: UUID | None = None
    agent: DQAgentName
    checkpoint: Checkpoint
    rule_id: RuleId | None = None
    status: DQStatus
    blocking: bool
    expected: dict[str, Any] | None = None
    actual: dict[str, Any] | None = None
    failed_row_count: NonNegativeInt | None = None
    message: NonEmptyStr | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    dq_service_job_id: ShortText | None = None
    report_ref: ShortText | None = None
    evaluated_at: UtcDatetime


class DQRunResult(Contract):
    """Aggregated outcome of one checkpoint run."""

    dq_run_id: UUID | None = None
    request_key: ShortText
    pipeline_id: PipelineId
    run_id: RunId | None = None
    checkpoint: Checkpoint
    status: DQStatus
    results: list[DQAgentResult]
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    error: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _status_never_hides_failures(self) -> Self:
        # Invariant 7 depends on this: an errored or failed check must never surface as PASS.
        if self.status is DQStatus.PASS and any(
            r.status in (DQStatus.ERROR, DQStatus.CRITICAL) for r in self.results
        ):
            raise ValueError("status PASS contradicts an ERROR or CRITICAL result")
        if self.status not in (DQStatus.CRITICAL, DQStatus.ERROR) and any(
            r.blocking and r.status is DQStatus.CRITICAL for r in self.results
        ):
            raise ValueError("a blocking CRITICAL result requires run status CRITICAL or ERROR")
        return self
