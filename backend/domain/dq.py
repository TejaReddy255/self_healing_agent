"""Platform-side data-quality contracts (src §23, architecture §18 C9).

The DQ service (ADR-013) has its own API models. Backend adapters map them onto these types, so
nothing here depends on how a DQ agent computes its result.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import (
    ConfigDict,
    Field,
    NonNegativeInt,
    RootModel,
    StrictBool,
    field_validator,
    model_validator,
)

from backend.domain.common import (
    Contract,
    DagId,
    JsonObject,
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
    enabled: StrictBool = True
    blocking: StrictBool = False
    rules: tuple[RuleId, ...] | None = None
    schedule: ShortText | None = None
    params: JsonObject = Field(default_factory=dict)

    @field_validator("agent", mode="before")
    @classmethod
    def _canonical_agent_name(cls, value: object) -> object:
        if value == "reconciliation":
            raise ValueError("use the logical agent name 'recon' (architecture §18 C9)")
        return value


class DQProfile(RootModel[dict[Checkpoint, tuple[DQProfileEntry, ...]]]):
    """Per-checkpoint DQ agent selection, e.g. ``{"TARGET": [{"agent": "custom_dq", ...}]}``."""

    model_config = ConfigDict(frozen=True)


class DQExecutionContext(Contract):
    """Input to ``DQAgentAdapter.run`` (src §23.1)."""

    pipeline_id: PipelineId
    dag_id: DagId | None = None
    run_id: RunId | None = None
    checkpoint: Checkpoint
    request_key: ShortText
    rule_ids: tuple[RuleId, ...] | None = None


class DQAgentResult(Contract):
    """One agent/rule outcome at a checkpoint, as persisted in ``dq_results`` (S17)."""

    result_id: UUID | None = None
    agent: DQAgentName
    checkpoint: Checkpoint
    rule_id: RuleId | None = None
    # Identifies profiling/statistical/anomaly checks that have no business rule.
    check_id: ShortText | None = None
    status: DQStatus
    blocking: StrictBool
    expected: JsonObject | None = None
    actual: JsonObject | None = None
    failed_row_count: NonNegativeInt | None = None
    message: NonEmptyStr | None = None
    evidence: JsonObject = Field(default_factory=dict)
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
    results: tuple[DQAgentResult, ...]
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    error: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _status_never_hides_failures(self) -> Self:
        # Invariant 7 depends on this: an errored, failed or empty check never surfaces as PASS.
        if self.status is DQStatus.PASS and not self.results:
            raise ValueError("status PASS requires at least one result")
        if self.status is DQStatus.PASS and any(
            r.status in (DQStatus.ERROR, DQStatus.CRITICAL) for r in self.results
        ):
            raise ValueError("status PASS contradicts an ERROR or CRITICAL result")
        if self.status not in (DQStatus.CRITICAL, DQStatus.ERROR) and any(
            r.blocking and r.status in (DQStatus.CRITICAL, DQStatus.ERROR) for r in self.results
        ):
            raise ValueError(
                "a blocking CRITICAL or ERROR result requires run status CRITICAL or ERROR"
            )
        return self
