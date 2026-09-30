"""Readiness check for schema migrations (S02): ready only when every migration file is applied
and no applied file has drifted."""

from __future__ import annotations

import asyncpg

from backend.db.migrations import MigrationFile, inspect
from backend.db.pool import Database, DatabaseUnavailableError
from backend.observability.readiness import ComponentStatus, ReadinessCheck


def migrations_check(database: Database, files: list[MigrationFile]) -> ReadinessCheck:
    async def check() -> ComponentStatus:
        try:
            async with database.connection() as conn:
                state = await inspect(conn, files)
        except (OSError, asyncpg.PostgresError, DatabaseUnavailableError, TimeoutError) as exc:
            return ComponentStatus(status="down", detail=f"cannot inspect: {type(exc).__name__}")
        if not state.initialised:
            return ComponentStatus(
                status="down", detail="not initialised: run python -m backend.db.migrate up"
            )
        if not state.consistent:
            return ComponentStatus(
                status="down",
                detail=f"migration drift (versions {state.drifted or state.missing_files}); "
                "run python -m backend.db.migrate status",
            )
        if state.pending:
            return ComponentStatus(
                status="down", detail=f"{len(state.pending)} pending migration(s)"
            )
        return ComponentStatus(status="ok", detail=f"{len(state.applied)} applied")

    return check
