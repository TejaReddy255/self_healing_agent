"""SQL validation for the database MCP servers (S07-AC1, AC3 validation, AC7)."""

from __future__ import annotations

import pytest

from mcp_servers.common.errors import ToolRejected
from mcp_servers.common.identifiers import Dialect
from mcp_servers.common.sql import (
    MAX_SQL_CHARS,
    affected_tables,
    render,
    validate_ddl,
    validate_dml,
    validate_select,
)

PG: Dialect = "postgres"
MY: Dialect = "mysql"

# Statements every dialect must refuse as a read. (sql, reason fragment or "" for any refusal)
COMMON_READ_REJECTS = [
    "",
    "   ",
    "SELEC 1 FROM {s}.t",
    "SELECT 1 FROM {s}.t; SELECT 2 FROM {s}.t",
    "SELECT 1 FROM {s}.t; DROP TABLE {s}.t",
    "INSERT INTO {s}.t (a) VALUES (1)",
    "UPDATE {s}.t SET a = 1 WHERE b = 2",
    "DELETE FROM {s}.t WHERE a = 1",
    "DROP TABLE {s}.t",
    "TRUNCATE TABLE {s}.t",
    "CREATE TABLE {s}.x (a INT)",
    "ALTER TABLE {s}.t ADD COLUMN z INT",
    "GRANT SELECT ON {s}.t TO someone",
    "CALL {s}.proc()",
    "SET search_path = other",
    "SELECT * FROM {s}.t FOR UPDATE",
    "SELECT * FROM t",
    "SELECT * FROM other_schema.t",
    "SELECT * FROM {s}.t JOIN other_schema.u ON t.a = u.a",
    "SELECT * FROM {s}.t WHERE a IN (SELECT a FROM other_schema.u)",
    "SELECT * FROM {s}.t UNION SELECT * FROM other_schema.u",
    "SELECT * FROM information_schema.tables",
    "EXPLAIN ANALYZE SELECT * FROM {s}.t",
    "SHOW TABLES",
]

PG_READ_REJECTS = [
    "SELECT * INTO dw.copy FROM dw.t",
    "WITH d AS (DELETE FROM dw.t WHERE a = 1 RETURNING *) SELECT * FROM d",
    "WITH i AS (INSERT INTO dw.t (a) VALUES (1) RETURNING a) SELECT a FROM i",
    "COPY dw.t TO '/tmp/out.csv'",
    "DO $$ BEGIN PERFORM 1; END $$",
    "SELECT * FROM dw.t FOR SHARE",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT set_config('statement_timeout', '0', false)",
    "SELECT * FROM dblink('host=evil', 'select 1') AS x(a int)",
    "SELECT pg_terminate_backend(1)",
    "SELECT * FROM pg_catalog.pg_authid",
    "SELECT * FROM warehouse.dw.t",
    "VACUUM dw.t",
    "LISTEN chan",
    "WITH pg_stats AS (SELECT attname FROM pg_stats) SELECT * FROM pg_stats",
    "SELECT table_to_xml('dw.customer', true, false, '')",
    "SELECT * FROM ts_stat('select 1')",
    "SELECT \"set_config\"('a', 'b', false)",
    "SELECT current_setting('data_directory')",
    "SELECT lo_create(0)",
]

MY_READ_REJECTS = [
    "SELECT * INTO OUTFILE '/tmp/x' FROM sales.t",
    "SELECT a INTO @v FROM sales.t",
    "SELECT * FROM sales.t LOCK IN SHARE MODE",
    "SELECT LOAD_FILE('/etc/passwd')",
    "SELECT GET_LOCK('x', 10)",
    "SELECT * FROM mysql.user",
    "REPLACE INTO sales.t (a) VALUES (1)",
    "HANDLER sales.t OPEN",
    "LOAD DATA INFILE '/tmp/x' INTO TABLE sales.t",
    "SELECT * FROM performance_schema.threads",
    "SELECT email FROM customer WHERE EXISTS (WITH customer AS (SELECT 1) SELECT 1 FROM customer)",
    "SELECT `load_file`('/etc/passwd')",
    "SELECT benchmark(100000000, md5('x'))",
]

