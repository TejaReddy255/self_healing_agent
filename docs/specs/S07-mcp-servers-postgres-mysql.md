---
id: S07
title: PostgreSQL and MySQL MCP servers
status: READY
phase: 1
owner_agent: backend-engineer
reviewers: [code-reviewer, security-reviewer]
depends_on: [S06]
blocked_by: []
traces: [FR-023, NFR-001, NFR-004, NFR-006, NFR-011]
source_sections: ["src §8", "src §33.5 (SOP examples)", "src §35.9 (canonical data)"]
tags: [security-sensitive]
approved_by:
approved_on:
---

# S07 — PostgreSQL and MySQL MCP servers

## Objective
Provide in-repo MCP servers for PostgreSQL (port 8001) and MySQL (port 8002), built with the official MCP Python
SDK (OQ-02: build in-repo). They are the agents' only way to inspect or change data:
- read tools run under read-only database roles with validated SQL, row and byte caps, and PII protection;
- write tools need a WHERE clause and roll back past `max_affected_rows`.

Also provide seeded dev databases for the canonical scenario (src §35.9).

## Read first
- `docs/architecture.md` §8 (MCP), §13 (timeouts), §17 (destructive DML, data leakage), ADR-002 (sqlglot), ADR-005.
- `docs/agent-design.md` §6 (tool matrix).
- S06 (`backend/mcp`): registry pins, `readOnlyHint`, the autostart environment (only `MCP_*` variables), the `/health` probe, and `tools/mcp_pin.py`.
- Use-case NFR-006 and OQ-08 (PII).

## In scope

### Shared server code (`mcp_servers/common/`)
- **`app.py`:** a factory that builds an SDK 2.x `MCPServer` with streamable HTTP and `GET /health` returning `{status, server, db}`. `db` is a reader-pool ping; the HTTP status is 200 even when it fails, so the S06 probe tells "server up, DB down" apart from "down". It binds to `MCP_BIND_HOST`.
- **`sql.py`, read validation (sqlglot, per dialect):**
  - exactly one statement, which must be a `SELECT`, `UNION`/`INTERSECT`/`EXCEPT` of selects, or `WITH … SELECT`;
  - rejected: `INTO`, locking clauses (`FOR UPDATE/SHARE/NO KEY UPDATE/KEY SHARE`, `LOCK IN SHARE MODE`), data-modifying CTEs (`WITH x AS (DELETE … RETURNING …)`), `COPY`/`LOAD`/`HANDLER`/`CALL`/`DO`/`SET`, comments carrying statements, and anything that does not parse;
  - schema references must be within the server's allowed schemas.
- **`sql.py`, DML validation:**
  - exactly one `INSERT`, `UPDATE` or `DELETE` on a table in an allowed schema;
  - `UPDATE`/`DELETE` require a `WHERE` that references at least one column, so `WHERE 1=1` and `WHERE true` are rejected;
  - no multi-statement input, `INTO OUTFILE` or DDL.
- **`sql.py`, DDL allowlist (PostgreSQL only):** `CREATE TABLE <allowed schema>.<name> PARTITION OF <allowed schema>.<parent> FOR VALUES …` and `CREATE INDEX … ON <allowed schema>.<table> (…)`. `CONCURRENTLY` is rejected because it cannot run in a transaction. Everything else is rejected, including `DROP`, `ALTER`, `TRUNCATE`, `GRANT` and `CREATE TABLE AS`.
- **`identifiers.py`:** schema, table and column names must match `^[A-Za-z_][A-Za-z0-9_$]{0,62}$` and **exist** (checked with a parameterized `information_schema` lookup) before being quoted with the dialect's identifier quoting. User values are never concatenated into SQL.
- **`pii.py`** *(decision 2026-10-01)*:
  - `MCP_PII_COLUMNS` is a JSON list of `schema.table.column`.
  - **`run_select` rejects** any query that references a configured PII column anywhere (select list, WHERE, JOIN, ORDER/GROUP BY, function arguments; unqualified names resolved against the referenced tables), or uses `*`/`t.*` on a table with PII columns. The error names the restricted columns so the agent can rephrase.
  - Other tools never return raw PII values: `describe_table` marks PII columns `pii: true`, and the sample keys in `find_duplicates` are masked (`***`) when a key column is PII.
