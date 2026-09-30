"""Activity stream (S02): persist, count, and push to live WebSocket subscribers.

The broadcaster is in-process (single API instance in v1). Cross-process fan-out via
PostgreSQL LISTEN/NOTIFY is S23 scope.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from backend.db.pool import Database
from backend.domain.audit import Activity
from backend.repositories.activity_repository import ActivityRepository
from backend.repositories.performance_repository import PerformanceRepository
from backend.services.audit_service import prepare_payload

logger = logging.getLogger(__name__)


class ActivityBroadcaster:
    """Fan-out to per-subscriber bounded queues. A slow subscriber loses its oldest items rather
    than blocking publishers or other subscribers."""

    def __init__(self, queue_size: int = 100) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[Activity]] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue[Activity]]:
        queue: asyncio.Queue[Activity] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    def publish(self, activity: Activity) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                queue.get_nowait()
                logger.warning("activity subscriber is slow; dropped oldest queued activity")
            queue.put_nowait(activity)


class ActivityService:
    def __init__(
        self,
        database: Database,
        broadcaster: ActivityBroadcaster,
        max_details_bytes: int,
        activities: ActivityRepository | None = None,
        performance: PerformanceRepository | None = None,
    ) -> None:
        self._database = database
        self._broadcaster = broadcaster
        self._max_details_bytes = max_details_bytes
        self._activities = activities or ActivityRepository()
        self._performance = performance or PerformanceRepository()

    async def publish(
        self,
        type_: str,
        details: dict[str, Any] | None = None,
        *,
        incident_id: UUID | None = None,
        pipeline_id: str | None = None,
    ) -> Activity:
        """Persist an activity and bump ``total_activities`` atomically, then notify subscribers.

        Subscribers are notified only after the transaction commits, so they never see an
        activity that was rolled back.
        """
        safe_details = prepare_payload(details or {}, self._max_details_bytes) or {}
        async with self._database.transaction() as conn:
            activity = await self._activities.insert(
                conn, type_, safe_details, incident_id, pipeline_id
            )
            await self._performance.increment(conn, "total_activities")
        self._broadcaster.publish(activity)
        return activity

    async def list_recent(self, limit: int, before_id: int | None) -> list[Activity]:
        async with self._database.connection() as conn:
            return await self._activities.list_recent(conn, limit, before_id)
