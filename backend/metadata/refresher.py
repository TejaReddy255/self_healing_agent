"""Periodic metadata refresh so every process follows reloads (decision 2026-10-01, S04).

The API, the monitoring worker and the job workers each hold their own snapshot. Each runs a
``MetadataRefresher``: when the tree on disk changes, the change is validated and swapped in, or
rejected while last-known-good keeps serving. A failing check never stops the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from backend.metadata.models import ReloadResult
from backend.metadata.service import MetadataService

logger = logging.getLogger(__name__)


class MetadataRefresher:
    def __init__(
        self,
        service: MetadataService,
        interval_s: float,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._service = service
        self._interval_s = interval_s
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return self._interval_s > 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run_once(self) -> ReloadResult | None:
        try:
            return await self._service.refresh_if_changed()
        except Exception:
            logger.exception("metadata refresh check failed; will retry next interval")
            return None

    async def run_forever(self) -> None:
        while True:
            await self._sleep(self._interval_s)
            await self.run_once()

    def start(self) -> None:
        if not self.enabled or self.running:
            return
        self._task = asyncio.create_task(self.run_forever(), name="metadata-refresher")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
