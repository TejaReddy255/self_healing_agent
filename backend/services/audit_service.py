"""Audit recording (S02). Redaction and size limits are applied here, never left to callers:
the audit table is a data-leak surface."""

from __future__ import annotations

import json
from typing import Any

from backend.db.pool import Database
from backend.domain.audit import AuditEvent
from backend.observability.context import current_request_id
from backend.observability.redaction import redact_value
from backend.repositories.audit_repository import AuditRepository

TRUNCATION_MARKER = "_truncated"


def prepare_payload(value: dict[str, Any] | None, max_bytes: int) -> dict[str, Any] | None:
    """Redact a JSON payload and cap its serialized size.

    Oversized payloads are replaced by ``{"_truncated": true, "original_bytes": n, "preview": ...}``
    where the preview is the redacted JSON cut to half the budget.
    """
    if value is None:
        return None
    redacted = redact_value(value)
    encoded = json.dumps(redacted, default=str, ensure_ascii=False)
    size = len(encoded.encode("utf-8"))
    if size <= max_bytes:
        result: dict[str, Any] = json.loads(encoded)
        return result
    preview = encoded.encode("utf-8")[: max_bytes // 2].decode("utf-8", errors="ignore")
    return {TRUNCATION_MARKER: True, "original_bytes": size, "preview": preview}


class AuditService:
    def __init__(
        self,
        database: Database,
        max_payload_bytes: int,
        repository: AuditRepository | None = None,
    ) -> None:
        self._database = database
        self._max_payload_bytes = max_payload_bytes
        self._repository = repository or AuditRepository()

    def sanitize(self, event: AuditEvent) -> AuditEvent:
        return event.model_copy(
            update={
                "request_id": event.request_id or current_request_id(),
                "input": prepare_payload(event.input, self._max_payload_bytes),
                "output": prepare_payload(event.output, self._max_payload_bytes),
                "details": prepare_payload(event.details, self._max_payload_bytes),
            }
        )

    async def record(self, event: AuditEvent) -> int:
        """Persist one audit event and return its id."""
        async with self._database.transaction() as conn:
            return await self._repository.insert(conn, self.sanitize(event))
