"""RCA request and structured analysis output (src §6.2)."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, PositiveInt, StrictBool, model_validator

from backend.domain.common import (
    Confidence,
    Contract,
    DagId,
    EvidenceStatus,
    NonEmptyStr,
    PipelineId,
    RunId,
    Severity,
    ShortText,
    SystemName,
    TaskId,
)
from backend.domain.remediation import RemediationPlan
from backend.domain.sop import SOPMatchStatus, SOPProposal, SOPReference, check_sop_match

# Execution-result semantics (src §10.2): the healing agent reports "remediated" or "blocked"
# (a plan stop condition was observed); "failed" means the pipeline is still failed. These are the
# agent's claims only: they never move an incident towards RESOLVED on their own (invariants 6, 7),
# and the diagnostic service rejects fixes_applied=true from a read-only RCA (S09).
PipelineStatus = Literal["unknown", "failed", "remediated", "blocked"]


class PipelineFailureRequest(Contract):
    """Body of ``POST /agent``: the failed Airflow run (and optionally task) to analyse."""

    workflow_id: DagId = Field(description="The Airflow dag_id of the failed run.")
    run_id: RunId
    task_id: TaskId | None = None


class Finding(Contract):
    title: ShortText
    description: NonEmptyStr
    evidence_status: EvidenceStatus
    severity: Severity
    category: ShortText | None = None
    evidence_refs: tuple[ShortText, ...] = ()


class Recommendation(Contract):
    priority: PositiveInt
    action: NonEmptyStr
    rationale: NonEmptyStr
    requires_human_approval: StrictBool


class AnalysisSummary(Contract):
    pipeline_id: PipelineId | None = None
    dag_id: DagId
    run_id: RunId
    task_id: TaskId | None = None
    root_cause: NonEmptyStr
    impact: NonEmptyStr
    affected_system: SystemName | None = None
    affected_resource: ShortText | None = None
    error_details: NonEmptyStr | None = None


class StructuredAnalysis(Contract):
    summary: AnalysisSummary
    detailed_findings: tuple[Finding, ...] = ()
    recommendations: tuple[Recommendation, ...] = ()
    prevention_measures: tuple[NonEmptyStr, ...] = ()
    sop_match_status: SOPMatchStatus
    matched_sop_reference: SOPReference | None = None
    proposed_sop: SOPProposal | None = None
    proposed_remediation: RemediationPlan | None = None
    analysis_confidence: Confidence
    additional_investigation_needed: tuple[NonEmptyStr, ...] = ()
    fixes_applied: StrictBool = False
    pipeline_status: PipelineStatus = "unknown"

    @model_validator(mode="after")
    def _sop_reference_matches_status(self) -> Self:
        check_sop_match(self.sop_match_status, self.matched_sop_reference)
        return self
