"""SQL validation with sqlglot (S07).

Validation is one layer of several: reads also run under a read-only role in a read-only
transaction, writes under row ceilings with rollback. Anything that does not parse cleanly is
rejected rather than guessed at.
"""

from __future__ import annotations

import logging

import sqlglot
import sqlglot.expressions as exp
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.optimizer.scope import traverse_scope

from mcp_servers.common.errors import ToolRejected
from mcp_servers.common.identifiers import Dialect

# sqlglot logs a warning when it falls back to a raw Command; we reject those anyway.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

MAX_SQL_CHARS = 20_000
# Functions are allowlisted (security review 2026-10-01): a denylist missed functions that run SQL
# passed as a string (table_to_xml, query_to_xml_and_xmlschema, ts_stat, ts_rewrite, ...), which
# the table and PII checks cannot see. Typed sqlglot functions are matched by their sqlglot name,
# unknown ("anonymous") ones by their lowercase name. Anything else is refused with its name.
ALLOWED_FUNCTIONS = frozenset(
    {
        # structure sqlglot models as functions
        "AND", "OR", "NOT", "CASE", "IF", "CAST", "TRY_CAST", "COALESCE", "NULLIF", "EXISTS",
        "ARRAY", "GREATEST", "LEAST",
        # aggregates
        "COUNT", "COUNT_IF", "SUM", "AVG", "MIN", "MAX", "STDDEV", "STDDEV_POP", "STDDEV_SAMP",
        "VARIANCE", "VARIANCE_POP", "APPROX_DISTINCT", "MEDIAN", "PERCENTILE_CONT",
        "PERCENTILE_DISC", "LOGICAL_AND", "LOGICAL_OR", "ARRAY_AGG", "GROUP_CONCAT",
        # window functions
        "ROW_NUMBER", "RANK", "DENSE_RANK", "NTILE", "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE",
        "NTH_VALUE", "PERCENT_RANK", "CUME_DIST",
        # strings
        "LOWER", "UPPER", "LENGTH", "SUBSTRING", "TRIM", "CONCAT", "CONCAT_WS", "REPLACE", "LEFT",
        "RIGHT", "PAD", "STR_POSITION", "SPLIT_PART", "REVERSE", "INITCAP", "REPEAT", "STARTS_WITH",
        "REGEXP_LIKE", "REGEXP_REPLACE", "MD5",
        # numbers
        "ABS", "ROUND", "FLOOR", "CEIL", "SQRT", "POWER", "SIGN", "LN", "LOG", "EXP", "TRUNC",
        # dates and times
        "CURRENT_DATE", "CURRENT_TIMESTAMP", "CURRENT_TIME", "DATE", "DATE_ADD", "DATE_SUB",
        "DATEDIFF", "DATE_DIFF", "TIMESTAMPDIFF", "TIMESTAMP_TRUNC", "DATE_TRUNC", "EXTRACT",
        "TIME_TO_STR", "STR_TO_DATE", "STR_TO_TIME", "UNIX_TO_TIME", "TS_OR_DS_TO_DATE",
        "TS_OR_DS_TO_TIMESTAMP", "LAST_DAY", "YEAR", "MONTH", "DAY", "DAY_OF_WEEK", "WEEK", "HOUR",
        # set-returning helpers for gap analysis (bounded by the statement timeout)
        "EXPLODE", "GENERATE_SERIES", "EXPLODING_GENERATE_SERIES",
    }
)  # fmt: skip
ALLOWED_ANONYMOUS = frozenset(
    {
        "now", "age", "make_date", "date_part", "to_char", "std", "unix_timestamp", "minute",
        "second", "char_length", "character_length", "ifnull",
        # bounded by the statement timeout; used to prove it (S07-AC5)
        "pg_sleep", "sleep",
    }
)  # fmt: skip
# Node types that must never appear inside a read.
_FORBIDDEN_IN_READ: tuple[type[exp.Expression], ...] = (
    exp.Into,
    exp.Lock,
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Copy,
    exp.Set,
    exp.TruncateTable,
)


def _parse_one(sql: str, dialect: Dialect) -> exp.Expression:
    if not isinstance(sql, str) or not sql.strip():
        raise ToolRejected("empty SQL")
    if len(sql) > MAX_SQL_CHARS:
        raise ToolRejected(f"SQL longer than {MAX_SQL_CHARS} characters")
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except SqlglotError:
        raise ToolRejected("SQL could not be parsed") from None
    if len(statements) != 1:
        raise ToolRejected("exactly one SQL statement is allowed")
    statement = statements[0]
    if not isinstance(statement, exp.Expression) or isinstance(statement, exp.Command):
        raise ToolRejected("this statement type is not allowed")
    return statement


def _check_functions(tree: exp.Expression) -> None:
    refused: set[str] = set()
    for node in tree.walk():
        if isinstance(node, exp.Anonymous):
            name = node.name.lower()  # the identifier's text: quoting cannot disguise a name
            if name not in ALLOWED_ANONYMOUS:
                refused.add(name)
        elif isinstance(node, exp.Func) and node.sql_name() not in ALLOWED_FUNCTIONS:
            refused.add(node.sql_name().lower())
    if refused:
        raise ToolRejected(f"function not allowed: {', '.join(sorted(refused))}")


def _names_a_table(table: exp.Table) -> bool:
    """False for a table function in FROM (generate_series(...)): the allowlist governs those."""
    return isinstance(table.this, exp.Identifier)


