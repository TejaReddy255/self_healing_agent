"""``find_duplicates``: shared SQL shape and result contract for both servers (S07).

Result: ``duplicate_keys`` (key values occurring more than once), ``duplicate_rows`` (rows that
belong to those keys) and ``excess_rows`` (rows beyond one per key: what a de-duplication would
remove), plus at most five sample keys with their counts. Sample values of PII key columns are
masked.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mcp_servers.common.errors import ToolRejected
from mcp_servers.common.identifiers import Dialect, check_identifier, quote
from mcp_servers.common.pii import MASK
from mcp_servers.common.results import dumps, safe_value

MAX_KEY_COLUMNS = 8
SAMPLE_GROUPS = 5
COUNT_ALIAS = "mcp_dup_count"


def check_key_columns(key_columns: list[str], *, lower: bool) -> list[str]:
    if not isinstance(key_columns, list) or not 1 <= len(key_columns) <= MAX_KEY_COLUMNS:
        raise ToolRejected(f"give between 1 and {MAX_KEY_COLUMNS} key columns")
    keys = [check_identifier(c, "column") for c in key_columns]
    keys = [k.lower() for k in keys] if lower else keys
    if COUNT_ALIAS in {k.lower() for k in keys} or len({k.lower() for k in keys}) != len(keys):
        raise ToolRejected("key columns must be distinct")
    return keys


def queries(keys: list[str], source: str, dialect: Dialect) -> tuple[str, str]:
    """(summary, sample) SQL. ``source`` and ``keys`` are validated, catalog-checked identifiers."""
    cols = ", ".join(quote(k, dialect) for k in keys)
    groups = (
        f"SELECT {cols}, count(*) AS {COUNT_ALIAS} FROM {source}"  # noqa: S608
        f" GROUP BY {cols} HAVING count(*) > 1"
    )
    summary = (
        f"SELECT count(*), coalesce(sum({COUNT_ALIAS}), 0),"  # noqa: S608
        f" coalesce(sum({COUNT_ALIAS} - 1), 0) FROM ({groups}) d"
    )
    sample = (
        f"SELECT * FROM ({groups}) d"  # noqa: S608
        f" ORDER BY {COUNT_ALIAS} DESC, {cols} LIMIT {SAMPLE_GROUPS}"
    )
    return summary, sample


def result(
    table: str,
    keys: list[str],
    summary: Sequence[Any],
    sample: Sequence[Sequence[Any]],
    masked: set[str],
) -> str:
    """``sample`` rows are the key values in ``keys`` order followed by the count."""
    keys_n, rows_n, excess = summary
    return dumps(
        {
            "table": table,
            "key_columns": keys,
            "duplicate_keys": int(keys_n),
            "duplicate_rows": int(rows_n),
            "excess_rows": int(excess),
            "sample": [
                {
                    "key": {
                        k: MASK if k in masked else safe_value(row[i]) for i, k in enumerate(keys)
                    },
                    "count": int(row[len(keys)]),
                }
                for row in sample
            ],
        }
    )