- **`results.py`:** results are JSON text `{columns, rows, row_count, truncated, limit}`.
  - At most `MCP_ROW_LIMIT` rows (default **20**, NFR-006): `limit + 1` are fetched to detect truncation.
  - Values are made JSON-safe: Decimal and dates become strings, bytes become `"<N bytes>"`, and long text is cut at 256 chars.

### PostgreSQL server (`mcp_servers/postgres/`, `python -m mcp_servers.postgres`)
- **Two asyncpg pools:**
  - **reader:** role `mcp_reader`, connections set `default_transaction_read_only=on`, every read runs in `BEGIN READ ONLY`, and grants are SELECT only on allowed schemas;
  - **writer:** role `mcp_writer`, DML on allowed schemas plus `CREATE` on the schema (for allowlisted DDL).
  - Every statement runs with `SET LOCAL statement_timeout = MCP_STATEMENT_TIMEOUT_MS`.
- **Read tools** (`readOnlyHint=true`, `destructiveHint=false`):
  - `list_schemas()` lists the allowed schemas only;
  - `list_tables(schema)`;
  - `describe_table(table)` returns columns, types, nullability, primary or unique keys and `pii` flags;
  - `run_select(sql)`;
  - `get_row_count(table)`;
  - `find_duplicates(table, key_columns)` returns `{duplicate_keys, duplicate_rows, sample: [≤ 5 keys with counts]}`;
  - `list_partitions(parent_table)` returns partition names with their bounds.
- **Write tools** (`readOnlyHint=false`, `destructiveHint=true`):
  - `execute_dml(sql, max_affected_rows)`: runs in a writer transaction. If `rowcount > max_affected_rows` it rolls back and returns `{status: "aborted", affected_rows}`, otherwise it commits and returns `{status: "committed", affected_rows}`. `max_affected_rows` must be 1–`MCP_MAX_AFFECTED_ROWS` (default 10,000).
  - `execute_ddl(sql)`: allowlist only, in a transaction.

### MySQL server (`mcp_servers/mysql/`, `python -m mcp_servers.mysql`)
- **Driver:** **aiomysql**, pure Python. It avoids native DLLs, which Windows Smart App Control blocked for psycopg (ADR-002 amendment).
- **Read tools:** the PostgreSQL set without `list_partitions`. Reads run on the reader user (SELECT-only grants), in `START TRANSACTION READ ONLY`, with `MAX_EXECUTION_TIME` per statement.
- **Write tools:** `execute_dml` with the same semantics. MySQL has no DDL tool.

### Configuration (`MCP_*` only, so S06 autostart passes it to the child process)
| Key | Default | Notes |
|---|---|---|
| `MCP_POSTGRES_READER_DSN` / `MCP_POSTGRES_WRITER_DSN` | — (secret, no default) | Without a writer DSN, the PostgreSQL server exposes no write tools. |
| `MCP_MYSQL_READER_DSN` / `MCP_MYSQL_WRITER_DSN` | — (secret, no default) | Same behaviour for MySQL. |
| `MCP_POSTGRES_SCHEMAS` / `MCP_MYSQL_SCHEMAS` | `["dw"]` / `["sales"]` | Allowed schemas. Any other schema is refused. |
| `MCP_POSTGRES_PORT` / `MCP_MYSQL_PORT` | `8001` / `8002` | src §8 |
| `MCP_BIND_HOST` | `127.0.0.1` | |
| `MCP_ROW_LIMIT` | `20` | NFR-006 (max 200) |
| `MCP_STATEMENT_TIMEOUT_MS` | `30000` | |
| `MCP_MAX_AFFECTED_ROWS` | `10000` | Server ceiling for `max_affected_rows` |
| `MCP_PII_COLUMNS` | `[]` | e.g. `["dw.customer.email","dw.customer.phone","sales.customer.email","sales.customer.phone"]` |

DSNs are `SecretStr`. They are never logged or echoed, and errors are generic.

