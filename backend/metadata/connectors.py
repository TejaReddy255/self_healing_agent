"""Connector seam for knowledge sources (src §35.2, FR-005).

Only the static connector exists. Jira/Confluence connectors are future work: they will normalise
their payloads into ``MetadataDocument`` (``source_type`` JIRA/CONFLUENCE) behind this Protocol, so
consumers do not change.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from backend.metadata.models import MetadataDocument, MetadataQuery
from backend.metadata.service import MetadataService


@runtime_checkable
class MetadataConnector(Protocol):
    async def search(self, query: MetadataQuery) -> list[MetadataDocument]: ...

    async def get(self, external_id: str) -> MetadataDocument | None: ...


class StaticMetadataConnector:
    """Serves the version-controlled ``metadata/`` tree from the active snapshot."""

    def __init__(self, service: MetadataService) -> None:
        self._service = service

    async def search(self, query: MetadataQuery) -> list[MetadataDocument]:
        return list(self._service.snapshot().search(query))

    async def get(self, external_id: str) -> MetadataDocument | None:
        return self._service.snapshot().get_document(external_id)
