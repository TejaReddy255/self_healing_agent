from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.db import migrate as cli
from backend.db.migrations import (
    MigrationError,
    MigrationState,
    checksum,
    discover,
    inspect,
    migrate_up,
    read_applied,
)
from backend.db.settings import DEFAULT_MIGRATIONS_DIR, DatabaseSettings
from tests.integration.conftest import connect

pytestmark = pytest.mark.integration


def write_migrations(directory: Path, files: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, sql in files.items():
        (directory / name).write_text(sql, encoding="utf-8")
    return directory


# --- S02-AC1 ----------------------------------------------------------------------------------


@pytest.mark.spec("S02-AC1")
async def test_up_applies_real_migrations_then_is_a_no_op(empty_db: DatabaseSettings) -> None:
    files = discover(DEFAULT_MIGRATIONS_DIR)
    conn = await connect(empty_db)
    try:
        first = await migrate_up(conn, files)
        second = await migrate_up(conn, files)
        tables = {
            r["tablename"]
            for r in await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        }
        seed = await conn.fetchval("SELECT count(*) FROM agent_performance WHERE id = 1")
        applied = await read_applied(conn)
    finally:
        await conn.close()

    assert [m.label for m in first] == [f.label for f in files]
    assert second == []
    assert {"schema_migrations", "operation_audit", "activities", "agent_performance"} <= tables
    assert seed == 1
    assert applied is not None
    assert [a.checksum for a in applied] == [f.checksum for f in files]


@pytest.mark.spec("S02-AC1")
def test_cli_status_lists_applied_and_pending(
    empty_db: DatabaseSettings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    migrations = write_migrations(
        tmp_path / "m",
        {
            "0001_first.sql": "CREATE TABLE a (id int);",
            "0002_second.sql": "CREATE TABLE b (id int);",
        },
    )
    assert cli.main(["status", "--dir", str(migrations)], settings=empty_db) == 0
    assert "schema_migrations not initialised" in capsys.readouterr().out

    assert cli.main(["up", "--dir", str(migrations)], settings=empty_db) == 0
    assert "applied  0001_first" in capsys.readouterr().out

    write_migrations(migrations, {"0003_third.sql": "CREATE TABLE c (id int);"})
    assert cli.main(["status", "--dir", str(migrations)], settings=empty_db) == 0
    out = capsys.readouterr().out
    assert "applied  0001  first" in out
    assert "applied  0002  second" in out
    assert "pending  0003  third" in out

    assert cli.main(["up", "--dir", str(migrations)], settings=empty_db) == 0
    assert cli.main(["up", "--dir", str(migrations)], settings=empty_db) == 0
    assert "nothing to apply" in capsys.readouterr().out


# --- S02-AC2 ----------------------------------------------------------------------------------


@pytest.mark.spec("S02-AC2")
def test_modified_applied_migration_blocks_up_and_verify(
    empty_db: DatabaseSettings, tmp_path: Path
) -> None:
    migrations = write_migrations(tmp_path / "m", {"0001_first.sql": "CREATE TABLE a (id int);"})

    async def scenario() -> tuple[object, object]:
        conn = await connect(empty_db)
        try:
            await migrate_up(conn, discover(migrations))
            (migrations / "0001_first.sql").write_text(
                "CREATE TABLE a (id bigint);", encoding="utf-8"
            )
            write_migrations(migrations, {"0002_second.sql": "CREATE TABLE b (id int);"})
            files = discover(migrations)
            with pytest.raises(MigrationError, match=r"checksum mismatch.*0001"):
                await migrate_up(conn, files)
            return await inspect(conn, files), await conn.fetchval(
                "SELECT to_regclass('b') IS NOT NULL"
            )
        finally:
            await conn.close()

    state, b_exists = asyncio.run(scenario())
    assert isinstance(state, MigrationState)
    assert state.drifted == [1]
    assert [m.version for m in state.pending] == [2]
    assert b_exists is False, "nothing may be applied when drift is detected"
    assert cli.main(["verify", "--dir", str(migrations)], settings=empty_db) == 1
    assert cli.main(["up", "--dir", str(migrations)], settings=empty_db) == 1


@pytest.mark.spec("S02-AC2")
async def test_missing_applied_file_is_an_error(empty_db: DatabaseSettings, tmp_path: Path) -> None:
    migrations = write_migrations(tmp_path / "m", {"0001_first.sql": "CREATE TABLE a (id int);"})
    conn = await connect(empty_db)
    try:
        await migrate_up(conn, discover(migrations))
        (migrations / "0001_first.sql").unlink()
        with pytest.raises(MigrationError, match="no file"):
            await migrate_up(conn, discover(migrations))
    finally:
        await conn.close()


def test_crlf_checkout_is_not_drift() -> None:
    assert checksum("CREATE TABLE a (id int);\nSELECT 1;\n") == checksum(
        "CREATE TABLE a (id int);\r\nSELECT 1;\r\n"
    )


@pytest.mark.parametrize("bad_name", ["1_short.sql", "0001-dash.sql", "0001_Upper.sql"])
def test_badly_named_files_are_rejected(tmp_path: Path, bad_name: str) -> None:
    write_migrations(tmp_path, {bad_name: "SELECT 1;"})
    with pytest.raises(MigrationError, match="invalid migration file name"):
        discover(tmp_path)


def test_duplicate_versions_are_rejected(tmp_path: Path) -> None:
    write_migrations(tmp_path, {"0001_a.sql": "SELECT 1;", "0001_b.sql": "SELECT 1;"})
    with pytest.raises(MigrationError, match="duplicate"):
        discover(tmp_path)


async def test_failed_migration_rolls_back_and_reports(
    empty_db: DatabaseSettings, tmp_path: Path
) -> None:
    migrations = write_migrations(
        tmp_path / "m",
        {
            "0001_ok.sql": "CREATE TABLE a (id int);",
            "0002_broken.sql": "CREATE TABLE b (id int); SELECT * FROM does_not_exist;",
        },
    )
    conn = await connect(empty_db)
    try:
        with pytest.raises(MigrationError, match="0002_broken"):
            await migrate_up(conn, discover(migrations))
        applied = await read_applied(conn)
        b_exists = await conn.fetchval("SELECT to_regclass('b') IS NOT NULL")
    finally:
        await conn.close()
    assert applied is not None and [a.version for a in applied] == [1]
    assert b_exists is False


# --- S02-AC3 ----------------------------------------------------------------------------------


@pytest.mark.spec("S02-AC3")
async def test_concurrent_runners_apply_each_migration_once(
    empty_db: DatabaseSettings, tmp_path: Path
) -> None:
    migrations = write_migrations(
        tmp_path / "m",
        {
            # The sleep keeps the first runner inside the lock while the second one arrives.
            "0001_slow.sql": "CREATE TABLE a (id int); SELECT pg_sleep(0.5);",
            "0002_next.sql": "CREATE TABLE b (id int);",
        },
    )
    files = discover(migrations)
    conns = [await connect(empty_db), await connect(empty_db)]
    try:
        results = await asyncio.gather(*(migrate_up(c, files) for c in conns))
        applied = await read_applied(conns[0])
    finally:
        for c in conns:
            await c.close()

    applied_by_runner = sorted(len(r) for r in results)
    assert applied_by_runner == [0, 2], "one runner applies everything, the other finds nothing"
    assert applied is not None and [a.version for a in applied] == [1, 2]


@pytest.mark.spec("S02-AC3")
def test_migrations_ignore_the_api_statement_timeout(
    empty_db: DatabaseSettings, tmp_path: Path
) -> None:
    # The API profile would cancel this after 100 ms; the migration profile has no limit.
    migrations = write_migrations(tmp_path / "m", {"0001_slow.sql": "SELECT pg_sleep(0.5);"})
    settings = empty_db.model_copy(update={"pg_statement_timeout_ms": 100})
    assert cli.main(["up", "--dir", str(migrations)], settings=settings) == 0


@pytest.mark.spec("S02-AC3")
async def test_waiting_runner_is_not_limited_by_the_ddl_lock_timeout(
    empty_db: DatabaseSettings, tmp_path: Path
) -> None:
    migrations = write_migrations(
        tmp_path / "m", {"0001_slow.sql": "CREATE TABLE a (id int); SELECT pg_sleep(0.6);"}
    )
    settings = empty_db.model_copy(update={"pg_migration_lock_timeout_ms": 100})
    files = discover(migrations)

    async def runner() -> int:
        import asyncpg

        conn = await asyncpg.connect(**settings.migration_connect_kwargs())
        try:
            return len(await migrate_up(conn, files))
        finally:
            await conn.close()

    # The second runner waits ~0.6 s for the advisory lock, well past the 100 ms lock_timeout.
    results = await asyncio.gather(runner(), runner())
    assert sorted(results) == [0, 1]


@pytest.mark.spec("S02-AC1")
def test_out_of_order_migration_is_rejected(
    empty_db: DatabaseSettings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    migrations = write_migrations(
        tmp_path / "m",
        {"0001_a.sql": "CREATE TABLE a (id int);", "0003_c.sql": "CREATE TABLE c (id int);"},
    )
    assert cli.main(["up", "--dir", str(migrations)], settings=empty_db) == 0
    write_migrations(migrations, {"0002_b.sql": "CREATE TABLE b (id int);"})
    capsys.readouterr()
    assert cli.main(["up", "--dir", str(migrations)], settings=empty_db) == 1
    assert "numbered below the highest applied" in capsys.readouterr().err
    assert cli.main(["status", "--dir", str(migrations)], settings=empty_db) == 1
    assert "OUT-OF-ORDER  0002  b" in capsys.readouterr().out
