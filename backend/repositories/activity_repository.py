"""SQL for ``activities`` (S02)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from backend.db.pool import Connection
from backend.domain.audit import Activity

_SELECT = "SELECT id, created_at, type, details, incident_id, pipeline_id FROM activities"


class ActivityRepository:
    async def insert(
        self,
        conn: Connection,
        type_: str,
        details: dict[str, Any],
        incident_id: UUID | None,
        pipeline_id: str | None,
    ) -> Activity:
        row = await conn.fetchrow(
            "INSERT INTO activities (type, details, incident_id, pipeline_id) "
            "VALUES ($1, $2, $3, $4) "
            "RETURNING id, created_at, type, details, incident_id, pipeline_id",
            type_,
            details,
            incident_id,
            pipeline_id,
        )
        if row is None:  # pragma: no cover - INSERT ... RETURNING always yields a row
            raise RuntimeError("activity insert returned no row")
        return Activity(**dict(row))

    async def list_recent(
        self, conn: Connection, limit: int, before_id: int | None
    ) -> list[Activity]:
        """Newest first; keyset pagination on ``id`` (pass the last id seen as ``before_id``)."""
        if before_id is None:
            rows = await conn.fetch(f"{_SELECT} ORDER BY id DESC LIMIT $1", limit)
        else:
            rows = await conn.fetch(
                f"{_SELECT} WHERE id < $1 ORDER BY id DESC LIMIT $2", before_id, limit
            )
        return [Activity(**dict(r)) for r in rows]
