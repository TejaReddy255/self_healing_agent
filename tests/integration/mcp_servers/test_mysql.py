"""MySQL MCP tools against the seeded sales container (S07)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import pytest
from pydantic import SecretStr

from mcp_servers.common.errors import ToolFailed, ToolRejected
from mcp_servers.common.settings import MySQLServerSettings
from mcp_servers.mysql.tools import MySQLTools
from tests.integration.mcp_servers.conftest import SCRATCH_ROWS, my_admin, secrets_of

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

HEAVY = (
    "SELECT sum(a.customer_id * b.customer_id) FROM sales.customer a CROSS JOIN sales.customer b"
)


def load(text: str) -> Any:
    return json.loads(text)


async def scratch_count(tools: MySQLTools) -> int:
    return int(load(await tools.get_row_count("sales.mcp_scratch"))["row_count"])


@pytest.mark.spec("S07-AC4")
async def test_canonical_source_count_and_no_duplicates(my_tools: MySQLTools) -> None:
    assert load(await my_tools.get_row_count("sales.customer"))["row_count"] == 100_000
    dupes = load(await my_tools.find_duplicates("sales.customer", ["customer_id"]))
    assert dupes["duplicate_keys"] == 0 and dupes["sample"] == []
    assert load(await my_tools.list_schemas()) == {"schemas": ["sales"]}
    tables = {t["name"] for t in load(await my_tools.list_tables("sales"))["tables"]}
    assert "customer" in tables
    described = load(await my_tools.describe_table("sales.customer"))
    assert described["primary_key"] == ["customer_id"]


# --- AC2 reader user, read-only transaction ----------------------------------------------------


@pytest.mark.spec("S07-AC2")
async def test_reads_run_as_reader_and_cannot_write(my_tools: MySQLTools, my_scratch: str) -> None:
    async def who(cur: Any) -> str:
        await cur.execute("SELECT CURRENT_USER()")
        return str((await cur.fetchone())[0])

    assert (await my_tools._read(who)).startswith("mcp_reader@")

    async def write(cur: Any) -> None:
        await cur.execute("DELETE FROM sales.mcp_scratch WHERE id = 1")

    with pytest.raises(ToolFailed, match="database error"):
        await my_tools._read(write)
    assert await scratch_count(my_tools) == SCRATCH_ROWS


@pytest.mark.spec("S07-AC1", "S07-AC2")
async def test_server_refuses_a_second_statement_on_the_wire(my_tools: MySQLTools) -> None:
    # Validation stops this first; the connection itself also refuses multi-statements.
    async def multi(cur: Any) -> None:
        await cur.execute("SELECT 1; SELECT 2")

    with pytest.raises(ToolFailed, match="42000"):  # syntax error: the second statement
        await my_tools._read(multi)
    assert load(await my_tools.run_select("SELECT 1"))["rows"] == [[1]]


# --- AC3 DML with rollback ceiling ----------------------------------------------------------------


@pytest.mark.spec("S07-AC3")
async def test_dml_over_ceiling_rolls_back(my_tools: MySQLTools, my_scratch: str) -> None:
    result = load(await my_tools.execute_dml("DELETE FROM sales.mcp_scratch WHERE id > 10", 5))
    assert result == {"status": "aborted", "affected_rows": 40, "max_affected_rows": 5}
    assert await scratch_count(my_tools) == SCRATCH_ROWS


@pytest.mark.spec("S07-AC3")
async def test_dml_within_ceiling_commits(my_tools: MySQLTools, my_scratch: str) -> None:
    result = load(await my_tools.execute_dml("DELETE FROM sales.mcp_scratch WHERE id <= 3", 3))
    assert result == {"status": "committed", "affected_rows": 3}
    assert await scratch_count(my_tools) == SCRATCH_ROWS - 3


@pytest.mark.spec("S07-AC3")
async def test_unchanged_matched_rows_still_count_against_the_ceiling(
    my_tools: MySQLTools, my_scratch: str
) -> None:
    # 'n11'..'n50' -> same value: MySQL changes 0 rows but matches 40; the ceiling uses matches.
    result = load(
        await my_tools.execute_dml("UPDATE sales.mcp_scratch SET note = note WHERE id > 10", 5)
    )
    assert result["status"] == "aborted" and result["affected_rows"] == 40


@pytest.mark.spec("S07-AC3")
@pytest.mark.parametrize(
    ("sql", "limit"),
    [
        ("DELETE FROM sales.mcp_scratch", 100),
        ("DELETE FROM sales.mcp_scratch WHERE 1 = 1", 100),
        ("UPDATE sales.mcp_scratch SET note = 'x' WHERE TRUE", 100),
        ("DELETE FROM sales.mcp_scratch WHERE id = 1", 0),
        ("DELETE FROM sales.mcp_scratch WHERE id = 1", 10_001),
    ],
)
async def test_dml_rejections_change_nothing(
    my_tools: MySQLTools, my_scratch: str, sql: str, limit: int
) -> None:
    with pytest.raises(ToolRejected):
        await my_tools.execute_dml(sql, limit)
    assert await scratch_count(my_tools) == SCRATCH_ROWS


# --- AC5 limits and timeouts ----------------------------------------------------------------------


@pytest.mark.spec("S07-AC5")
async def test_row_limit_and_truncation(my_tools: MySQLTools) -> None:
    result = load(await my_tools.run_select("SELECT customer_id FROM sales.customer ORDER BY 1"))
    assert result["row_count"] == 20 and result["truncated"] is True
    assert result["rows"][0] == [1]


@pytest.mark.spec("S07-AC5")
async def test_statement_timeout_and_recovery(my_settings: MySQLServerSettings) -> None:
    tools = MySQLTools(my_settings.model_copy(update={"mcp_statement_timeout_ms": 300}))
    try:
        with pytest.raises(ToolFailed, match="timed out"):
            await tools.run_select(HEAVY)
        # MySQL ends an over-long SLEEP() early, returning 1 instead of raising an error.
        assert load(await tools.run_select("SELECT SLEEP(5)"))["rows"] == [[1]]
        # The timed-out connection was discarded; the next call gets a clean one.
        assert load(await tools.get_row_count("sales.customer"))["row_count"] == 100_000
    finally:
        await tools.close()


# --- AC7 no DDL on MySQL --------------------------------------------------------------------------


@pytest.mark.spec("S07-AC7")
async def test_mysql_has_no_ddl_or_partitions(my_tools: MySQLTools) -> None:
    assert my_tools.supports_ddl is False and my_tools.supports_partitions is False
    with pytest.raises(ToolRejected):
        await my_tools.execute_ddl("CREATE INDEX ix ON sales.customer (country)")


# --- AC8 PII --------------------------------------------------------------------------------------


@pytest.mark.spec("S07-AC8")
async def test_pii_never_leaves_the_server(my_tools: MySQLTools) -> None:
    columns = load(await my_tools.describe_table("sales.customer"))["columns"]
    assert {c["name"] for c in columns if c["pii"]} == {"email", "phone"}
    for sql in ("SELECT email FROM sales.customer", "SELECT * FROM sales.customer"):
        with pytest.raises(ToolRejected, match="PII"):
            await my_tools.run_select(sql)
    outputs = [
        await my_tools.run_select("SELECT customer_id, name, country FROM sales.customer"),
        await my_tools.find_duplicates("sales.customer", ["country", "phone"]),
        await my_tools.describe_table("sales.customer"),
    ]
    for text in outputs:
        assert "@example.test" not in text and "+1-555-" not in text


# --- AC9 identifiers ------------------------------------------------------------------------------


@pytest.mark.spec("S07-AC9")
@pytest.mark.parametrize(
    ("call", "args"),
    [
        ("get_row_count", ("sales.missing_table",)),
        ("get_row_count", ("sales.customer; DROP TABLE sales.customer",)),
        ("describe_table", ("sales.`customer`",)),
        ("list_tables", ("mysql",)),
        ("describe_table", ("mysql.user",)),
        ("find_duplicates", ("sales.customer", ["no_such_column"])),
        ("find_duplicates", ("sales.customer", ["customer_id` FROM sales.customer --"])),
    ],
)
async def test_identifiers_must_be_valid_and_exist(
    my_tools: MySQLTools, call: str, args: tuple[Any, ...]
) -> None:
    with pytest.raises(ToolRejected):
        await getattr(my_tools, call)(*args)
    assert load(await my_tools.get_row_count("sales.customer"))["row_count"] == 100_000


# --- AC10 secrets ---------------------------------------------------------------------------------


@pytest.mark.spec("S07-AC10")
async def test_no_secrets_in_errors_or_logs(
    my_settings: MySQLServerSettings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    reader = my_settings.mcp_mysql_reader_dsn
    assert reader is not None
    wrong = reader.get_secret_value().replace("@", "x@", 1)
    bad = MySQLTools(my_settings.model_copy(update={"mcp_mysql_reader_dsn": SecretStr(wrong)}))
    messages: list[str] = []
    try:
        with pytest.raises(ToolFailed) as info:
            await bad.run_select("SELECT 1")
        messages.append(str(info.value))
        assert await bad.ping() is False
    finally:
        await bad.close()
    good = MySQLTools(my_settings)
    try:
        with pytest.raises(ToolFailed) as info:
            await good.run_select("SELECT no_such_col FROM sales.customer")
        messages.append(str(info.value))
    finally:
        await good.close()
    assert messages == [
        "database error: OperationalError (28000)",  # access denied
        "database error: OperationalError (42S22)",  # unknown column
    ]
    haystack = "\n".join([*messages, caplog.text])
    for secret in [*secrets_of(my_settings), wrong]:
        assert secret not in haystack


# --- AC11 reader only -----------------------------------------------------------------------------


@pytest.mark.spec("S07-AC11")
async def test_without_writer_dsn_writes_are_impossible(my_settings: MySQLServerSettings) -> None:
    tools = MySQLTools(my_settings.model_copy(update={"mcp_mysql_writer_dsn": None}))
    try:
        assert tools.has_writer is False
        with pytest.raises(ToolFailed, match="no writer credentials"):
            await tools.execute_dml("DELETE FROM sales.customer WHERE customer_id = 1", 1)
    finally:
        await tools.close()


# --- review fixes (2026-10-01) ------------------------------------------------------------------


@pytest.mark.spec("S07-AC5")
async def test_row_cap_is_applied_by_the_server(my_tools: MySQLTools) -> None:
    # 10^10 rows: without LIMIT in the query the driver would buffer them all before fetching 21.
    started = time.monotonic()
    result = load(
        await my_tools.run_select(
            "SELECT a.customer_id FROM sales.customer a CROSS JOIN sales.customer b"
        )
    )
    assert result["row_count"] == 20 and result["truncated"] is True
    assert time.monotonic() - started < 5


@pytest.mark.spec("S07-AC5", "S07-AC3")
async def test_blocked_dml_times_out_is_killed_and_commits_nothing(
    admin: Any, my_settings: MySQLServerSettings, my_scratch: str
) -> None:
    # Another session holds the row lock, so the UPDATE waits past the statement timeout.
    holder = await my_admin(admin)
    tools = MySQLTools(my_settings.model_copy(update={"mcp_statement_timeout_ms": 500}))
    try:
        async with holder.cursor() as cur:
            await cur.execute("SET autocommit = 0")
            await cur.execute("SELECT * FROM sales.mcp_scratch WHERE id = 1 FOR UPDATE")
            with pytest.raises(ToolFailed, match="timed out; nothing was committed"):
                await tools.execute_dml("UPDATE sales.mcp_scratch SET note = 'x' WHERE id = 1", 1)
            await cur.execute(
                "SELECT count(*) FROM performance_schema.processlist"
                " WHERE user = 'mcp_writer' AND info LIKE 'UPDATE%'"
            )
            assert (await cur.fetchone())[0] == 0  # KILL QUERY stopped it
            await cur.execute("ROLLBACK")
            await cur.execute("SELECT note FROM sales.mcp_scratch WHERE id = 1")
            assert (await cur.fetchone())[0] == "n1"
    finally:
        holder.close()
        await tools.close()


@pytest.mark.spec("S07-AC9")
async def test_describe_reports_composite_unique_keys(
    my_tools: MySQLTools, my_scratch: str
) -> None:
    described = load(await my_tools.describe_table("sales.mcp_scratch"))
    assert described["primary_key"] == ["id"]
    assert described["unique"] == [["note", "id"]]
