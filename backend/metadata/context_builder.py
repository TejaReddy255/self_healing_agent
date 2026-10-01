"""Reference-metadata bundle for agent context (src §33.7 MetadataContextBuilder, S04).

S04 provides only the static *reference* part. S09/S16 combine it with current runtime evidence
(Airflow, DQ, monitoring events), which always outranks it (src §33.12).
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

from backend.domain.common import JsonObject
from backend.metadata.models import ContextItem, DocumentType, MetadataContext
from backend.metadata.repository import MetadataSnapshot
from backend.metadata.service import MetadataReader

# (pipeline_id, category) -> SOP documents as (sop_id, content). S10's SOPResolver plugs in here
# to add runtime-approved SOPs (ADR-008); the default serves the static YAML SOPs.
SopSource = Callable[[str, str | None], tuple[tuple[str, JsonObject], ...]]

# Section order follows the evidence authority model (src §33.1, Figure 8).
_SECTION_ORDER = (
    DocumentType.SOP,
    DocumentType.PIPELINE,
    DocumentType.BUSINESS_RULE,
    DocumentType.KNOWN_ISSUE,
    DocumentType.HISTORICAL_INCIDENT,
    DocumentType.RUNBOOK,
    DocumentType.OWNERSHIP,
)


class MetadataContextBuilder:
    def __init__(self, service: MetadataReader, sop_source: SopSource | None = None) -> None:
        self._service = service
        self._sop_source = sop_source

    async def build(self, pipeline_id: str, category: str | None = None) -> MetadataContext | None:
        """The pipeline's reference metadata from one snapshot, or None for an unknown pipeline."""
        snap = self._service.snapshot()
        pipeline = snap.get_pipeline(pipeline_id)
        if pipeline is None or snap.version is None:
            return None
        ids: dict[DocumentType, list[str]] = {
            DocumentType.PIPELINE: [pipeline.pipeline_id],
            DocumentType.BUSINESS_RULE: [r.rule_set_id for r in snap.business_rules(pipeline_id)],
            DocumentType.RUNBOOK: [r.runbook_id for r in snap.runbooks(pipeline_id, category)],
            DocumentType.KNOWN_ISSUE: [
                k.issue_id for k in snap.known_issues(pipeline_id, category)
            ],
            DocumentType.HISTORICAL_INCIDENT: [
                h.historical_incident_id for h in snap.historical_incidents(pipeline_id, category)
            ],
            DocumentType.OWNERSHIP: [
                o.ownership_id for o in (snap.ownership(pipeline_id),) if o is not None
            ],
        }
        sops = (
            self._sop_source(pipeline_id, category)
            if self._sop_source is not None
            else tuple(
                (s.sop_id, s.model_dump(mode="json")) for s in snap.sops(pipeline_id, category)
            )
        )
        items = tuple(
            ContextItem(
                ref_id=sop_id,
                document_type=DocumentType.SOP,
                version=snap.version,
                fetched_at=snap.loaded_at,
                content=deepcopy(content),
            )
            for sop_id, content in sops
        ) + tuple(
            _item(snap, document_type, document_id)
            for document_type in _SECTION_ORDER
            if document_type is not DocumentType.SOP
            for document_id in ids[document_type]
        )
        return MetadataContext(
            pipeline_id=pipeline_id, version=snap.version, fetched_at=snap.loaded_at, items=items
        )


def _item(snap: MetadataSnapshot, document_type: DocumentType, document_id: str) -> ContextItem:
    envelope = snap.get_document(document_id)
    if envelope is None or snap.version is None:  # pragma: no cover - IDs come from this snapshot
        raise LookupError(f"document {document_id!r} is not in the snapshot")
    return ContextItem(
        ref_id=document_id,
        document_type=document_type,
        version=snap.version,
        fetched_at=snap.loaded_at,
        content=envelope.content,
    )
