"""Audit recording (S02). Redaction, JSON-safety and size limits are applied here, never left to
callers: the audit table is a data-leak surface and must accept every legitimate event."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.db.pool import Connection, Database
from backend.domain.audit import AuditEvent
from backend.observability.context import current_request_id
from backend.observability.redaction import redact_value
from backend.repositories.audit_repository import AuditRepository

TRUNCATION_MARKER = "_truncated"
# Tolerated clock skew for caller-supplied event times; later timestamps are rejected.
MAX_FUTURE_SKEW = timedelta(minutes=5)


def json_safe(value: Any) -> Any:
    """Make a redacted value storable as PostgreSQL JSONB/text.

    JSONB rejects NaN/Infinity and the NUL character (``\\u0000``), both of which occur in real
    inputs (DQ statistics, Airflow log excerpts). Non-finite floats become their string form
    and NUL characters are removed, in keys and values.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {str(k).replace("\x00", ""): json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(v) for v in value]
    return value


def prepare_payload(value: dict[str, Any] | None, max_bytes: int) -> dict[str, Any] | None:
    """Redact a JSON payload, make it JSONB-safe and cap its serialized size.

    Oversized payloads are replaced by ``{"_truncated": true, "original_bytes": n, "preview": ...}``
    where the preview is the redacted JSON cut to half the budget.
    """
    if value is None:
        return None
    safe = json_safe(redact_value(value))
    encoded = json.dumps(safe, default=str, ensure_ascii=False, allow_nan=False)
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
        if (
            event.occurred_at is not None
            and event.occurred_at > datetime.now(UTC) + MAX_FUTURE_SKEW
        ):
            raise ValueError("audit occurred_at lies in the future")
        text_fields = {
            name: value.replace("\x00", "")
            for name, value in event.model_dump().items()
            if isinstance(value, str)
        }
        return event.model_copy(
            update={
                **text_fields,
                "request_id": text_fields.get("request_id") or current_request_id(),
                "input": prepare_payload(event.input, self._max_payload_bytes),
                "output": prepare_payload(event.output, self._max_payload_bytes),
                "details": prepare_payload(event.details, self._max_payload_bytes),
            }
        )

    async def record(self, event: AuditEvent, *, conn: Connection | None = None) -> int:
        """Persist one audit event and return its id.

        Pass ``conn`` to write inside the caller's transaction, so a material state change and
        its audit row commit (or roll back) together. Without it, the event gets its own
        transaction.
        """
        sanitized = self.sanitize(event)
        if conn is not None:
            return await self._repository.insert(conn, sanitized)
        async with self._database.transaction() as own:
            return await self._repository.insert(own, sanitized)
