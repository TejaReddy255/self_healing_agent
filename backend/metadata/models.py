"""Static metadata contracts (src §33.4 to §33.6, S04).

Content models describe *intent* only: live Airflow state never appears here (invariant 8). They
reuse the S03 contract base, so documents are frozen, reject unknown fields and normalise text.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from functools import cached_property
from typing import Annotated, Any, Literal, Self

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    computed_field,
    model_validator,
)

from backend.domain.common import (
    ColumnName,
    Contract,
    DagId,
    JsonObject,
    NonEmptyStr,
    PipelineId,
    QualifiedName,
    RuleId,
    Severity,
    ShortText,
    SystemName,
    UtcDatetime,
)
from backend.domain.dq import DQProfile
from backend.domain.hashing import sop_content_sha256
from backend.domain.sop import SOPProposal

DocumentId = Annotated[
    str, StringConstraints(max_length=128, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
]
UpperSnake = Annotated[str, StringConstraints(max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")]
Tags = tuple[ShortText, ...]


class DocumentType(StrEnum):
    PIPELINE = "PIPELINE"
    BUSINESS_RULE = "BUSINESS_RULE"
    SOP = "SOP"
    RUNBOOK = "RUNBOOK"
    KNOWN_ISSUE = "KNOWN_ISSUE"
    OWNERSHIP = "OWNERSHIP"
    HISTORICAL_INCIDENT = "HISTORICAL_INCIDENT"


class SourceType(StrEnum):
    STATIC = "STATIC"
    JIRA = "JIRA"
    CONFLUENCE = "CONFLUENCE"


# --- Pipeline ----------------------------------------------------------------------------------


class AirflowLink(Contract):
    """Only the static relationship. Runs, tasks and logs are always read live (src §33.4)."""

    dag_id: DagId


class DataEndpoint(Contract):
    system: SystemName
    database: ColumnName | None = None
    schema_: ColumnName | None = Field(default=None, alias="schema")
    table: ColumnName

    # "schema" is accepted and emitted under its YAML name (it would shadow a BaseModel attribute).
    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)

    @property
    def qualified_name(self) -> str:
        prefix = self.schema_ or self.database
        return f"{prefix}.{self.table}" if prefix else self.table


class DatasetSpec(Contract):
    name: QualifiedName
    system: SystemName
    pii_columns: tuple[ColumnName, ...] = ()


class SlaPolicy(Contract):
    enabled: StrictBool = False
    expected_completion_time: (
        Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] | None
    ) = None
    timezone: ShortText = "UTC"
    max_runtime_minutes: Annotated[StrictInt, Field(gt=0, le=10_080)] | None = None
    severity: Severity = Severity.CRITICAL


class MonitorToggle(Contract):
    enabled: StrictBool = True


class MonitoringPolicy(Contract):
    enabled: StrictBool = True
    airflow: MonitorToggle = MonitorToggle()
    dq: MonitorToggle = MonitorToggle()
    sla: MonitorToggle = MonitorToggle()
    database: MonitorToggle = MonitorToggle()
    mcp: MonitorToggle = MonitorToggle()
    critical_runtime_factor: Annotated[float, Field(gt=1, le=100)] = 1.5

    def monitor_enabled(self, name: Literal["airflow", "dq", "sla", "database", "mcp"]) -> bool:
        toggle: MonitorToggle = getattr(self, name)
        return self.enabled and toggle.enabled


class SelfHealingPolicy(Contract):
    enabled: StrictBool = False
    auto_rca: StrictBool = False
    # Writes always need an exact human-approved revision (invariant 5).
    human_approval_required: Literal[True] = True
    permitted_systems: tuple[SystemName, ...] | None = None
    on_recovery_failure: Literal["REPLAN_REQUIRED", "FAILED"] = "REPLAN_REQUIRED"


class PipelineMetadata(Contract):
    pipeline_id: PipelineId
    name: ShortText
    description: NonEmptyStr | None = None
    status: Literal["ACTIVE", "INACTIVE"] = "ACTIVE"
    airflow: AirflowLink
    source: DataEndpoint
    target: DataEndpoint
    datasets: tuple[DatasetSpec, ...] = ()
    sla: SlaPolicy = SlaPolicy()
    monitoring: MonitoringPolicy = MonitoringPolicy()
    self_healing: SelfHealingPolicy = SelfHealingPolicy()
    dq_profile: DQProfile = DQProfile({})
    tags: Tags = ()

    @model_validator(mode="after")
    def _datasets_agree_with_endpoints(self) -> Self:
        endpoints = {e.qualified_name: e.system for e in (self.source, self.target)}
        names = [d.name for d in self.datasets]
        if len(names) != len(set(names)):
            raise ValueError("datasets must not repeat a name")
        for dataset in self.datasets:
            system = endpoints.get(dataset.name)
            if system is not None and system is not dataset.system:
                raise ValueError(
                    f"dataset {dataset.name} is declared on {dataset.system}, "
                    f"but the pipeline endpoint uses {system}"
                )
        return self

    def declared_datasets(self) -> tuple[DatasetSpec, ...]:
        """Source, target and extra datasets, merged by name and sorted."""
        merged: dict[str, DatasetSpec] = {
            e.qualified_name: DatasetSpec(name=e.qualified_name, system=e.system)
            for e in (self.source, self.target)
        }
        for dataset in self.datasets:
            merged[dataset.name] = dataset
        return tuple(merged[name] for name in sorted(merged))

    def dataset_systems(self) -> frozenset[SystemName]:
        return frozenset(d.system for d in self.declared_datasets())

    def effective_permitted_systems(self) -> tuple[SystemName, ...]:
        """Systems a remediation may target; defaults to the source and target (agent-design N5)."""
        systems = self.self_healing.permitted_systems
        if systems is None:
            systems = (self.source.system, self.target.system)
        return tuple(sorted(set(systems)))

    def pii_columns(self, dataset: str) -> tuple[ColumnName, ...]:
        for spec in self.declared_datasets():
            if spec.name == dataset:
                return spec.pii_columns
        return ()


# --- Business rules ----------------------------------------------------------------------------


class BusinessRule(Contract):
    rule_id: RuleId
    rule_type: UpperSnake
    column: ColumnName | None = None
    columns: tuple[ColumnName, ...] | None = None
    severity: Severity
    blocking: StrictBool
    expected: JsonObject
    params: JsonObject = Field(default_factory=dict)
    description: NonEmptyStr | None = None


class BusinessRuleSet(Contract):
    rule_set_id: DocumentId
    pipeline_id: PipelineId
    dataset: QualifiedName
    status: Literal["ACTIVE", "INACTIVE"] = "ACTIVE"
    rules: tuple[BusinessRule, ...] = Field(min_length=1)
    tags: Tags = ()


# --- Knowledge documents -----------------------------------------------------------------------


class SOPMetadata(SOPProposal):
    """A catalogued SOP: the proposal shape plus catalogue identity (src §33.5)."""

    sop_id: DocumentId
    status: Literal["APPROVED", "DRAFT", "DEPRECATED"]
    version: ShortText | None = None
    pipeline_ids: tuple[PipelineId, ...] = ()
    tags: Tags = ()

    @model_validator(mode="before")
    @classmethod
    def _ignore_derived_hash(cls, data: Any) -> Any:
        # content_sha256 is derived: accept it in round-tripped dumps, never trust it.
        if isinstance(data, dict) and "content_sha256" in data:
            data = {k: v for k, v in data.items() if k != "content_sha256"}
        return data

    @computed_field  # type: ignore[prop-decorator]
    @cached_property
    def content_sha256(self) -> str:
        """Pins this SOP's behavioural content in an approval (ADR-016)."""
        return sop_content_sha256(self.sop_id, self)


