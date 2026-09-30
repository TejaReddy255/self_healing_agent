"""Pure (no database) tests for migration discovery and state comparison."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.db.migrations import AppliedMigration, checksum, compare, discover
from backend.db.settings import DatabaseSettings


@pytest.mark.spec("S02-AC2")
def test_bom_and_crlf_do_not_change_the_checksum(tmp_path: Path) -> None:
    body = "CREATE TABLE a (id int);\nSELECT 1;\n"
    (tmp_path / "plain").mkdir()
    (tmp_path / "windows").mkdir()
    (tmp_path / "plain" / "0001_a.sql").write_bytes(body.encode("utf-8"))
    (tmp_path / "windows" / "0001_a.sql").write_bytes(
        b"\xef\xbb\xbf" + body.replace("\n", "\r\n").encode("utf-8")
    )
    plain, windows = discover(tmp_path / "plain")[0], discover(tmp_path / "windows")[0]
    assert plain.checksum == windows.checksum == checksum(body)
    assert not windows.sql.startswith(chr(0xFEFF)), "a BOM must never be sent to PostgreSQL"


@pytest.mark.spec("S02-AC1")
def test_pending_below_highest_applied_is_out_of_order(tmp_path: Path) -> None:
    for name in ("0001_a.sql", "0002_b.sql", "0003_c.sql"):
        (tmp_path / name).write_text("SELECT 1;", encoding="utf-8")
    files = discover(tmp_path)
    applied = [
        AppliedMigration(f.version, f.name, f.checksum, datetime.now(UTC))
        for f in files
        if f.version in (1, 3)
    ]
    state = compare(files, applied)
    assert [m.version for m in state.pending] == [2]
    assert state.out_of_order == [2]
    assert not state.consistent


def test_uninitialised_database_has_everything_pending(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    state = compare(discover(tmp_path), None)
    assert not state.initialised
    assert [m.version for m in state.pending] == [1]
    assert not state.up_to_date


def test_migration_connection_profile_lifts_statement_timeout() -> None:
    settings = DatabaseSettings(
        _env_file=None, pg_statement_timeout_ms=30_000, pg_migration_lock_timeout_ms=5_000
    )
    api = settings.connect_kwargs()
    migrate = settings.migration_connect_kwargs()
    assert api["server_settings"]["statement_timeout"] == "30000"
    assert migrate["server_settings"]["statement_timeout"] == "0"
    assert migrate["server_settings"]["lock_timeout"] == "5000"
    assert migrate["command_timeout"] is None
