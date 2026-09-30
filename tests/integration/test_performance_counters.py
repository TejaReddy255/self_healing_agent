from __future__ import annotations

import asyncio

import pytest

from backend.db.pool import Database
from backend.db.settings import DatabaseSettings
from backend.repositories.performance_repository import PerformanceRepository

pytestmark = pytest.mark.integration


@pytest.mark.spec("S02-AC8")
async def test_concurrent_increments_are_atomic(migrated_db: DatabaseSettings) -> None:
    database = Database(migrated_db.model_copy(update={"pg_pool_max": 10}))
    repo = PerformanceRepository()
    await database.open()

    async def bump() -> None:
        async with database.transaction() as conn:
            await repo.increment(conn, "tool_executions")

    try:
        await asyncio.gather(*(bump() for _ in range(50)))
        async with database.connection() as conn:
            counters = await repo.get(conn)
    finally:
        await database.close()

    assert counters == {
        "completed_analyses": 0,
        "tool_executions": 50,
        "issues_detected": 0,
        "total_activities": 0,
    }


async def test_unknown_or_negative_increment_rejected(migrated_db: DatabaseSettings) -> None:
    database = Database(migrated_db)
    repo = PerformanceRepository()
    await database.open()
    try:
        async with database.connection() as conn:
            with pytest.raises(ValueError, match="unknown"):
                await repo.increment(conn, "tool_executions; DROP TABLE x")  # type: ignore[arg-type]
            with pytest.raises(ValueError, match="only increase"):
                await repo.increment(conn, "tool_executions", by=-1)
    finally:
        await database.close()
