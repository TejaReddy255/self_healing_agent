"""PII reference rejection for run_select (S07-AC8, validation half)."""

from __future__ import annotations

import pytest
import sqlglot

from mcp_servers.common.errors import ToolRejected
from mcp_servers.common.identifiers import Dialect
from mcp_servers.common.pii import PiiPolicy
from mcp_servers.common.sql import validate_select

POLICY = PiiPolicy(
    ["dw.customer.email", "dw.customer.phone", "sales.customer.email", "sales.customer.phone"]
)

REJECTS = [
    "SELECT email FROM {s}.customer",
    "SELECT EMAIL FROM {s}.customer",
    "SELECT c.phone FROM {s}.customer c",
    "SELECT customer.email FROM {s}.customer",
    "SELECT * FROM {s}.customer",
    "SELECT c.* FROM {s}.customer c",
    "SELECT customer_id FROM {s}.customer WHERE email LIKE 'a%'",
    "SELECT customer_id FROM {s}.customer ORDER BY phone",
    "SELECT customer_id FROM {s}.customer GROUP BY email",
    "SELECT lower(email) AS e FROM {s}.customer",
    "SELECT count(DISTINCT email) FROM {s}.customer",
    "SELECT x FROM (SELECT email AS x FROM {s}.customer) d",
    "SELECT d.x FROM (SELECT * FROM {s}.customer) d",
    "WITH d AS (SELECT phone FROM {s}.customer) SELECT * FROM d",
    "SELECT a.customer_id FROM {s}.customer a JOIN {s}.customer b ON a.email = b.email",
    "SELECT customer_id FROM {s}.customer UNION SELECT email FROM {s}.customer",
    "SELECT email FROM {s}.customer_other JOIN {s}.customer ON 1 = 1",
]

ACCEPTS = [
    "SELECT count(*) FROM {s}.customer",
    "SELECT customer_id, name, country FROM {s}.customer",
    "SELECT c.customer_id FROM {s}.customer c WHERE c.country = 'GB'",
    "SELECT customer_id, count(*) FROM {s}.customer GROUP BY customer_id HAVING count(*) > 1",
    "SELECT max(updated_at) FROM {s}.customer",
]

PG_ONLY_REJECTS = [
    "SELECT row_to_json(c) FROM dw.customer c",
    "SELECT to_jsonb(c.*) FROM dw.customer c",
    "SELECT c FROM dw.customer c",
    "SELECT customer FROM dw.customer",
]


def check(sql: str, dialect: Dialect, schema: str) -> None:
    POLICY.check_select(validate_select(sql.replace("{s}", schema), dialect, [schema]))


@pytest.mark.spec("S07-AC8")
@pytest.mark.parametrize("sql", REJECTS)
@pytest.mark.parametrize(("dialect", "schema"), [("postgres", "dw"), ("mysql", "sales")])
def test_pii_references_are_rejected(sql: str, dialect: Dialect, schema: str) -> None:
    with pytest.raises(ToolRejected, match="PII"):
        check(sql, dialect, schema)


@pytest.mark.spec("S07-AC8")
@pytest.mark.parametrize("sql", PG_ONLY_REJECTS)
def test_whole_row_references_are_rejected(sql: str) -> None:
    with pytest.raises(ToolRejected):
        check(sql, "postgres", "dw")
    # The PII policy refuses them on its own too, independent of the function allowlist.
    with pytest.raises(ToolRejected, match="PII"):
        POLICY.check_select(sqlglot.parse_one(sql, read="postgres"))


# Bypasses found in review (2026-10-01).
REVIEW_BYPASSES = [
    # column alias list renames email to e
    ("SELECT t.e FROM dw.customer AS t(a, b, c, e)", "alias lists"),
    ("SELECT e FROM dw.customer t(l, i, n, e)", "alias lists"),
    # SQL in a string argument: invisible to table and PII checks
    ("SELECT table_to_xml('dw.customer', true, false, '')", "function not allowed"),
    ("SELECT schema_to_xml('dw', true, false, '')", "function not allowed"),
    ("SELECT query_to_xml_and_xmlschema('select email from dw.customer', true, false, '')",
     "function not allowed"),
    ("SELECT * FROM ts_stat('select to_tsvector(email) from dw.customer')", "function not allowed"),
    ("SELECT unnest(xpath('//email/text()', table_to_xml('dw.customer', true, false, '')))",
     "function not allowed"),
    # quoting a function name does not disguise it
    ("SELECT \"set_config\"('statement_timeout', '0', false)", "function not allowed"),
    ("SELECT \"pg_terminate_backend\"(1)", "function not allowed"),
    # a CTE name does not cover the real table inside its own body or outside its scope
    ("WITH pg_stats AS (SELECT attname FROM pg_stats) SELECT * FROM pg_stats", "qualify"),
    # membership oracle through join keys
    ("SELECT count(*) FROM dw.customer JOIN dw.probe v USING (email)", "USING"),
    ("SELECT count(*) FROM dw.customer NATURAL JOIN dw.probe", "NATURAL"),
]  # fmt: skip


@pytest.mark.spec("S07-AC8")
@pytest.mark.parametrize(("sql", "reason"), REVIEW_BYPASSES)
def test_review_bypasses_are_closed(sql: str, reason: str) -> None:
    with pytest.raises(ToolRejected, match=reason):
        check(sql, "postgres", "dw")


@pytest.mark.spec("S07-AC8")
def test_mysql_cte_shadowing_cannot_read_an_unqualified_table() -> None:
    sql = (
        "SELECT email FROM customer"
        " WHERE EXISTS (WITH customer AS (SELECT 1) SELECT 1 FROM customer)"
    )
    with pytest.raises(ToolRejected, match="qualify"):
        check(sql, "mysql", "sales")


@pytest.mark.spec("S07-AC8")
def test_alias_lists_and_using_are_fine_without_pii() -> None:
    check("SELECT s.d FROM dw.sales_daily AS s(d, st, amt)", "postgres", "dw")
    check("SELECT count(*) FROM dw.customer JOIN dw.probe p USING (customer_id)", "postgres", "dw")


@pytest.mark.spec("S07-AC8")
@pytest.mark.parametrize("sql", ACCEPTS)
@pytest.mark.parametrize(("dialect", "schema"), [("postgres", "dw"), ("mysql", "sales")])
def test_queries_avoiding_pii_are_accepted(sql: str, dialect: Dialect, schema: str) -> None:
    check(sql, dialect, schema)


@pytest.mark.spec("S07-AC8")
def test_tables_without_pii_allow_star() -> None:
    check("SELECT * FROM dw.sales_daily", "postgres", "dw")


@pytest.mark.spec("S07-AC8")
def test_rejection_names_the_columns_but_no_values() -> None:
    with pytest.raises(ToolRejected) as info:
        check("SELECT * FROM dw.customer WHERE email = 'customer1@example.test'", "postgres", "dw")
    message = str(info.value)
    assert "dw.customer.email" in message and "dw.customer.phone" in message
    assert "example.test" not in message


@pytest.mark.spec("S07-AC8")
def test_is_pii_is_case_insensitive() -> None:
    assert POLICY.is_pii("DW", "Customer", "Email")
    assert not POLICY.is_pii("dw", "customer", "customer_id")
    assert POLICY.columns_of("sales", "customer") == {"email", "phone"}
