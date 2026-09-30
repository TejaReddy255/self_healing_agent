"""SQL for ``operation_audit`` (S02)."""

from __future__ import annotations

from typing import cast

from backend.db.pool import Connection
from backend.domain.audit import AuditEvent

_COLUMNS = (
    "phase",
    "event_type",
    "operation",
    "tool_name",
    "server",
    "status",
    "duration_ms",
    "actor_subject",
    "actor_role",
    "request_id",
    "pipeline_id",
    "dag_id",
    "run_id",
    "task_id",
    "analysis_id",
    "review_id",
    "job_id",
    "incident_id",
    "input",
    "output",
    "details",
)

# Built only from the constant column list above; all values are bind parameters.
_INSERT = (
    f"INSERT INTO operation_audit ({', '.join(_COLUMNS)}, occurred_at) "  # noqa: S608
    f"VALUES ({', '.join(f'${i}' for i in range(1, len(_COLUMNS) + 1))}, "
    f"COALESCE(${len(_COLUMNS) + 1}, now())) RETURNING id"
)


class AuditRepository:
    async def insert(self, conn: Connection, event: AuditEvent) -> int:
        values = [getattr(event, column) for column in _COLUMNS]
        return cast(int, await conn.fetchval(_INSERT, *values, event.occurred_at))
