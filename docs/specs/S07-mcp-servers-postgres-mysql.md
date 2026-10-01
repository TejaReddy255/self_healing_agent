---
id: S07
title: PostgreSQL and MySQL MCP servers
status: DRAFT
phase: 1
owner_agent: backend-engineer
reviewers: [code-reviewer, security-reviewer]
depends_on: [S06]
blocked_by: []                      # OQ-02 has a default (build in-repo); confirm at refine
traces: [FR-023, NFR-006]
source_sections: ["src §8", "src §33.5 (SOP examples)", "src §35.9 (canonical data)"]
tags: [security-sensitive]
approved_by:
approved_on:
---

# S07 — PostgreSQL and MySQL MCP servers

## Objective
Provide MCP servers for PostgreSQL (8001) and MySQL (8002). Read tools use read-only credentials
and validated SQL. Write tools require WHERE clauses and enforce `max_affected_rows` with rollback.
Also provide seeded dev data for the canonical scenario.

> OQ-02: if existing MCP servers are provided, this slice becomes "adapt and register existing servers".
> The ACs on read-only enforcement, write limits and registry coverage still apply.

## In scope
- `mcp_servers/common/`: FastMCP app factory (streamable HTTP), `/health`, SQL validation (sqlglot:
  single statement; `SELECT` only for reads; reject `INTO`, locking clauses and multiple statements),
  identifier validation, row/byte caps, PII masking of configured columns (`MCP_PII_COLUMNS` config:
  `schema.table.column` list).
- `mcp_servers/postgres/`: read tools `list_schemas, list_tables, describe_table, run_select,
  get_row_count, find_duplicates(table, key_columns), list_partitions(parent_table)`; write tools
  `execute_dml(sql, max_affected_rows)`, `execute_ddl(sql)` (allowlist: `CREATE TABLE … PARTITION OF`,
  `CREATE INDEX`). Two pools: reader role (`default_transaction_read_only=on`) and writer role.
- `mcp_servers/mysql/`: same read set (minus partitions) plus `execute_dml`. Reader user has only SELECT grants.
- `execute_dml`: requires WHERE for UPDATE/DELETE; runs in a transaction; if `rowcount > max_affected_rows`
  it rolls back and returns `{status: "aborted", affected_rows}`.
- `config/tool_permissions.yaml` entries for every tool.
- `deploy/docker-compose.dev.yml`: add MySQL (`sales.customer`) and a target PostgreSQL (`warehouse.dw.customer`),
  plus `deploy/seed/` scripts parameterized by row count. Canonical: 100,000 source rows; target 100,417 with 417 duplicate `customer_id`.
- `python -m mcp_servers.postgres` and `python -m mcp_servers.mysql` entry points.

## Out of scope
S3/MongoDB (S24). Agent usage (S09/S11).

## Configuration
Per server: `*_READER_DSN`, `*_WRITER_DSN` (SecretStr, no defaults), `MCP_ROW_LIMIT=200`,
`MCP_STATEMENT_TIMEOUT_MS=30000`, `MCP_PII_COLUMNS`, `MCP_BIND_HOST=127.0.0.1`.

## Acceptance criteria
- **S07-AC1** `run_select` rejects non-SELECT statements, multiple statements, `SELECT … INTO`, and `FOR UPDATE` (table-driven, ≥ 15 cases).
- **S07-AC2** Read tools execute under the reader role. A write smuggled past validation (e.g. via a volatile function in PG) fails at the database level.
- **S07-AC3** `execute_dml` without WHERE (UPDATE/DELETE) is rejected. Affected rows > `max_affected_rows` → rolled back, and the response reports the count.
- **S07-AC4** On seeded canonical data, `find_duplicates("dw.customer", ["customer_id"])` reports 417 duplicate keys and `get_row_count` returns 100,417 (target) and 100,000 (source).
- **S07-AC5** Row limits and statement timeouts are enforced. Truncated results carry `truncated: true`. Configured PII columns are masked in outputs.
- **S07-AC6** `/health` responds for the S06 probe. Every discovered tool has a registry entry (test: discovered ⊆ registry), with correct `read/write` access.
- **S07-AC7** `execute_ddl` rejects statements outside the allowlist (DROP, ALTER … DROP, TRUNCATE, GRANT).

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC7 | unit | `tests/unit/mcp_servers/test_sql_validation.py` |
| AC2–AC5 | integration (compose DBs) | `tests/integration/mcp_servers/test_postgres.py`, `test_mysql.py` |
| AC6 | integration | `tests/integration/mcp_servers/test_registry_coverage.py` |

## Security and eval considerations
Defence in depth: validation, DB role, row limits, rollback ceiling, registry, write guard (S11). `security-reviewer` checks SQL-injection paths in identifier handling.

## Rollback
Revert; stop the servers.

## Open issues
- From S06 refinement: add a `config/tool_permissions.yaml` entry for every tool, pinning `input_schema_sha256` (SHA-256 of the canonical JSON input schema). Servers must expose `GET /health` at the URL origin and run as `python -m mcp_servers.<name>`, so S06 autostart can launch them in dev.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