class Runbook(Contract):
    runbook_id: DocumentId
    title: ShortText
    status: Literal["ACTIVE", "RETIRED"] = "ACTIVE"
    pipeline_ids: tuple[PipelineId, ...] = ()
    systems: tuple[SystemName, ...] = ()
    categories: tuple[UpperSnake, ...] = ()
    steps: tuple[NonEmptyStr, ...] = Field(min_length=1)
    related_sops: tuple[DocumentId, ...] = ()
    tags: Tags = ()


class KnownIssue(Contract):
    issue_id: DocumentId
    title: ShortText
    category: UpperSnake
    status: Literal["ACTIVE", "RETIRED"] = "ACTIVE"
    pipeline_ids: tuple[PipelineId, ...] = ()
    error_signature: JsonObject = Field(default_factory=dict)
    symptoms: tuple[NonEmptyStr, ...] = ()
    likely_causes: tuple[NonEmptyStr, ...] = ()
    recommended_diagnostics: tuple[NonEmptyStr, ...] = ()
    related_sops: tuple[DocumentId, ...] = ()
    tags: Tags = ()


class Ownership(Contract):
    ownership_id: DocumentId
    pipeline_ids: tuple[PipelineId, ...] = Field(min_length=1)
    team: ShortText
    support_group: ShortText
    reviewers: tuple[ShortText, ...] = ()
    escalation: tuple[ShortText, ...] = ()
    tags: Tags = ()