### Registry
`config/tool_permissions.yaml` gets one entry per tool, generated with `uv run python tools/mcp_pin.py <url> <server>`. Each entry carries a human-chosen `access` and the `definition_sha256` pin. Server `postgresql` → `mcp_servers.postgres`, `mysql` → `mcp_servers.mysql`; the S06 default module setting is used.

### Dev data (`deploy/`)
- **Containers in `deploy/docker-compose.dev.yml`:**
  - `sales-db`: `mysql:8.4`, database `sales`, host port `${MYSQL_PORT:-53306}`;
  - `warehouse-db`: `postgres:16.4-alpine`, database `warehouse`, host port `${WAREHOUSE_PG_PORT:-55433}`.
- **Credentials:** all come from `.env` (no defaults):
  - root and admin: `MYSQL_ROOT_PASSWORD`, `WAREHOUSE_PG_PASSWORD`;
  - reader and writer: `MCP_*_READER_PASSWORD` and `MCP_*_WRITER_PASSWORD`, used by init scripts that create the roles and grants.
- **Seeding:** `deploy/seed/canonical.py --rows 100000 --duplicates 417 [--seed 7]` is deterministic and idempotent: it drops and recreates the tables.
  - `sales.customer`: `customer_id` PK, name, email, phone, country, `updated_at`; 100,000 rows.
  - `dw.customer`: the same columns plus `load_id` identity and `loaded_at`, with no unique constraint on `customer_id`; 100,417 rows, 417 of them duplicate keys.
  - `dw.sales_daily`: range-partitioned by `sale_date` with two monthly partitions, used by `list_partitions` and the missing-partition SOP.
  - Values are synthetic (no real people), and `email`/`phone` are listed in `MCP_PII_COLUMNS`.

## Out of scope
- S3 and MongoDB servers (S24); agent usage (S09, S11); the write guard (S11).
- Authenticating MCP clients to servers (loopback-only binding in v1; S23).
- Generating `MCP_PII_COLUMNS` from metadata `pii_columns` (S23/S25).

## Public contracts
- The tool names, argument schemas, annotations and JSON result shapes listed above. They are pinned in the registry, and any change is a deliberate re-pin.
- `/health`.

## Persistence and migrations
None in the operational database. The dev data containers are created by init scripts and the seed script.

## Acceptance criteria
- **S07-AC1** `run_select` rejects non-SELECT statements, multiple statements, `SELECT … INTO`, locking clauses, data-modifying CTEs, `COPY`/`CALL`/`SET`/`DO`, unparseable input and schemas outside the allowlist. Table-driven, ≥ 20 cases per dialect.
- **S07-AC2** Read tools run under the reader role in a read-only transaction. A write smuggled past validation (e.g. PostgreSQL `SELECT` calling a volatile function that inserts, or `nextval`) fails at the database level, and nothing changes.
- **S07-AC3** `execute_dml` rejects UPDATE/DELETE without WHERE and with column-free WHERE (`1=1`, `true`). When affected rows exceed `max_affected_rows`, it rolls back (the row count is unchanged afterwards) and reports `aborted` with the count. A `max_affected_rows` outside 1–`MCP_MAX_AFFECTED_ROWS` is rejected.
- **S07-AC4** On the seeded canonical data, `find_duplicates("dw.customer", ["customer_id"])` reports 417 duplicate keys, and `get_row_count` returns 100,417 (target) and 100,000 (source).
- **S07-AC5** Results are capped at `MCP_ROW_LIMIT` rows with `truncated: true`, and statement timeouts are enforced (a `pg_sleep`/`SLEEP` beyond the limit fails with a timeout error).
- **S07-AC6** `/health` answers for the S06 probe. Every discovered tool has a registry entry with the correct access: discovered = registered, with both servers healthy and classified READ/WRITE (no UNCLASSIFIED, MISMATCH or SCHEMA_CHANGED) by the real S06 manager.
- **S07-AC7** `execute_ddl` rejects statements outside the allowlist (`DROP`, `ALTER … DROP`, `TRUNCATE`, `GRANT`, `CREATE TABLE AS`, `CREATE INDEX CONCURRENTLY`, objects outside allowed schemas).
- **S07-AC8** *(new, decision 2026-10-01)*
  - `run_select` rejects every query referencing a configured PII column: direct, qualified, aliased table, in WHERE/JOIN/ORDER BY, inside functions, `*` and `t.*`. It accepts queries that avoid PII columns.
  - `describe_table` flags PII columns.
  - `find_duplicates` masks sample keys of PII key columns.
  - No tool output contains a seeded PII value.
