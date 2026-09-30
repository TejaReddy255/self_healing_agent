"""SQL for the single-row ``agent_performance`` counters (S02)."""

from __future__ import annotations

from typing import get_args

from backend.db.pool import Connection
from backend.domain.audit import PerformanceCounter

_COUNTERS: tuple[str, ...] = get_args(PerformanceCounter)
# Built only from the closed PerformanceCounter allowlist, never from caller input.
_SELECT_COUNTERS = f"SELECT {', '.join(_COUNTERS)} FROM agent_performance WHERE id = 1"  # noqa: S608


class PerformanceRepository:
    async def increment(self, conn: Connection, counter: PerformanceCounter, by: int = 1) -> None:
        # Column names come from a closed allowlist, never from callers' free text.
        if counter not in _COUNTERS:
            raise ValueError(f"unknown performance counter {counter!r}")
        if by < 0:
            raise ValueError("counters only increase")
        # A single UPDATE of the form "c = c + n" is atomic under concurrency (row lock).
        await conn.execute(
            f"UPDATE agent_performance SET {counter} = {counter} + $1, updated_at = now() "  # noqa: S608
            "WHERE id = 1",
            by,
        )

    async def get(self, conn: Connection) -> dict[str, int]:
        row = await conn.fetchrow(_SELECT_COUNTERS)
        return {name: int(row[name]) for name in _COUNTERS} if row else dict.fromkeys(_COUNTERS, 0)
