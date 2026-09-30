"""RCA request and structured analysis output (src §6.2)."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, PositiveInt, model_validator

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
# (a plan stop condition was observed); "failed" means the pipeline is still failed.
PipelineStatus = Literal["unknown", "failed", "remediated", "blocked"]


class PipelineFailureRequest(Contract):
    """Body of ``POST /agent``: the failed Airflow run (and optionally task) to analyse."""

    workflow_id: DagId
    run_id: RunId
    task_id: TaskId | None = None


class Finding(Contract):
    title: ShortText
    description: NonEmptyStr
    evidence_status: EvidenceStatus
    severity: Severity
    category: ShortText | None = None
    evidence_refs: list[ShortText] = Field(default_factory=list)


class Recommendation(Contract):
    priority: PositiveInt
    action: NonEmptyStr
    rationale: NonEmptyStr
    requires_human_approval: bool


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
    detailed_findings: list[Finding] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    prevention_measures: list[NonEmptyStr] = Field(default_factory=list)
    sop_match_status: SOPMatchStatus
    matched_sop_reference: SOPReference | None = None
    proposed_sop: SOPProposal | None = None
    proposed_remediation: RemediationPlan | None = None
    analysis_confidence: Confidence
    additional_investigation_needed: list[NonEmptyStr] = Field(default_factory=list)
    fixes_applied: bool = False
    pipeline_status: PipelineStatus = "unknown"

    @model_validator(mode="after")
    def _sop_reference_matches_status(self) -> Self:
        check_sop_match(self.sop_match_status, self.matched_sop_reference)
        return self