def _check_tables(tree: exp.Expression, allowed_schemas: list[str]) -> None:
    """Every table is ``allowed_schema.table`` or a CTE visible in its own scope.

    A CTE name only counts where SQL scoping makes it visible: inside a non-recursive CTE's own
    body, or outside the subquery that defines it, the same unqualified name means a real table
    (``WITH pg_stats AS (SELECT ... FROM pg_stats)`` reads ``pg_catalog.pg_stats``).
    """
    try:
        scopes = traverse_scope(tree)
    except (OptimizeError, SqlglotError):
        raise ToolRejected("the query structure could not be analysed") from None
    real_unqualified = [
        source
        for scope in scopes
        for source in scope.sources.values()
        if isinstance(source, exp.Table) and _names_a_table(source) and not source.db
    ]
    if real_unqualified:
        raise ToolRejected(
            f"qualify table {real_unqualified[0].name!r} with its schema (schema.table)"
        )
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if not _names_a_table(table):
            continue
        schema = (table.db or "").lower()
        if not schema:
            if table.name.lower() in ctes:
                continue
            raise ToolRejected(f"qualify table {table.name!r} with its schema (schema.table)")
        if table.catalog:
            raise ToolRejected("cross-database references are not allowed")
        if schema not in allowed_schemas:
            raise ToolRejected(f"schema {table.db!r} is not available on this server")


def validate_select(sql: str, dialect: Dialect, allowed_schemas: list[str]) -> exp.Expression:
    """A single read-only query over allowed schemas."""
    tree = _parse_one(sql, dialect)
    if not isinstance(tree, (exp.Select, exp.SetOperation)):
        raise ToolRejected("only SELECT queries are allowed")
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_IN_READ):
            raise ToolRejected(f"{node.key.upper()} is not allowed in a read")
        if isinstance(node, exp.Select) and node.args.get("locks"):
            raise ToolRejected("locking clauses are not allowed in a read")
    _check_functions(tree)
    _check_tables(tree, allowed_schemas)
    return tree


def validate_dml(sql: str, dialect: Dialect, allowed_schemas: list[str]) -> exp.Expression:
    """One INSERT/UPDATE/DELETE on an allowed schema; UPDATE/DELETE need a real WHERE."""
    tree = _parse_one(sql, dialect)
    if not isinstance(tree, (exp.Insert, exp.Update, exp.Delete)):
        raise ToolRejected("only INSERT, UPDATE or DELETE statements are allowed")
    for node in tree.walk():
        if node is not tree and isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Merge)):
            raise ToolRejected("nested data-modifying statements are not allowed")
        if isinstance(node, (exp.Into, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Copy)):
            raise ToolRejected(f"{node.key.upper()} is not allowed in DML")
    if isinstance(tree, (exp.Update, exp.Delete)):
        where = tree.args.get("where")
        if where is None:
            raise ToolRejected(f"{tree.key.upper()} requires a WHERE clause")
        if not any(True for _ in where.find_all(exp.Column)):
            raise ToolRejected("the WHERE clause must reference at least one column")
    _check_functions(tree)
    _check_tables(tree, allowed_schemas)
    return tree


def validate_ddl(sql: str, allowed_schemas: list[str]) -> exp.Expression:
    """PostgreSQL allowlist: CREATE TABLE … PARTITION OF … and (non-concurrent) CREATE INDEX."""
    tree = _parse_one(sql, "postgres")
    if not isinstance(tree, exp.Create):
        raise ToolRejected("only CREATE TABLE … PARTITION OF and CREATE INDEX are allowed")
    kind = str(tree.args.get("kind") or "").upper()
    if kind == "INDEX":
        if tree.args.get("concurrently"):
            raise ToolRejected("CREATE INDEX CONCURRENTLY cannot run in a transaction")
    elif kind == "TABLE":
        properties = tree.args.get("properties")
        partition_of = properties is not None and any(
            isinstance(p, exp.PartitionedOfProperty) for p in properties.expressions
        )
        if not partition_of or tree.args.get("expression") is not None:
            raise ToolRejected("only CREATE TABLE … PARTITION OF … is allowed")
    else:
        raise ToolRejected("only CREATE TABLE … PARTITION OF and CREATE INDEX are allowed")
    for node in tree.walk():
        if isinstance(node, (exp.Select, exp.Command, exp.Drop, exp.Alter, exp.Insert)):
            raise ToolRejected(f"{node.key.upper()} is not allowed in DDL")
    _check_functions(tree)  # DDL runs as dw_owner: no function outside the allowlist
    _check_tables(tree, allowed_schemas)
    return tree


def cap_rows(tree: exp.Expression, limit: int) -> exp.Expression:
    """At most ``limit`` rows from the server (a smaller literal LIMIT is kept; OFFSET is kept).

    MySQL drivers buffer whole result sets, so the cap belongs in the query itself.
    """
    current = tree.args.get("limit")
    if isinstance(current, exp.Limit):
        value = current.expression
        if isinstance(value, exp.Literal) and value.is_int and int(value.name) <= limit:
            return tree
    capped = tree.copy()
    capped.set("limit", exp.Limit(expression=exp.Literal.number(limit)))
    return capped


def render(tree: exp.Expression, dialect: Dialect) -> str:
    """The SQL actually executed: regenerated from the validated tree, comments dropped."""
    return tree.sql(dialect=dialect, comments=False)


def affected_tables(tree: exp.Expression) -> list[tuple[str, str]]:
    return sorted(
        {((t.db or "").lower(), t.name.lower()) for t in tree.find_all(exp.Table) if t.db}
    )