- **S07-AC9** *(new)* Identifiers: tool arguments naming schemas, tables or columns must match the identifier pattern and exist. Injection attempts (`customer; DROP TABLE x`, `"a"."b"--`, backticks, unicode quotes) are rejected before any SQL runs.
- **S07-AC10** *(new)* Secrets and errors: DSNs and passwords never appear in tool output, errors, `/health` or logs. Database error text returned to the caller is reduced to a category plus a sanitized message (no connection details).
- **S07-AC11** *(new)* Without a writer DSN, a server exposes no write tools, and S06 then raises `NoWriteTools` for that target.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC7, AC8 (validation), AC9 (patterns) | unit | `tests/unit/mcp_servers/test_sql_validation.py`, `test_pii.py`, `test_identifiers.py` |
| AC2–AC5, AC8 (outputs), AC9 (existence), AC10, AC11 | integration (compose `sales-db`, `warehouse-db`) | `tests/integration/mcp_servers/test_postgres.py`, `test_mysql.py` |
| AC6 | integration | `tests/integration/mcp_servers/test_registry_coverage.py` (real S06 manager plus both servers) |

The integration tests skip locally when the data containers are not running, and fail in CI (`REQUIRE_INTEGRATION_DB=1`). The CI integration job gains MySQL and warehouse PostgreSQL services.

## Runtime verification
1. Start the containers, seed the data, and start both servers.
2. Call `/mcp-status` from the API with `MCP_AUTOSTART=true`: both servers healthy, all tools READ/WRITE, nothing unclassified.
3. Run `find_duplicates` and `get_row_count` through S06 `ToolHandle`s and check the audit rows.

## Security and eval considerations
**Defence in depth:**
- sqlglot validation and allowlisted schemas;
- identifiers checked for existence and quoted;
- PII references rejected;
- the reader role in a read-only transaction;
- statement timeouts, row and byte caps;
- the rollback ceiling;
- registry pins and annotations (S06), then the write guard (S11).

`security-reviewer` checks SQL injection in identifier handling, validator bypasses (dialect quirks, comments, nested statements) and leakage paths.

## Rollback
Revert the code, stop the servers, and `docker compose down` the data containers (their volumes hold only seeded data).

## Open issues
- **OQ-02 (reuse or build):** decided as build in-repo.
- **Decided 2026-10-01 (product owner):** `run_select` rejects PII references (AC8).
- **Spec values changed:**
  - `MCP_ROW_LIMIT` defaults to **20** (use-case NFR-006), replacing the draft's 200. The use-case spec outranks a draft value.
  - The draft's `*_READER_DSN` names became `MCP_*` names, so S06 autostart passes them through.
- **Downstream:**
  - **S09:** plan assertions and N5 use `find_duplicates`/`get_row_count` semantics as defined here.
  - **S11:** relies on `execute_dml`'s `aborted`/`committed` status.
  - **S19:** maps assertion kinds to these tools.
  - **S23:** MCP client authentication, and generating `MCP_PII_COLUMNS` from metadata.
- **From S06:** registry pins (`definition_sha256`), annotations, `MCP_*` names and DB-level read-only enforcement are all covered above.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
- 2026-10-01 — refined via `/slice-spec`.
  - Concrete validation rules per dialect, identifier existence checks, PII policy (reject references), result shapes and caps, roles and read-only transactions.
  - The aiomysql driver, `MCP_*` configuration, registry generation via `mcp_pin`, and dev containers with deterministic seeding.
  - AC1–AC7 sharpened (IDs kept); AC8–AC11 added.
  - Product owner decided that `run_select` rejects PII references.
  - Status READY.
