"""Migration CLI (S02, ADR-003).

Usage::

    python -m backend.db.migrate up|status|verify [--dir migrations]

Connection settings come from ``PG_*`` environment variables / ``.env`` (never the command line).
Exit codes: 0 success, 1 migration error or drift, 2 configuration or connection error.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg

from backend.config import ConfigurationError
from backend.db.migrations import (
    MigrationError,
    MigrationFile,
    MigrationState,
    discover,
    inspect,
    migrate_up,
    verify,
)
from backend.db.settings import DatabaseSettings, load_database_settings


def _render_state(state: MigrationState) -> list[str]:
    lines = [] if state.initialised else ["schema_migrations not initialised"]
    for a in state.applied:
        marker = "DRIFT  " if a.version in state.drifted else "applied"
        suffix = " (file missing)" if a.version in state.missing_files else ""
        lines.append(f"{marker}  {a.version:04d}  {a.name}  {a.applied_at.isoformat()}{suffix}")
    for m in state.pending:
        marker = "OUT-OF-ORDER" if m.version in state.out_of_order else "pending"
        lines.append(f"{marker}  {m.version:04d}  {m.name}")
    return lines


async def run(command: str, settings: DatabaseSettings, files: list[MigrationFile]) -> int:
    conn = await asyncpg.connect(**settings.migration_connect_kwargs())
    try:
        if command == "up":
            applied = await migrate_up(conn, files)
            if applied:
                for m in applied:
                    print(f"applied  {m.label}")
            else:
                print("nothing to apply: database is up to date")
            return 0
        if command == "verify":
            state = await verify(conn, files)
            print(f"ok: {len(state.applied)} applied, {len(state.pending)} pending, no drift")
            return 0
        state = await inspect(conn, files)
        print("\n".join(_render_state(state)) or "no migrations")
        return 0 if state.consistent else 1
    finally:
        await conn.close()


def main(argv: list[str] | None = None, settings: DatabaseSettings | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.db.migrate", description=__doc__)
    parser.add_argument("command", choices=["up", "status", "verify"])
    parser.add_argument("--dir", type=Path, default=None, help="migrations directory")
    args = parser.parse_args(argv)

    try:
        settings = settings or load_database_settings()
        if not settings.configured:
            raise ConfigurationError("PG_PASSWORD is not set; cannot connect to the database")
        files = discover(args.dir or settings.migrations_dir)
    except (ConfigurationError, MigrationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(args.command, settings, files))
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        print(
            f"error: cannot use database {settings.describe()}: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