PG_READ_ACCEPTS = [
    "SELECT 1",
    "SELECT count(*) FROM dw.customer",
    "SELECT customer_id, count(*) FROM dw.customer GROUP BY customer_id HAVING count(*) > 1",
    "WITH d AS (SELECT customer_id FROM dw.customer) SELECT count(*) FROM d",
    "SELECT c.customer_id FROM dw.customer c JOIN dw.sales_daily s ON s.store_id = c.load_id",
    "SELECT customer_id FROM dw.customer UNION ALL SELECT customer_id FROM dw.customer",
    "SELECT max(loaded_at) FROM dw.customer -- trailing comment",
    "SELECT DATE_TRUNC('month', sale_date), sum(amount) FROM dw.sales_daily GROUP BY 1",
    "SELECT d::date FROM generate_series(DATE '2026-08-01', DATE '2026-09-30', INTERVAL '1 day') d"
    " WHERE d::date NOT IN (SELECT sale_date FROM dw.sales_daily)",
    "SELECT store_id, row_number() OVER (PARTITION BY store_id ORDER BY sale_date DESC),"
    " lag(amount) OVER (ORDER BY sale_date), coalesce(amount, 0), round(avg(amount), 2)"
    " FROM dw.sales_daily GROUP BY store_id, sale_date, amount",
    "SELECT lower(name), length(name), extract(year FROM loaded_at), now() - loaded_at"
    " FROM dw.customer WHERE name LIKE 'A%' AND country IN ('GB', 'US')",
    "WITH a AS (SELECT 1 AS x), b AS (SELECT x FROM a) SELECT * FROM b",
]

MY_READ_ACCEPTS = [
    "SELECT 1",
    "SELECT count(*) FROM sales.customer",
    "SELECT `customer_id` FROM `sales`.`customer` LIMIT 5",
    "SELECT country, count(*) FROM sales.customer GROUP BY country",
    "WITH d AS (SELECT customer_id FROM sales.customer) SELECT count(*) FROM d",
    "SELECT max(updated_at) FROM sales.customer /* comment */",
    "SELECT date_format(updated_at, '%Y-%m'), count(*), ifnull(country, '?')"
    " FROM sales.customer GROUP BY 1, 3",
    "SELECT year(updated_at), datediff(now(), updated_at), if(country = 'GB', 1, 0)"
    " FROM sales.customer",
]


def _fill(sql: str, schema: str) -> str:
    return sql.replace("{s}", schema)


@pytest.mark.spec("S07-AC1")
@pytest.mark.parametrize("sql", COMMON_READ_REJECTS + PG_READ_REJECTS)
def test_postgres_rejects_non_reads(sql: str) -> None:
    assert len(COMMON_READ_REJECTS + PG_READ_REJECTS) >= 20
    with pytest.raises(ToolRejected):
        validate_select(_fill(sql, "dw"), PG, ["dw"])


@pytest.mark.spec("S07-AC1")
@pytest.mark.parametrize("sql", COMMON_READ_REJECTS + MY_READ_REJECTS)
def test_mysql_rejects_non_reads(sql: str) -> None:
    assert len(COMMON_READ_REJECTS + MY_READ_REJECTS) >= 20
    with pytest.raises(ToolRejected):
        validate_select(_fill(sql, "sales"), MY, ["sales"])


@pytest.mark.spec("S07-AC1")
@pytest.mark.parametrize("sql", PG_READ_ACCEPTS)
def test_postgres_accepts_plain_reads(sql: str) -> None:
    validate_select(sql, PG, ["dw"])


@pytest.mark.spec("S07-AC1")
@pytest.mark.parametrize("sql", MY_READ_ACCEPTS)
def test_mysql_accepts_plain_reads(sql: str) -> None:
    validate_select(sql, MY, ["sales"])


@pytest.mark.spec("S07-AC1")
def test_oversized_sql_is_rejected() -> None:
    values = ",".join(["1"] * MAX_SQL_CHARS)
    sql = f"SELECT 1 FROM dw.t WHERE a IN ({values})"  # noqa: S608 - test input only
    with pytest.raises(ToolRejected, match="longer than"):
        validate_select(sql, PG, ["dw"])


@pytest.mark.spec("S07-AC1")
def test_regenerated_sql_drops_comments_and_keeps_meaning() -> None:
    tree = validate_select("SELECT a /* x */ FROM dw.t -- y", PG, ["dw"])
    assert render(tree, "postgres") == "SELECT a FROM dw.t"


# --- DML (AC3 validation) -----------------------------------------------------------------

