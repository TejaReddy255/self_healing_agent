"""PostgreSQL MCP tools against the seeded warehouse container (S07)."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from pydantic import SecretStr

from mcp_servers.common.errors import ToolFailed, ToolRejected
from mcp_servers.common.settings import PostgresServerSettings
from mcp_servers.postgres.tools import PostgresTools
from tests.integration.mcp_servers.conftest import SCRATCH_ROWS, secrets_of

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


def load(text: str) -> Any:
    return json.loads(text)


async def scratch_count(tools: PostgresTools) -> int:
    return int(load(await tools.get_row_count("dw.mcp_scratch"))["row_count"])


# --- AC4 canonical data ---------------------------------------------------------------------------


@pytest.mark.spec("S07-AC4")
async def test_canonical_duplicates_and_counts(pg_tools: PostgresTools) -> None:
    dupes = load(await pg_tools.find_duplicates("dw.customer", ["customer_id"]))
    assert dupes["duplicate_keys"] == 417
    assert dupes["excess_rows"] == 417
    assert dupes["duplicate_rows"] == 834
    assert len(dupes["sample"]) == 5 and all(s["count"] == 2 for s in dupes["sample"])
    assert load(await pg_tools.get_row_count("dw.customer"))["row_count"] == 100_417


@pytest.mark.spec("S07-AC4")
async def test_catalog_tools(pg_tools: PostgresTools) -> None:
    assert load(await pg_tools.list_schemas()) == {"schemas": ["dw"]}
    tables = {t["name"] for t in load(await pg_tools.list_tables("dw"))["tables"]}
    assert {"customer", "sales_daily", "sales_daily_2026_08", "sales_daily_2026_09"} <= tables
    parts = load(await pg_tools.list_partitions("dw.sales_daily"))["partitions"]
    assert [p["name"] for p in parts] == ["sales_daily_2026_08", "sales_daily_2026_09"]
    assert "2026-08-01" in parts[0]["bound"]


# --- AC2 reader role, read-only transaction ----------------------------------------------------


@pytest.mark.spec("S07-AC2")
async def test_reads_run_as_reader_in_read_only_transaction(pg_tools: PostgresTools) -> None:
    async def probe(conn: Any) -> tuple[str, str]:
        return await conn.fetchval("SELECT current_user"), await conn.fetchval(
            "SHOW transaction_read_only"
        )

    assert await pg_tools._read(probe) == ("mcp_reader", "on")


@pytest.mark.spec("S07-AC2")
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT nextval('dw.customer_load_id_seq')",
        "SELECT setval('dw.customer_load_id_seq', 1)",
        "SELECT lo_create(0)",
    ],
)
async def test_writes_smuggled_into_a_select_fail_in_the_database(
    pg_tools: PostgresTools, sql: str
) -> None:
    # The validator refuses these (function allowlist); the database refuses them on its own too.
    with pytest.raises(ToolRejected, match="function not allowed"):
        await pg_tools.run_select(sql)

    async def raw(conn: Any) -> None:
        await conn.fetchval(sql)

    with pytest.raises(ToolFailed, match="database error"):
        await pg_tools._read(raw)
    assert load(await pg_tools.get_row_count("dw.customer"))["row_count"] == 100_417


@pytest.mark.spec("S07-AC2", "S07-AC8")
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT table_to_xml('dw.customer', true, false, '')",
        "SELECT query_to_xml('select email from dw.customer', true, false, '')",
        "SELECT * FROM ts_stat('select to_tsvector(email) from dw.customer')",
    ],
)
async def test_sql_in_a_string_functions_are_revoked_from_the_reader(
    pg_tools: PostgresTools, sql: str
) -> None:
    # deploy/init/warehouse/02-revoke-functions.sh: even past the validator, no EXECUTE right.
    async def raw(conn: Any) -> None:
        await conn.fetch(sql)

    with pytest.raises(ToolFailed, match="42501"):  # insufficient_privilege
        await pg_tools._read(raw)


@pytest.mark.spec("S07-AC2")
async def test_reader_cannot_write_even_without_validation(
    pg_tools: PostgresTools, pg_scratch: str
) -> None:
    async def write(conn: Any) -> None:
        await conn.execute("DELETE FROM dw.mcp_scratch WHERE id = 1")

    with pytest.raises(ToolFailed, match="25006"):  # read_only_sql_transaction
        await pg_tools._read(write)
    assert await scratch_count(pg_tools) == SCRATCH_ROWS


# --- AC3 DML with rollback ceiling ----------------------------------------------------------------


@pytest.mark.spec("S07-AC3")
async def test_dml_over_ceiling_rolls_back(pg_tools: PostgresTools, pg_scratch: str) -> None:
    result = load(await pg_tools.execute_dml("DELETE FROM dw.mcp_scratch WHERE id > 10", 5))
    assert result == {"status": "aborted", "affected_rows": 40, "max_affected_rows": 5}
    assert await scratch_count(pg_tools) == SCRATCH_ROWS


@pytest.mark.spec("S07-AC3")
async def test_dml_within_ceiling_commits(pg_tools: PostgresTools, pg_scratch: str) -> None:
    result = load(await pg_tools.execute_dml("DELETE FROM dw.mcp_scratch WHERE id <= 3", 3))
    assert result == {"status": "committed", "affected_rows": 3}
    assert await scratch_count(pg_tools) == SCRATCH_ROWS - 3
    update = "UPDATE dw.mcp_scratch SET note = 'x' WHERE id = 10"
    assert load(await pg_tools.execute_dml(update, 1))["affected_rows"] == 1


@pytest.mark.spec("S07-AC3")
@pytest.mark.parametrize(
    ("sql", "limit"),
    [
        ("DELETE FROM dw.mcp_scratch", 100),
        ("DELETE FROM dw.mcp_scratch WHERE 1 = 1", 100),
        ("UPDATE dw.mcp_scratch SET note = 'x' WHERE true", 100),
        ("DELETE FROM dw.mcp_scratch WHERE id = 1", 0),
        ("DELETE FROM dw.mcp_scratch WHERE id = 1", 10_001),
    ],
)
async def test_dml_rejections_change_nothing(
    pg_tools: PostgresTools, pg_scratch: str, sql: str, limit: int
) -> None:
    with pytest.raises(ToolRejected):
        await pg_tools.execute_dml(sql, limit)
    assert await scratch_count(pg_tools) == SCRATCH_ROWS


@pytest.mark.spec("S07-AC3", "S07-AC7")
async def test_writer_has_no_owner_rights_outside_execute_ddl(
    pg_tools: PostgresTools, pg_scratch: str
) -> None:
    # mcp_writer is NOINHERIT: DML grants yes; dw_owner rights only via SET ROLE in execute_ddl.
    async def drop(conn: Any) -> None:
        await conn.execute("DROP TABLE dw.mcp_scratch")

    with pytest.raises(ToolFailed, match="42501"):  # insufficient_privilege
        await pg_tools._run("writer", drop, readonly=False)
    assert await scratch_count(pg_tools) == SCRATCH_ROWS


# --- AC5 limits and timeouts ----------------------------------------------------------------------


@pytest.mark.spec("S07-AC5")
async def test_row_limit_and_truncation(pg_tools: PostgresTools) -> None:
    result = load(await pg_tools.run_select("SELECT customer_id FROM dw.customer ORDER BY 1"))
    assert result["row_count"] == 20 and result["truncated"] is True
    assert result["rows"][0] == [1]
    small = load(await pg_tools.run_select("SELECT customer_id FROM dw.customer LIMIT 3"))
    assert small["truncated"] is False and small["row_count"] == 3


@pytest.mark.spec("S07-AC5")
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_sleep(2)",
        "SELECT count(*) FROM dw.customer a CROSS JOIN dw.customer b",
    ],
)
async def test_statement_timeout(pg_settings: PostgresServerSettings, sql: str) -> None:
    tools = PostgresTools(pg_settings.model_copy(update={"mcp_statement_timeout_ms": 300}))
    try:
        with pytest.raises(ToolFailed, match="timed out"):
            await tools.run_select(sql)
    finally:
        await tools.close()


# --- AC7 DDL --------------------------------------------------------------------------------------


@pytest.mark.spec("S07-AC7")
async def test_allowlisted_ddl_creates_partition_and_index(
    pg_tools: PostgresTools, pg_scratch: str
) -> None:
    ddl = (
        "CREATE TABLE dw.sales_daily_2026_10 PARTITION OF dw.sales_daily"
        " FOR VALUES FROM ('2026-10-01') TO ('2026-11-01')"
    )
    assert load(await pg_tools.execute_ddl(ddl)) == {"status": "committed"}
    parts = load(await pg_tools.list_partitions("dw.sales_daily"))["partitions"]
    assert "sales_daily_2026_10" in {p["name"] for p in parts}
    await pg_tools.execute_ddl("CREATE INDEX ix_mcp_scratch_note ON dw.mcp_scratch (note)")
    # The writer can use the new partition through its default privileges.
    insert = "INSERT INTO dw.sales_daily VALUES ('2026-10-02', 1, 10.00)"
    assert load(await pg_tools.execute_dml(insert, 1))["status"] == "committed"


@pytest.mark.spec("S07-AC7")
@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE dw.mcp_scratch",
        "TRUNCATE dw.mcp_scratch",
        "ALTER TABLE dw.mcp_scratch DROP COLUMN note",
        "CREATE TABLE dw.copy AS SELECT * FROM dw.mcp_scratch",
    ],
)
async def test_ddl_outside_allowlist_changes_nothing(
    pg_tools: PostgresTools, pg_scratch: str, sql: str
) -> None:
    with pytest.raises(ToolRejected):
        await pg_tools.execute_ddl(sql)
    assert await scratch_count(pg_tools) == SCRATCH_ROWS


# --- AC8 PII --------------------------------------------------------------------------------------


@pytest.mark.spec("S07-AC8")
async def test_pii_never_leaves_the_server(pg_tools: PostgresTools) -> None:
    columns = load(await pg_tools.describe_table("dw.customer"))["columns"]
    assert {c["name"] for c in columns if c["pii"]} == {"email", "phone"}
    with pytest.raises(ToolRejected, match="PII"):
        await pg_tools.run_select("SELECT email FROM dw.customer")
    masked = load(await pg_tools.find_duplicates("dw.customer", ["email"]))
    assert masked["duplicate_keys"] == 417
    assert {s["key"]["email"] for s in masked["sample"]} == {"***"}
    outputs = [
        await pg_tools.run_select("SELECT customer_id, name, country FROM dw.customer"),
        json.dumps(masked),
        await pg_tools.find_duplicates("dw.customer", ["customer_id", "phone"]),
    ]
    for text in outputs:
        assert "@example.test" not in text and "+1-555-" not in text


# --- AC9 identifiers ------------------------------------------------------------------------------


@pytest.mark.spec("S07-AC9")
@pytest.mark.parametrize(
    ("call", "args"),
    [
        ("get_row_count", ("dw.missing_table",)),
        ("get_row_count", ("dw.customer; DROP TABLE dw.customer",)),
        ("describe_table", ('dw."customer"--',)),
        ("list_tables", ("public",)),
        ("list_partitions", ("pg_catalog.pg_class",)),
        ("find_duplicates", ("dw.customer", ["no_such_column"])),
        ("find_duplicates", ("dw.customer", ["customer_id) FROM dw.customer --"])),
        ("find_duplicates", ("dw.customer", ["`customer_id`"])),
    ],
)
async def test_identifiers_must_be_valid_and_exist(
    pg_tools: PostgresTools, call: str, args: tuple[Any, ...]
) -> None:
    with pytest.raises(ToolRejected):
        await getattr(pg_tools, call)(*args)
    assert load(await pg_tools.get_row_count("dw.customer"))["row_count"] == 100_417


# --- AC10 secrets ---------------------------------------------------------------------------------


@pytest.mark.spec("S07-AC10")
async def test_no_secrets_in_errors_or_logs(
    pg_settings: PostgresServerSettings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    reader = pg_settings.mcp_postgres_reader_dsn
    assert reader is not None
    wrong = reader.get_secret_value().replace("@", "x@", 1)  # same user, wrong password
    bad = PostgresTools(
        pg_settings.model_copy(update={"mcp_postgres_reader_dsn": SecretStr(wrong)})
    )
    messages: list[str] = []
    try:
        for coro in (bad.list_schemas(), bad.run_select("SELECT 1")):
            with pytest.raises(ToolFailed) as info:
                await coro
            messages.append(str(info.value))
        assert await bad.ping() is False
    finally:
        await bad.close()
    good = PostgresTools(pg_settings)
    try:
        with pytest.raises(ToolFailed) as info:
            await good.run_select("SELECT 1 / 0")
        messages.append(str(info.value))
    finally:
        await good.close()
    assert messages[0].startswith("database error: InvalidPasswordError")
    haystack = "\n".join(messages + [r.getMessage() for r in caplog.records] + [caplog.text])
    for secret in [*secrets_of(pg_settings), wrong]:
        assert secret not in haystack


# --- AC11 reader only -----------------------------------------------------------------------------


@pytest.mark.spec("S07-AC11")
async def test_without_writer_dsn_writes_are_impossible(
    pg_settings: PostgresServerSettings,
) -> None:
    tools = PostgresTools(pg_settings.model_copy(update={"mcp_postgres_writer_dsn": None}))
    try:
        assert tools.has_writer is False
        with pytest.raises(ToolFailed, match="no writer credentials"):
            await tools.execute_dml("DELETE FROM dw.customer WHERE customer_id = 1", 1)
    finally:
        await tools.close()
