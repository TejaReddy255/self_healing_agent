"""Immutable, indexed metadata snapshot (S04).

A snapshot is built once from a validated tree and never mutated. Reload replaces the whole
snapshot reference, so a reader that holds one snapshot sees a consistent view. Every query result
is sorted by document ID, independent of file discovery order.

Applicability: a SOP or runbook applies to a pipeline when it lists the pipeline, or lists no
pipelines and targets one of the pipeline's systems (SOPs: its permitted systems; runbooks: any of
its dataset systems). ``search`` instead filters the envelope fields literally.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import TypeVar

from backend.domain.common import SystemName
from backend.metadata.models import (
    BusinessRuleSet,
    ContentModel,
    DocumentType,
    HistoricalIncident,
    KnownIssue,
    MetadataDocument,
    MetadataQuery,
    Ownership,
    PipelineMetadata,
    Runbook,
    SOPMetadata,
    ValidationIssue,
)

M = TypeVar("M", bound=ContentModel)


@dataclass(frozen=True)
class LoadedDocument:
    document_type: DocumentType
    document_id: str
    model: ContentModel
    envelope: MetadataDocument


def _freeze(
    documents: Iterable[LoadedDocument],
) -> Mapping[DocumentType, Mapping[str, LoadedDocument]]:
    by_type: dict[DocumentType, dict[str, LoadedDocument]] = {t: {} for t in DocumentType}
    for doc in documents:
        by_type[doc.document_type][doc.document_id] = doc
    return MappingProxyType(
        {t: MappingProxyType({k: docs[k] for k in sorted(docs)}) for t, docs in by_type.items()}
    )


@dataclass(frozen=True)
class MetadataSnapshot:
    version: str | None
    loaded_at: datetime
    documents: Mapping[DocumentType, Mapping[str, LoadedDocument]]
    warnings: tuple[ValidationIssue, ...] = ()
    # Only an EMPTY snapshot (no valid tree since startup) carries errors.
    errors: tuple[ValidationIssue, ...] = ()
    _by_id: Mapping[str, LoadedDocument] = field(default_factory=dict, repr=False)
    _by_dag: Mapping[str, PipelineMetadata] = field(default_factory=dict, repr=False)

    @classmethod
    def build(
        cls,
        version: str,
        loaded_at: datetime,
        documents: Iterable[LoadedDocument],
        warnings: tuple[ValidationIssue, ...] = (),
    ) -> MetadataSnapshot:
        frozen = _freeze(documents)
        by_id = {doc_id: doc for docs in frozen.values() for doc_id, doc in docs.items()}
        by_dag = {
            doc.model.airflow.dag_id: doc.model
            for doc in frozen[DocumentType.PIPELINE].values()
            if isinstance(doc.model, PipelineMetadata)
        }
        return cls(
            version=version,
            loaded_at=loaded_at,
            documents=frozen,
            warnings=warnings,
            _by_id=MappingProxyType(by_id),
            _by_dag=MappingProxyType(by_dag),
        )

    @classmethod
    def empty(
        cls,
        loaded_at: datetime,
        errors: tuple[ValidationIssue, ...] = (),
        warnings: tuple[ValidationIssue, ...] = (),
    ) -> MetadataSnapshot:
        return cls(
            version=None,
            loaded_at=loaded_at,
            documents=_freeze(()),
            warnings=warnings,
            errors=errors,
        )

    @property
    def is_empty(self) -> bool:
        return self.version is None

    # --- generic access ---------------------------------------------------------------------

    def _models(self, document_type: DocumentType, model_type: type[M]) -> tuple[M, ...]:
        return tuple(
            doc.model
            for doc in self.documents[document_type].values()
            if isinstance(doc.model, model_type)
        )

    def counts_by_type(self) -> dict[DocumentType, int]:
        return {t: len(self.documents[t]) for t in DocumentType}

    def get_document(self, document_id: str) -> MetadataDocument | None:
        """A copy: envelope ``content`` dicts must never be edited in the shared snapshot."""
        doc = self._by_id.get(document_id)
        return doc.envelope.model_copy(deep=True) if doc else None

    # --- pipelines --------------------------------------------------------------------------

    def get_pipeline(self, pipeline_id: str) -> PipelineMetadata | None:
        doc = self.documents[DocumentType.PIPELINE].get(pipeline_id)
        return doc.model if doc and isinstance(doc.model, PipelineMetadata) else None

    def get_pipeline_by_dag_id(self, dag_id: str) -> PipelineMetadata | None:
        return self._by_dag.get(dag_id)

    def list_pipelines(self) -> tuple[PipelineMetadata, ...]:
        return self._models(DocumentType.PIPELINE, PipelineMetadata)

    # --- knowledge by pipeline --------------------------------------------------------------

    def business_rules(self, pipeline_id: str) -> tuple[BusinessRuleSet, ...]:
        return tuple(
            r
            for r in self._models(DocumentType.BUSINESS_RULE, BusinessRuleSet)
            if r.pipeline_id == pipeline_id
        )

    def get_sop(self, sop_id: str) -> SOPMetadata | None:
        doc = self.documents[DocumentType.SOP].get(sop_id)
        return doc.model if doc and isinstance(doc.model, SOPMetadata) else None

    def sops(
        self, pipeline_id: str, category: str | None = None, *, approved_only: bool = True
    ) -> tuple[SOPMetadata, ...]:
        """SOPs that apply to the pipeline (APPROVED only unless ``approved_only=False``).

        A SOP applies when it lists the pipeline, or lists no pipelines and targets one of the
        pipeline's permitted systems.
        """
        pipeline = self.get_pipeline(pipeline_id)
        if pipeline is None:
            return ()
        permitted = set(pipeline.effective_permitted_systems())
        return tuple(
            s
            for s in self._models(DocumentType.SOP, SOPMetadata)
            if (not approved_only or s.status == "APPROVED")
            and (category is None or s.category == category)
            and (
                pipeline_id in s.pipeline_ids
                or (not s.pipeline_ids and s.target_system in permitted)
            )
        )

    def runbooks(self, pipeline_id: str, category: str | None = None) -> tuple[Runbook, ...]:
        pipeline = self.get_pipeline(pipeline_id)
        if pipeline is None:
            return ()
        systems = pipeline.dataset_systems()
        return tuple(
            r
            for r in self._models(DocumentType.RUNBOOK, Runbook)
            if (category is None or category in r.categories)
            and (
                pipeline_id in r.pipeline_ids
                or (not r.pipeline_ids and bool(set(r.systems) & systems))
            )
        )

    def known_issues(self, pipeline_id: str, category: str | None = None) -> tuple[KnownIssue, ...]:
        return tuple(
            k
            for k in self._models(DocumentType.KNOWN_ISSUE, KnownIssue)
            if pipeline_id in k.pipeline_ids and (category is None or k.category == category)
        )

    def historical_incidents(
        self, pipeline_id: str, category: str | None = None
    ) -> tuple[HistoricalIncident, ...]:
        return tuple(
            h
            for h in self._models(DocumentType.HISTORICAL_INCIDENT, HistoricalIncident)
            if pipeline_id in h.pipeline_ids and (category is None or h.category == category)
        )

    def ownership(self, pipeline_id: str) -> Ownership | None:
        for owner in self._models(DocumentType.OWNERSHIP, Ownership):
            if pipeline_id in owner.pipeline_ids:
                return owner
        return None

    # --- unfiltered lists (API browsing) ------------------------------------------------------

    def list_sops(
        self,
        *,
        pipeline_id: str | None = None,
        category: str | None = None,
        status: str | None = "APPROVED",
    ) -> tuple[SOPMetadata, ...]:
        """SOPs filtered by applicability (when ``pipeline_id`` is set), category and status."""
        candidates = (
            self._models(DocumentType.SOP, SOPMetadata)
            if pipeline_id is None
            else self.sops(pipeline_id, approved_only=False)
        )
        return tuple(
            s
            for s in candidates
            if (category is None or s.category == category)
            and (status is None or s.status == status)
        )

    def all_business_rules(self) -> tuple[BusinessRuleSet, ...]:
        return self._models(DocumentType.BUSINESS_RULE, BusinessRuleSet)

    def all_runbooks(self, category: str | None = None) -> tuple[Runbook, ...]:
        return tuple(
            r
            for r in self._models(DocumentType.RUNBOOK, Runbook)
            if category is None or category in r.categories
        )

    def all_known_issues(self, category: str | None = None) -> tuple[KnownIssue, ...]:
        return tuple(
            k
            for k in self._models(DocumentType.KNOWN_ISSUE, KnownIssue)
            if category is None or k.category == category
        )

    def all_ownership(self) -> tuple[Ownership, ...]:
        return self._models(DocumentType.OWNERSHIP, Ownership)

    # --- search -----------------------------------------------------------------------------

    def search(self, query: MetadataQuery) -> tuple[MetadataDocument, ...]:
        types = query.document_types or tuple(DocumentType)
        results: list[MetadataDocument] = []
        for document_type in sorted(types):
            for doc in self.documents[document_type].values():
                env = doc.envelope
                if query.pipeline_id is not None and query.pipeline_id not in env.pipeline_ids:
                    continue
                if query.system is not None and SystemName(query.system) not in env.systems:
                    continue
                if query.category is not None and query.category not in env.categories:
                    continue
                if query.tags and not set(query.tags) & set(env.tags):
                    continue
                results.append(env.model_copy(deep=True))
        return tuple(results)
