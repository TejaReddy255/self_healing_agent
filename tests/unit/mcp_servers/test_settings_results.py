"""Server settings and bounded results (S07-AC5, AC10, AC11)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from mcp_servers.common.duplicates import check_key_columns
from mcp_servers.common.duplicates import result as duplicate_result
from mcp_servers.common.errors import ToolRejected
from mcp_servers.common.results import MAX_TEXT_CHARS, safe_value, table_result
from mcp_servers.common.settings import MySQLServerSettings, PostgresServerSettings
from mcp_servers.common.sql import cap_rows, render, validate_select

DSN = "postgresql://mcp_reader:s3cr3t-pw@127.0.0.1:55433/warehouse"


@pytest.mark.spec("S07-AC5")
def test_defaults() -> None:
    settings = PostgresServerSettings(_env_file=None)
    assert settings.mcp_row_limit == 20
    assert settings.mcp_statement_timeout_ms == 30_000
    assert settings.mcp_postgres_schemas == ["dw"]
    assert MySQLServerSettings(_env_file=None).mcp_mysql_schemas == ["sales"]


@pytest.mark.spec("S07-AC11")
@pytest.mark.parametrize("value", ["", "   "])
def test_blank_writer_dsn_means_no_writer(value: str) -> None:
    settings = PostgresServerSettings(_env_file=None, mcp_postgres_writer_dsn=value)
    assert settings.mcp_postgres_writer_dsn is None


@pytest.mark.spec("S07-AC10")
def test_dsn_is_secret_in_repr_and_errors() -> None:
    settings = PostgresServerSettings(_env_file=None, mcp_postgres_reader_dsn=DSN)
    assert "s3cr3t" not in repr(settings) and "s3cr3t" not in str(settings.model_dump())
    with pytest.raises(ValidationError) as info:
        PostgresServerSettings(_env_file=None, mcp_postgres_reader_dsn=DSN, mcp_row_limit=0)
    assert "s3cr3t" not in str(info.value)


@pytest.mark.spec("S07-AC8")
def test_pii_columns_from_json_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_PII_COLUMNS", '["DW.Customer.Email"]')
    assert PostgresServerSettings(_env_file=None).mcp_pii_columns == ["dw.customer.email"]
    monkeypatch.setenv("MCP_PII_COLUMNS", '["customer.email"]')
    with pytest.raises(ValidationError):
        PostgresServerSettings(_env_file=None)
    monkeypatch.setenv("MCP_PII_COLUMNS", "dw.customer.email")
    with pytest.raises(ValidationError):
        PostgresServerSettings(_env_file=None)


@pytest.mark.spec("S07-AC9")
@pytest.mark.parametrize("value", ['["dw; drop"]', "[]", '["a.b"]'])
def test_schema_allowlist_must_be_plain_names(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MCP_POSTGRES_SCHEMAS", value)
    with pytest.raises(ValidationError):
        PostgresServerSettings(_env_file=None)


@pytest.mark.spec("S07-AC5")
def test_table_result_truncates_at_limit() -> None:
    rows = [(i, f"n{i}") for i in range(21)]
    result = json.loads(table_result(["id", "name"], rows, 20))
    assert result["truncated"] is True and result["row_count"] == 20 and len(result["rows"]) == 20
    exact = json.loads(table_result(["id", "name"], rows[:20], 20))
    assert exact["truncated"] is False


@pytest.mark.spec("S07-AC5")
def test_values_are_json_safe_and_bounded() -> None:
    assert safe_value(Decimal("1.50")) == "1.50"
    assert safe_value(date(2026, 10, 1)) == "2026-10-01"
    assert safe_value(b"\x00\x01") == "<2 bytes>"
    assert safe_value(float("nan")) == "nan"
    assert len(safe_value("x" * 1000)) == MAX_TEXT_CHARS + 1


@pytest.mark.spec("S07-AC10")
@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "example.com"])  # noqa: S104
def test_servers_bind_to_loopback_only(host: str) -> None:
    with pytest.raises(ValidationError):
        PostgresServerSettings(_env_file=None, mcp_bind_host=host)
    assert PostgresServerSettings(_env_file=None, mcp_bind_host="::1").mcp_bind_host == "::1"


@pytest.mark.spec("S07-AC5")
@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT a FROM s.t", "SELECT a FROM s.t LIMIT 21"),
        ("SELECT a FROM s.t LIMIT 5", "SELECT a FROM s.t LIMIT 5"),
        ("SELECT a FROM s.t LIMIT 500", "SELECT a FROM s.t LIMIT 21"),
        ("SELECT a FROM s.t LIMIT 10, 500", "SELECT a FROM s.t LIMIT 21 OFFSET 10"),
        (
            "SELECT a FROM s.t UNION SELECT b FROM s.u",
            "SELECT a FROM s.t UNION SELECT b FROM s.u LIMIT 21",
        ),
    ],
)
def test_mysql_row_cap_is_part_of_the_query(sql: str, expected: str) -> None:
    tree = validate_select(sql, "mysql", ["s"])
    assert render(cap_rows(tree, 21), "mysql") == expected


@pytest.mark.spec("S07-AC4")
@pytest.mark.parametrize(
    "keys", [[], ["a"] * 9, ["a", "A"], ["mcp_dup_count"], ["a; drop"], "customer_id"]
)
def test_duplicate_key_columns_are_checked(keys: Any) -> None:
    with pytest.raises(ToolRejected):
        check_key_columns(keys, lower=True)


@pytest.mark.spec("S07-AC4", "S07-AC8")
def test_duplicate_result_contract_and_masking() -> None:
    out = json.loads(
        duplicate_result("dw.customer", ["customer_id", "email"], (2, 5, 3),
                         [(7, "x@example.test", 3), (9, "y@example.test", 2)], {"email"})
    )  # fmt: skip
    assert (out["duplicate_keys"], out["duplicate_rows"], out["excess_rows"]) == (2, 5, 3)
    assert out["sample"][0] == {"key": {"customer_id": 7, "email": "***"}, "count": 3}
    assert "example.test" not in json.dumps(out)