DML_REJECTS = [
    ("UPDATE {s}.t SET a = 1", "WHERE"),
    ("DELETE FROM {s}.t", "WHERE"),
    ("UPDATE {s}.t SET a = 1 WHERE 1 = 1", "column"),
    ("DELETE FROM {s}.t WHERE TRUE", "column"),
    ("DELETE FROM {s}.t WHERE 'x' = 'x'", "column"),
    ("SELECT * FROM {s}.t", "only INSERT"),
    ("DROP TABLE {s}.t", ""),
    ("TRUNCATE TABLE {s}.t", ""),
    ("DELETE FROM other.t WHERE a = 1", "schema"),
    ("INSERT INTO {s}.t SELECT * FROM other.u", "schema"),
    ("UPDATE {s}.t SET a = 1 WHERE b = 2; DELETE FROM {s}.t WHERE a = 1", "exactly one"),
    ("DELETE FROM t WHERE a = 1", "qualify"),
]


@pytest.mark.spec("S07-AC3")
@pytest.mark.parametrize(("sql", "reason"), DML_REJECTS)
@pytest.mark.parametrize(("dialect", "schema"), [(PG, "dw"), (MY, "sales")])
def test_dml_rejections(sql: str, reason: str, dialect: Dialect, schema: str) -> None:
    with pytest.raises(ToolRejected, match=reason or None):
        validate_dml(_fill(sql, schema), dialect, [schema])


@pytest.mark.spec("S07-AC3")
def test_postgres_rejects_nested_dml_in_cte() -> None:
    sql = "WITH d AS (DELETE FROM dw.t WHERE a = 1 RETURNING a) INSERT INTO dw.u SELECT a FROM d"
    with pytest.raises(ToolRejected):
        validate_dml(sql, PG, ["dw"])


@pytest.mark.spec("S07-AC3")
@pytest.mark.parametrize(
    ("sql", "dialect", "schema"),
    [
        ("DELETE FROM dw.customer WHERE load_id IN (1, 2)", PG, "dw"),
        ("UPDATE dw.customer SET country = 'GB' WHERE customer_id = 7", PG, "dw"),
        ("INSERT INTO dw.note (id, text) VALUES (1, 'x')", PG, "dw"),
        ("UPDATE sales.customer SET country = 'GB' WHERE customer_id = 7", MY, "sales"),
        ("DELETE FROM sales.customer WHERE customer_id > 99990", MY, "sales"),
    ],
)
def test_dml_accepts_targeted_statements(sql: str, dialect: Dialect, schema: str) -> None:
    tree = validate_dml(sql, dialect, [schema])
    assert affected_tables(tree)[0][0] == schema


# --- DDL allowlist (AC7) ------------------------------------------------------------------

DDL_REJECTS = [
    "DROP TABLE dw.sales_daily_2026_08",
    "ALTER TABLE dw.sales_daily DROP COLUMN amount",
    "ALTER TABLE dw.sales_daily DETACH PARTITION dw.sales_daily_2026_08",
    "TRUNCATE TABLE dw.customer",
    "GRANT ALL ON dw.customer TO public",
    "CREATE TABLE dw.copy AS SELECT * FROM dw.customer",
    "CREATE TABLE dw.plain (a INT)",
    "CREATE INDEX CONCURRENTLY ix ON dw.customer (customer_id)",
    "CREATE INDEX ix ON other.customer (customer_id)",
    "CREATE TABLE other.p PARTITION OF dw.sales_daily"
    " FOR VALUES FROM ('2026-10-01') TO ('2026-11-01')",
    "CREATE TABLE dw.p PARTITION OF other.sales FOR VALUES FROM ('2026-10-01') TO ('2026-11-01')",
    "CREATE ROLE evil",
    "CREATE FUNCTION dw.f() RETURNS int AS 'select 1' LANGUAGE sql",
    "CREATE VIEW dw.v AS SELECT 1",
    "CREATE INDEX ix ON dw.customer (customer_id); DROP TABLE dw.customer",
    "CREATE INDEX ix ON dw.customer (customer_id) WHERE pg_read_file('x') <> ''",
    "CREATE INDEX ix ON dw.customer (dw.evil(customer_id))",
]


@pytest.mark.spec("S07-AC7")
@pytest.mark.parametrize("sql", DDL_REJECTS)
def test_ddl_outside_allowlist_is_rejected(sql: str) -> None:
    with pytest.raises(ToolRejected):
        validate_ddl(sql, ["dw"])


@pytest.mark.spec("S07-AC7")
@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE dw.sales_daily_2026_10 PARTITION OF dw.sales_daily"
        " FOR VALUES FROM ('2026-10-01') TO ('2026-11-01')",
        "CREATE INDEX ix_customer_id ON dw.customer (customer_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_load ON dw.customer (load_id)",
    ],
)
def test_allowlisted_ddl_is_accepted(sql: str) -> None:
    validate_ddl(sql, ["dw"])
