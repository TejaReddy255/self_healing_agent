"""Reference-metadata bundle for agent context (src §33.7 MetadataContextBuilder, S04).

S04 provides only the static *reference* part. S09/S16 combine it with current runtime evidence
(Airflow, DQ, monitoring events), which always outranks it (src §33.12).
"""

from __future__ import annotations

from backend.metadata.models import ContextItem, DocumentType, MetadataContext
from backend.metadata.repository import MetadataSnapshot
from backend.metadata.service import MetadataService

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
    def __init__(self, service: MetadataService) -> None:
        self._service = service

    async def build(self, pipeline_id: str, category: str | None = None) -> MetadataContext | None:
        """The pipeline's reference metadata from one snapshot, or None for an unknown pipeline."""
        snap = self._service.snapshot()
        pipeline = snap.get_pipeline(pipeline_id)
        if pipeline is None or snap.version is None:
            return None
        ids: dict[DocumentType, list[str]] = {
            DocumentType.PIPELINE: [pipeline.pipeline_id],
            DocumentType.BUSINESS_RULE: [r.rule_set_id for r in snap.business_rules(pipeline_id)],
            DocumentType.SOP: [s.sop_id for s in snap.sops(pipeline_id, category)],
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
        items = tuple(
            _item(snap, document_type, document_id)
            for document_type in _SECTION_ORDER
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
