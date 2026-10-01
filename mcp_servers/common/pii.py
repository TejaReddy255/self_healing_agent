"""PII policy for free-form reads (S07, decision 2026-10-01: reject references).

``run_select`` refuses any query that names a configured PII column anywhere, uses ``*`` / ``t.*``
(outside ``count(*)``) or a whole-row reference (``row_to_json(t)``) over a table with PII
columns. Unqualified column names are resolved conservatively against every table the query reads,
so aliases, functions and subqueries cannot smuggle them out.
"""

from __future__ import annotations

from collections import defaultdict

import sqlglot.expressions as exp

from mcp_servers.common.errors import ToolRejected

MASK = "***"


class PiiPolicy:
    def __init__(self, columns: list[str]) -> None:
        self._by_table: dict[tuple[str, str], set[str]] = defaultdict(set)
        for item in columns:
            schema, table, column = item.lower().split(".")
            self._by_table[(schema, table)].add(column)

    def columns_of(self, schema: str, table: str) -> set[str]:
        return set(self._by_table.get((schema.lower(), table.lower()), set()))

    def is_pii(self, schema: str, table: str, column: str) -> bool:
        return column.lower() in self._by_table.get((schema.lower(), table.lower()), set())

    def check_select(self, tree: exp.Expression) -> None:
        tables: dict[str, tuple[str, str]] = {}  # alias or name -> (schema, table)
        read: list[tuple[str, str]] = []
        for table in tree.find_all(exp.Table):
            if not table.db:
                continue  # CTE reference: its own body is checked where it reads real tables
            key = (table.db.lower(), table.name.lower())
            read.append(key)
            tables[table.name.lower()] = key
            if table.alias:
                tables[table.alias.lower()] = key
        restricted = {key: self._by_table[key] for key in read if self._by_table.get(key)}
        if not restricted:
            return
        for table in tree.find_all(exp.Table):
            alias = table.args.get("alias")
            key = ((table.db or "").lower(), table.name.lower())
            if key in restricted and alias is not None and alias.columns:
                # dw.customer AS c(a, b, c, e) would rename email to e.
                raise ToolRejected(
                    f"column alias lists cannot be used on {key[0]}.{key[1]}, which has PII columns"
                )
        pii_names = set().union(*restricted.values())
        for join in tree.find_all(exp.Join):
            using = {str(i.name).lower() for i in join.args.get("using") or []}
            if str(join.args.get("method") or "").upper() == "NATURAL" or using & pii_names:
                # A join on a PII column is a membership oracle even without selecting it.
                raise ToolRejected(
                    "NATURAL joins and USING on PII columns are not allowed with tables that have "
                    "PII columns; join with ON on other columns"
                )
        found: set[str] = set()

        def restrict(keys: list[tuple[str, str]]) -> None:
            for key in keys:
                found.update(f"{key[0]}.{key[1]}.{name}" for name in restricted.get(key, set()))

        restricted_names = {name for name, key in tables.items() if key in restricted}
        for column in tree.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                continue  # handled with the other stars below
            name = column.name.lower()
            qualifier = column.table.lower() if column.table else ""
            if name in restricted_names and qualifier in ("", tables[name][0]):
                # A bare table name or alias is a whole-row value in PostgreSQL (row_to_json(c)).
                restrict([tables[name]])
                continue
            candidates = [tables[qualifier]] if qualifier in tables else list(restricted)
            for key in candidates:
                if name in restricted.get(key, set()):
                    found.add(f"{key[0]}.{key[1]}.{name}")
        for star in tree.find_all(exp.Star):
            parent = star.parent
            if isinstance(parent, exp.Count):
                continue  # count(*) reads no column values
            star_table = ""
            if isinstance(parent, exp.Column) and parent.this is star:
                star_table = parent.table.lower()
            restrict([tables[star_table]] if star_table in tables else list(restricted))
        if found:
            raise ToolRejected(
                "query references PII columns, which cannot be read: "
                + ", ".join(sorted(found))
                + ". Select other columns or aggregate without them."
            )
