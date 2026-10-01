"""Identifier checks and quoting (S07).

Tool arguments that name schemas, tables or columns are matched against a strict pattern, checked
for existence by the server (parameterized catalog lookups) and only then quoted. User text is never
concatenated into SQL.
"""

from __future__ import annotations

import re
from typing import Literal

from mcp_servers.common.errors import ToolRejected

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
Dialect = Literal["postgres", "mysql"]


def check_identifier(value: str, what: str = "identifier") -> str:
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        raise ToolRejected(f"invalid {what}: use a plain name of letters, digits and underscores")
    return value


def split_table(value: str, allowed_schemas: list[str]) -> tuple[str, str]:
    """``schema.table`` with an allowed schema; both parts validated."""
    parts = value.split(".") if isinstance(value, str) else []
    if len(parts) != 2:
        raise ToolRejected("tables must be written as schema.table")
    schema, table = check_identifier(parts[0], "schema"), check_identifier(parts[1], "table")
    if schema.lower() not in allowed_schemas:
        raise ToolRejected(f"schema {schema!r} is not available on this server")
    return schema, table


def quote(value: str, dialect: Dialect) -> str:
    """Quote a *validated* identifier (the pattern excludes quote characters)."""
    check_identifier(value)
    return f"`{value}`" if dialect == "mysql" else f'"{value}"'


def qualified(schema: str, table: str, dialect: Dialect) -> str:
    return f"{quote(schema, dialect)}.{quote(table, dialect)}"