class HistoricalIncident(Contract):
    """A past incident for reference only; never current evidence (src §33.3)."""

    historical_incident_id: DocumentId
    title: ShortText
    pipeline_ids: tuple[PipelineId, ...] = ()
    category: UpperSnake
    occurred_on: date
    root_cause: NonEmptyStr
    resolution: NonEmptyStr
    related_sops: tuple[DocumentId, ...] = ()
    related_known_issues: tuple[DocumentId, ...] = ()
    tags: Tags = ()


ContentModel = (
    PipelineMetadata
    | BusinessRuleSet
    | SOPMetadata
    | Runbook
    | KnownIssue
    | Ownership
    | HistoricalIncident
)


# --- Envelope, queries, health -----------------------------------------------------------------


class MetadataDocument(Contract):
    """Normalised envelope returned by every connector (src §33.6)."""

    document_id: DocumentId
    document_type: DocumentType
    title: ShortText
    source_type: SourceType = SourceType.STATIC
    pipeline_ids: tuple[PipelineId, ...] = ()
    systems: tuple[SystemName, ...] = ()
    categories: tuple[ShortText, ...] = ()
    tags: Tags = ()
    status: ShortText = "ACTIVE"
    version: ShortText | None = None
    content: JsonObject
    source_reference: ShortText | None = None


class MetadataQuery(Contract):
    """Filters combine with AND; list fields match any of their values."""

    pipeline_id: PipelineId | None = None
    system: SystemName | None = None
    category: ShortText | None = None
    document_types: tuple[DocumentType, ...] = ()
    tags: Tags = ()


class IssueSeverity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"


class ValidationIssue(Contract):
    severity: IssueSeverity
    code: UpperSnake
    message: NonEmptyStr
    file: ShortText | None = None
    document_id: ShortText | None = None
    location: ShortText | None = None


class RejectedCandidate(Contract):
    version: ShortText
    checked_at: UtcDatetime
    errors: tuple[ValidationIssue, ...]
    warnings: tuple[ValidationIssue, ...] = ()


class MetadataHealth(Contract):
    """LOADED: serving a valid snapshot. DEGRADED: still serving it, but a newer tree on disk was
    rejected. EMPTY: no valid tree has been loaded since startup."""

    state: Literal["LOADED", "DEGRADED", "EMPTY"]
    version: ShortText | None
    loaded_at: UtcDatetime
    counts_by_type: dict[DocumentType, int]
    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()
    rejected_candidate: RejectedCandidate | None = None


class ReloadResult(Contract):
    outcome: Literal["RELOADED", "UNCHANGED", "REJECTED"]
    previous_version: ShortText | None
    version: ShortText | None
    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()
    health: MetadataHealth


class ContextItem(Contract):
    """One reference-metadata item for agent context; S09/S16 add runtime sections."""

    source: Literal["metadata"] = "metadata"
    ref_id: DocumentId
    document_type: DocumentType
    version: ShortText
    fetched_at: datetime
    content: JsonObject


class MetadataContext(Contract):
    pipeline_id: PipelineId
    version: ShortText
    fetched_at: datetime
    items: tuple[ContextItem, ...]
