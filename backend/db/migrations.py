"""Numbered SQL migrations (ADR-003): discovery, checksums, state comparison and the runner.

Files are ``migrations/NNNN_snake_name.sql``. Checksums are SHA-256 over the file with line
endings normalised to LF, so a Windows checkout with CRLF does not look like drift. Applied files
must never change; any drift or missing file stops ``up`` before anything is applied.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import asyncpg

from backend.db.pool import Connection

logger = logging.getLogger(__name__)

MIGRATION_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
# Arbitrary constant key for pg_advisory_lock; serialises concurrent runners on one database.
ADVISORY_LOCK_KEY = 0x5E1F_4EA1_0000_0001

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT        NOT NULL,
    checksum   TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    """Migration files or database state are inconsistent; nothing was applied."""


@dataclass(frozen=True)
class MigrationFile:
    version: int
    name: str
    path: Path
    checksum: str
    sql: str

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str
    applied_at: datetime


@dataclass
class MigrationState:
    initialised: bool
    applied: list[AppliedMigration] = field(default_factory=list)
    pending: list[MigrationFile] = field(default_factory=list)
    drifted: list[int] = field(default_factory=list)
    missing_files: list[int] = field(default_factory=list)
    # Pending versions lower than the highest applied one (e.g. after a branch merge). Numbers
    # are assigned at merge time (sdd-workflow §7), so this indicates a numbering mistake.
    out_of_order: list[int] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return not self.drifted and not self.missing_files and not self.out_of_order

    @property
    def up_to_date(self) -> bool:
        return self.initialised and self.consistent and not self.pending


def checksum(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def discover(directory: Path) -> list[MigrationFile]:
    """Return migration files sorted by version, rejecting badly named or duplicate versions."""
    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")
    files: dict[int, MigrationFile] = {}
    for path in sorted(directory.glob("*.sql")):
        match = MIGRATION_FILE.fullmatch(path.name)
        if match is None:
            raise MigrationError(
                f"invalid migration file name {path.name!r}; expected NNNN_snake_case.sql"
            )
        version = int(match.group(1))
        if version in files:
            raise MigrationError(f"duplicate migration version {version:04d}: {path.name}")
        # utf-8-sig drops a BOM that Windows editors may add; it must not reach the checksum or SQL.
        text = path.read_text(encoding="utf-8-sig")
        files[version] = MigrationFile(version, match.group(2), path, checksum(text), text)
    return [files[v] for v in sorted(files)]


async def read_applied(conn: Connection) -> list[AppliedMigration] | None:
    """Applied migrations, or ``None`` when ``schema_migrations`` does not exist yet."""
    exists = await conn.fetchval("SELECT to_regclass('schema_migrations') IS NOT NULL")
    if not exists:
        return None
    rows = await conn.fetch(
        "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
    )
    return [AppliedMigration(r["version"], r["name"], r["checksum"], r["applied_at"]) for r in rows]


def compare(files: list[MigrationFile], applied: list[AppliedMigration] | None) -> MigrationState:
    if applied is None:
        return MigrationState(initialised=False, pending=list(files))
    by_version = {f.version: f for f in files}
    applied_versions = {a.version for a in applied}
    pending = [f for f in files if f.version not in applied_versions]
    highest_applied = max(applied_versions, default=0)
    return MigrationState(
        initialised=True,
        applied=list(applied),
        pending=pending,
        out_of_order=[f.version for f in pending if f.version < highest_applied],
        drifted=[
            a.version
            for a in applied
            if a.version in by_version and by_version[a.version].checksum != a.checksum
        ],
        missing_files=[a.version for a in applied if a.version not in by_version],
    )


async def inspect(conn: Connection, files: list[MigrationFile]) -> MigrationState:
    return compare(files, await read_applied(conn))


def _raise_if_inconsistent(state: MigrationState) -> None:
    problems = []
    if state.drifted:
        versions = ", ".join(f"{v:04d}" for v in state.drifted)
        problems.append(
            f"checksum mismatch for applied migration(s) {versions}: applied files must never be "
            "edited; add a new migration instead"
        )
    if state.missing_files:
        versions = ", ".join(f"{v:04d}" for v in state.missing_files)
        problems.append(f"applied migration(s) {versions} have no file in the migrations directory")
    if state.out_of_order:
        versions = ", ".join(f"{v:04d}" for v in state.out_of_order)
        problems.append(
            f"pending migration(s) {versions} are numbered below the highest applied version; "
            "renumber them after the latest migration"
        )
    if problems:
        raise MigrationError("; ".join(problems))


async def migrate_up(conn: Connection, files: list[MigrationFile]) -> list[MigrationFile]:
    """Apply pending migrations in order, each in its own transaction, under an advisory lock.

    ``conn`` must be a dedicated connection (the session-level lock is held on it). Returns the
    migrations applied by this call; concurrent runners wait for the lock and then find nothing
    pending. The wait for another runner is unbounded (``lock_timeout`` is lifted for it); the
    session's own ``lock_timeout`` then applies again to the migrations' DDL.
    """
    ddl_lock_timeout = await conn.fetchval("SELECT current_setting('lock_timeout')")
    await conn.execute("SELECT set_config('lock_timeout', '0', false)")
    await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
    try:
        await conn.execute("SELECT set_config('lock_timeout', $1, false)", ddl_lock_timeout)
        await conn.execute(SCHEMA_MIGRATIONS_DDL)
        state = await inspect(conn, files)
        _raise_if_inconsistent(state)
        applied: list[MigrationFile] = []
        for migration in state.pending:
            try:
                async with conn.transaction():
                    await conn.execute(migration.sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, name, checksum) "
                        "VALUES ($1, $2, $3)",
                        migration.version,
                        migration.name,
                        migration.checksum,
                    )
            except asyncpg.PostgresError as exc:
                raise MigrationError(f"migration {migration.label} failed: {exc}") from exc
            logger.info("migration applied", extra={"migration": migration.label})
            applied.append(migration)
        return applied
    finally:
        # Never let an unlock failure (e.g. the connection dropped) mask the real error; the
        # server releases session-level advisory locks when the connection closes anyway.
        try:
            await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
            logger.warning("could not release the migration advisory lock explicitly")


async def verify(conn: Connection, files: list[MigrationFile]) -> MigrationState:
    """Raise ``MigrationError`` on drift or missing files; pending migrations are allowed."""
    state = await inspect(conn, files)
    _raise_if_inconsistent(state)
    return state
