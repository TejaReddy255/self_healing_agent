"""Build and validate a candidate metadata snapshot (src §33.14, S04).

``build_candidate`` reads the tree once, validates every document against its content model, then
runs cross-document checks. ERRORs block the candidate; WARNINGs are reported but do not. Messages
never echo document values.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from backend.domain.dq import Checkpoint
from backend.metadata.loader import (
    MetadataParseError,
    discover,
    file_sha256,
    parse,
    read_bytes,
    tree_version,
)
from backend.metadata.models import (
    BusinessRuleSet,
    ContentModel,
    DocumentType,
    HistoricalIncident,
    IssueSeverity,
    KnownIssue,
    MetadataDocument,
    Ownership,
    PipelineMetadata,
    Runbook,
    SOPMetadata,
    ValidationIssue,
)
from backend.metadata.repository import LoadedDocument, MetadataSnapshot

FORBIDDEN_RUNTIME_KEYS = frozenset(
    {
        "latest_run_id",
        "last_run_id",
        "last_run_status",
        "latest_dag_state",
        "current_task_status",
        "failed_task",
        "retry_count",
        "current_runtime",
        "last_error",
        "recovery_status",
    }
)
# Bounds the report size for a badly broken document.
MAX_SCHEMA_ISSUES_PER_DOCUMENT = 20

MODELS: dict[DocumentType, tuple[type[ContentModel], str]] = {
    DocumentType.PIPELINE: (PipelineMetadata, "pipeline_id"),
    DocumentType.BUSINESS_RULE: (BusinessRuleSet, "rule_set_id"),
    DocumentType.SOP: (SOPMetadata, "sop_id"),
    DocumentType.RUNBOOK: (Runbook, "runbook_id"),
    DocumentType.KNOWN_ISSUE: (KnownIssue, "issue_id"),
    DocumentType.OWNERSHIP: (Ownership, "ownership_id"),
    DocumentType.HISTORICAL_INCIDENT: (HistoricalIncident, "historical_incident_id"),
}


@dataclass(frozen=True)
class Candidate:
    version: str
    snapshot: MetadataSnapshot | None
    errors: tuple[ValidationIssue, ...]
    warnings: tuple[ValidationIssue, ...]

    @property
    def valid(self) -> bool:
        return self.snapshot is not None


def current_version(root: Path, max_bytes: int) -> str:
    """The tree version without parsing: cheap enough for periodic refresh checks."""
    discovery = discover(root)
    hashes: dict[str, str] = {}
    for source in discovery.files:
        try:
            hashes[source.relative] = file_sha256(source, max_bytes)
        except OSError:
            hashes[source.relative] = "unreadable"
    for issue in discovery.issues:
        if issue.code == "PATH_ESCAPE" and issue.file:
            hashes[issue.file] = "escape"
    return tree_version(hashes)


class _Report:
    def __init__(self) -> None:
        self.errors: list[ValidationIssue] = []
        self.warnings: list[ValidationIssue] = []

    def add(
        self,
        severity: IssueSeverity,
        code: str,
        message: str,
        *,
        file: str | None = None,
        document_id: str | None = None,
        location: str | None = None,
    ) -> None:
        issue = ValidationIssue(
            severity=severity,
            code=code,
            message=message,
            file=file,
            document_id=document_id,
            location=location,
        )
        (self.errors if severity is IssueSeverity.ERROR else self.warnings).append(issue)

    def error(self, code: str, message: str, **where: str | None) -> None:
        self.add(IssueSeverity.ERROR, code, message, **where)

    def warning(self, code: str, message: str, **where: str | None) -> None:
        self.add(IssueSeverity.WARNING, code, message, **where)


def _forbidden_keys(value: Any, path: str = "") -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            location = f"{path}.{key}" if path else str(key)
            if key in FORBIDDEN_RUNTIME_KEYS:
                yield location
            yield from _forbidden_keys(item, location)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _forbidden_keys(item, f"{path}[{index}]")


def _short_form_dq_profile(data: dict[str, Any]) -> str | None:
    profile = data.get("dq_profile")
    if not isinstance(profile, dict):
        return None
    checkpoints = {c.value for c in Checkpoint}
    for key in profile:
        if key not in checkpoints:
            return str(key)
    return None


def _envelope(document_type: DocumentType, model: ContentModel, relative: str) -> MetadataDocument:
    pipeline_ids: tuple[str, ...]
    systems: tuple[Any, ...] = ()
    categories: tuple[str, ...] = ()
    status = "ACTIVE"
    version: str | None = None
    match model:
        case PipelineMetadata():
            document_id, title, pipeline_ids = model.pipeline_id, model.name, (model.pipeline_id,)
            systems = tuple(sorted(model.dataset_systems()))
            status = model.status
        case BusinessRuleSet():
            document_id, title, pipeline_ids = (
                model.rule_set_id,
                model.rule_set_id,
                (model.pipeline_id,),
            )
            status = model.status
        case SOPMetadata():
            document_id, title, pipeline_ids = model.sop_id, model.title, model.pipeline_ids
            systems, categories = (model.target_system,), (model.category,)
            status, version = model.status, model.version
        case Runbook():
            document_id, title, pipeline_ids = model.runbook_id, model.title, model.pipeline_ids
            systems, categories, status = model.systems, model.categories, model.status
        case KnownIssue():
            document_id, title, pipeline_ids = model.issue_id, model.title, model.pipeline_ids
            categories, status = (model.category,), model.status
        case Ownership():
            document_id, title, pipeline_ids = model.ownership_id, model.team, model.pipeline_ids
        case HistoricalIncident():
            document_id = model.historical_incident_id
            title, pipeline_ids, categories = model.title, model.pipeline_ids, (model.category,)
    return MetadataDocument(
        document_id=document_id,
        document_type=document_type,
        title=title,
        pipeline_ids=pipeline_ids,
        systems=systems,
        categories=categories,
        tags=model.tags,
        status=status,
        version=version,
        content=model.model_dump(mode="json"),
        source_reference=relative,
    )


def _load_documents(
    root: Path, max_bytes: int, report: _Report
) -> tuple[str, list[LoadedDocument]]:
    discovery = discover(root)
    for issue in discovery.issues:
        (report.errors if issue.severity is IssueSeverity.ERROR else report.warnings).append(issue)
    hashes: dict[str, str] = {
        i.file: "escape" for i in discovery.issues if i.code == "PATH_ESCAPE" and i.file
    }
    loaded: list[LoadedDocument] = []
    seen_ids: dict[str, str] = {}

    for source in discovery.files:
        relative = source.relative
        try:
            hashes[relative] = file_sha256(source, max_bytes)
            data = parse(source, read_bytes(source, max_bytes))
        except MetadataParseError as exc:
            report.error(exc.code, exc.message, file=relative)
            continue
        except OSError:
            hashes[relative] = "unreadable"
            report.error("YAML_INVALID", "file cannot be read", file=relative)
            continue

        model_type, id_field = MODELS[source.document_type]
        raw_id = data.get(id_field)
        document_id = raw_id if isinstance(raw_id, str) else None

        forbidden = list(_forbidden_keys(data))
        for location in forbidden:
            report.error(
                "FORBIDDEN_RUNTIME_KEY",
                "live runtime state does not belong in static metadata (read it from Airflow)",
                file=relative,
                document_id=document_id,
                location=location,
            )
        if forbidden:
            continue

        if source.document_type is DocumentType.PIPELINE and (key := _short_form_dq_profile(data)):
            report.error(
                "SCHEMA_INVALID",
                "dq_profile keys must be checkpoint names (SOURCE, STAGING, TARGET, "
                "RECONCILIATION, SCHEDULED) with a list of agent entries; the short form is "
                "not supported (architecture §18 C9)",
                file=relative,
                document_id=document_id,
                location=f"dq_profile.{key}",
            )
            continue

        try:
            model = model_type.model_validate(data)
        except ValidationError as exc:
            errors = exc.errors(include_url=False, include_input=False, include_context=False)
            for err in errors[:MAX_SCHEMA_ISSUES_PER_DOCUMENT]:
                report.error(
                    "SCHEMA_INVALID",
                    err["msg"],
                    file=relative,
                    document_id=document_id,
                    location=".".join(str(p) for p in err["loc"]) or None,
                )
            continue

        envelope = _envelope(source.document_type, model, relative)
        first = seen_ids.get(envelope.document_id)
        if first is not None:
            report.error(
                "DUPLICATE_ID",
                f"document_id is already defined in {first}",
                file=relative,
                document_id=envelope.document_id,
            )
            continue
        seen_ids[envelope.document_id] = relative
        loaded.append(LoadedDocument(source.document_type, envelope.document_id, model, envelope))
    return tree_version(hashes), loaded


def _cross_check(documents: list[LoadedDocument], report: _Report) -> None:
    def of(model_type: type[Any]) -> list[tuple[Any, str]]:
        return [
            (d.model, d.envelope.source_reference or "")
            for d in documents
            if isinstance(d.model, model_type)
        ]

    pipelines = {p.pipeline_id: p for p, _ in of(PipelineMetadata)}
    sops = {s.sop_id: s for s, _ in of(SOPMetadata)}
    known_issues = {k.issue_id for k, _ in of(KnownIssue)}
    rule_sets = of(BusinessRuleSet)

    def check_pipelines(ids: tuple[str, ...], file: str, doc_id: str, field: str) -> None:
        for pipeline_id in ids:
            if pipeline_id not in pipelines:
                report.error(
                    "BROKEN_REFERENCE",
                    f"unknown pipeline {pipeline_id!r}",
                    file=file,
                    document_id=doc_id,
                    location=field,
                )

    def check_sops(ids: tuple[str, ...], file: str, doc_id: str) -> None:
        for sop_id in ids:
            sop = sops.get(sop_id)
            if sop is None:
                report.error(
                    "BROKEN_REFERENCE",
                    f"unknown SOP {sop_id!r}",
                    file=file,
                    document_id=doc_id,
                    location="related_sops",
                )
            elif sop.status != "APPROVED":
                report.warning(
                    "DEPRECATED_REFERENCE",
                    f"SOP {sop_id!r} is {sop.status}",
                    file=file,
                    document_id=doc_id,
                    location="related_sops",
                )

    # Duplicate DAG links: S13 maps an incoming dag_id to exactly one pipeline.
    dag_owner: dict[str, str] = {}
    for pipeline, file in of(PipelineMetadata):
        dag_id = pipeline.airflow.dag_id
        if dag_id in dag_owner:
            report.error(
                "DUPLICATE_ID",
                f"airflow.dag_id {dag_id!r} is already linked to pipeline {dag_owner[dag_id]!r}",
                file=file,
                document_id=pipeline.pipeline_id,
                location="airflow.dag_id",
            )
        else:
            dag_owner[dag_id] = pipeline.pipeline_id

    # Rule IDs are unique across all rule sets; rule sets point at their pipeline's datasets.
    rule_owner: dict[str, str] = {}
    rules_by_pipeline: dict[str, set[str]] = defaultdict(set)
    for rule_set, file in rule_sets:
        check_pipelines((rule_set.pipeline_id,), file, rule_set.rule_set_id, "pipeline_id")
        pipeline = pipelines.get(rule_set.pipeline_id)
        if pipeline is not None and rule_set.dataset not in {
            d.name for d in pipeline.declared_datasets()
        }:
            report.error(
                "BROKEN_REFERENCE",
                f"dataset {rule_set.dataset!r} is not declared by pipeline "
                f"{pipeline.pipeline_id!r}",
                file=file,
                document_id=rule_set.rule_set_id,
                location="dataset",
            )
        for rule in rule_set.rules:
            if rule.rule_id in rule_owner:
                report.error(
                    "DUPLICATE_ID",
                    f"rule_id {rule.rule_id!r} is already defined in {rule_owner[rule.rule_id]!r}",
                    file=file,
                    document_id=rule_set.rule_set_id,
                    location="rules",
                )
            else:
                rule_owner[rule.rule_id] = rule_set.rule_set_id
            rules_by_pipeline[rule_set.pipeline_id].add(rule.rule_id)

    for pipeline, file in of(PipelineMetadata):
        pid = pipeline.pipeline_id
        for checkpoint, entries in pipeline.dq_profile.root.items():
            for index, entry in enumerate(entries):
                for rule_id in entry.rules or ():
                    if rule_id not in rules_by_pipeline[pid]:
                        report.error(
                            "BROKEN_REFERENCE",
                            f"rule {rule_id!r} is not defined in this pipeline's rule sets",
                            file=file,
                            document_id=pid,
                            location=f"dq_profile.{checkpoint.value}[{index}].rules",
                        )
        permitted = pipeline.self_healing.permitted_systems
        if permitted is not None and not set(permitted) <= pipeline.dataset_systems():
            report.error(
                "SCOPE_INVALID",
                "permitted_systems must be systems of the pipeline's own datasets",
                file=file,
                document_id=pid,
                location="self_healing.permitted_systems",
            )
        sla = pipeline.sla
        if sla.enabled and sla.expected_completion_time is None and sla.max_runtime_minutes is None:
            report.error(
                "SCOPE_INVALID",
                "an enabled SLA needs expected_completion_time or max_runtime_minutes",
                file=file,
                document_id=pid,
                location="sla",
            )
        try:
            ZoneInfo(sla.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            report.error(
                "SCOPE_INVALID",
                "sla.timezone is not a known IANA time zone",
                file=file,
                document_id=pid,
                location="sla.timezone",
            )

    for sop, file in of(SOPMetadata):
        check_pipelines(sop.pipeline_ids, file, sop.sop_id, "pipeline_ids")
    for runbook, file in of(Runbook):
        check_pipelines(runbook.pipeline_ids, file, runbook.runbook_id, "pipeline_ids")
        check_sops(runbook.related_sops, file, runbook.runbook_id)
    for issue, file in of(KnownIssue):
        check_pipelines(issue.pipeline_ids, file, issue.issue_id, "pipeline_ids")
        check_sops(issue.related_sops, file, issue.issue_id)
    for history, file in of(HistoricalIncident):
        doc_id = history.historical_incident_id
        check_pipelines(history.pipeline_ids, file, doc_id, "pipeline_ids")
        check_sops(history.related_sops, file, doc_id)
        for issue_id in history.related_known_issues:
            if issue_id not in known_issues:
                report.error(
                    "BROKEN_REFERENCE",
                    f"unknown known issue {issue_id!r}",
                    file=file,
                    document_id=doc_id,
                    location="related_known_issues",
                )

    owners: dict[str, list[str]] = defaultdict(list)
    for owner, file in of(Ownership):
        check_pipelines(owner.pipeline_ids, file, owner.ownership_id, "pipeline_ids")
        for pipeline_id in owner.pipeline_ids:
            owners[pipeline_id].append(owner.ownership_id)
    for pipeline_id, owner_ids in owners.items():
        if len(owner_ids) > 1:
            report.error(
                "OWNERSHIP_CONFLICT",
                f"pipeline {pipeline_id!r} has several ownership documents: "
                f"{', '.join(sorted(owner_ids))}",
                document_id=pipeline_id,
            )
    for pipeline_id in sorted(pipelines):
        if pipeline_id not in owners:
            report.warning(
                "MISSING_OWNERSHIP", "pipeline has no ownership document", document_id=pipeline_id
            )


def build_candidate(root: Path, max_bytes: int, loaded_at: datetime) -> Candidate:
    """Load and validate the tree at ``root``. The snapshot is ``None`` when any ERROR exists."""
    report = _Report()
    version, documents = _load_documents(root, max_bytes, report)
    _cross_check(documents, report)
    errors, warnings = tuple(report.errors), tuple(report.warnings)
    snapshot = None if errors else MetadataSnapshot.build(version, loaded_at, documents, warnings)
    return Candidate(version=version, snapshot=snapshot, errors=errors, warnings=warnings)
