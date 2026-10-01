---
id: S07
title: PostgreSQL and MySQL MCP servers
status: DONE
phase: 1
owner_agent: backend-engineer
reviewers: [code-reviewer, security-reviewer]
depends_on: [S06]
blocked_by: []
traces: [FR-023, NFR-001, NFR-004, NFR-006, NFR-011]
source_sections: ["src §8", "src §33.5 (SOP examples)", "src §35.9 (canonical data)"]
tags: [security-sensitive]
approved_by: product owner
approved_on: 2026-10-01
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
- **`app.py`:** a factory that builds an SDK 2.x `MCPServer` with streamable HTTP and `GET /health` returning `{status, server, db}`. `db` is a reader-pool ping; the HTTP status is 200 even when it fails, so the S06 probe tells "server up, DB down" apart from "down". It binds to `MCP_BIND_HOST`, which must be a loopback address until client authentication (S23). The `db` ping is bounded at 1.5 s, below the S06 probe timeout.
- **`sql.py`, read validation (sqlglot, per dialect):**
  - exactly one statement, which must be a `SELECT`, `UNION`/`INTERSECT`/`EXCEPT` of selects, or `WITH … SELECT`;
  - rejected: `INTO`, locking clauses (`FOR UPDATE/SHARE/NO KEY UPDATE/KEY SHARE`, `LOCK IN SHARE MODE`), data-modifying CTEs (`WITH x AS (DELETE … RETURNING …)`), `COPY`/`LOAD`/`HANDLER`/`CALL`/`DO`/`SET`, comments carrying statements, and anything that does not parse;
  - schema references must be within the server's allowed schemas; an unqualified name counts as a CTE only where SQL scoping makes that CTE visible (so `WITH pg_stats AS (SELECT … FROM pg_stats)` is refused);
  - functions are **allowlisted** (aggregates, window, string, numeric, date/time and a few set-returning helpers). Quoting a name does not disguise it. Functions that run SQL passed as a string (`*_to_xml*`, `ts_stat`, `ts_rewrite`) are refused, and on PostgreSQL `EXECUTE` on them is also revoked from `PUBLIC` (`deploy/init/warehouse/02-revoke-functions.sh`);
  - the server executes SQL regenerated from the validated tree, with comments dropped.
- **`sql.py`, DML validation:**
  - exactly one `INSERT`, `UPDATE` or `DELETE` on a table in an allowed schema;
  - `UPDATE`/`DELETE` require a `WHERE` that references at least one column, so `WHERE 1=1` and `WHERE true` are rejected. This is a guard against obvious mistakes, not a proof of selectivity (`WHERE id = id` passes): the `max_affected_rows` ceiling and the S11 write guard are the real backstops;
  - no multi-statement input, `INTO OUTFILE` or DDL.
- **`sql.py`, DDL allowlist (PostgreSQL only):** `CREATE TABLE <allowed schema>.<name> PARTITION OF <allowed schema>.<parent> FOR VALUES …` and `CREATE INDEX … ON <allowed schema>.<table> (…)`. `CONCURRENTLY` is rejected because it cannot run in a transaction. Everything else is rejected, including `DROP`, `ALTER`, `TRUNCATE`, `GRANT` and `CREATE TABLE AS`. The function allowlist applies to DDL too, because DDL runs as `dw_owner`.
- **`identifiers.py`:** schema, table and column names must match `^[A-Za-z_][A-Za-z0-9_$]{0,62}$` and **exist** (checked with a parameterized `information_schema` lookup) before being quoted with the dialect's identifier quoting. User values are never concatenated into SQL.
- **`pii.py`** *(decision 2026-10-01)*:
  - `MCP_PII_COLUMNS` is a JSON list of `schema.table.column`.
  - **`run_select` rejects** any query that references a configured PII column anywhere (select list, WHERE, JOIN, ORDER/GROUP BY, function arguments; unqualified names resolved against the referenced tables), or uses `*`/`t.*` (outside `count(*)`), a whole-row reference (`row_to_json(t)`), a column alias list (`AS t(a, b, …)`), a `NATURAL` join, or `USING` on a PII column with a table that has PII columns. The error names the restricted columns so the agent can rephrase.
  - Other tools never return raw PII values: `describe_table` marks PII columns `pii: true`, and the sample keys in `find_duplicates` are masked (`***`) when a key column is PII.
- **`results.py`:** results are JSON text `{columns, rows, row_count, truncated, limit}`.
  - At most `MCP_ROW_LIMIT` rows (default **20**, NFR-006): `limit + 1` are fetched to detect truncation. PostgreSQL fetches through a cursor; MySQL puts `LIMIT limit + 1` into the query, because its driver buffers whole result sets.
  - Values are made JSON-safe: Decimal and dates become strings, bytes become `"<N bytes>"`, and long text is cut at 256 chars.

### PostgreSQL server (`mcp_servers/postgres/`, `python -m mcp_servers.postgres`)
- **Two asyncpg pools:**
  - **reader:** role `mcp_reader`, connections set `default_transaction_read_only=on`, every read runs in `BEGIN READ ONLY`, and grants are SELECT only on allowed schemas;
  - **writer:** role `mcp_writer`, DML on allowed schemas. It is a `NOINHERIT` member of `dw_owner`, the schema owner, and only `execute_ddl` uses that membership (`SET LOCAL ROLE dw_owner`), so DML never has owner rights.
  - Connections pin `standard_conforming_strings=on`.
  - Every statement runs with `SET LOCAL statement_timeout = MCP_STATEMENT_TIMEOUT_MS`.
- **Read tools** (`readOnlyHint=true`, `destructiveHint=false`):
  - `list_schemas()` lists the allowed schemas only;
  - `list_tables(schema)`;
  - `describe_table(table)` returns columns, types, nullability, primary or unique keys and `pii` flags;
  - `run_select(sql)`;
  - `get_row_count(table)`;
  - `find_duplicates(table, key_columns)` returns `{table, key_columns, duplicate_keys, duplicate_rows, excess_rows, sample: [≤ 5 keys with counts]}`. `duplicate_keys` counts key values that occur more than once, `duplicate_rows` counts the rows carrying them, and `excess_rows` counts rows beyond one per key (what a de-duplication would remove). For the canonical data these are 417, 834 and 417;
  - `list_partitions(parent_table)` returns partition names with their bounds.
- **Write tools** (`readOnlyHint=false`, `destructiveHint=true`):
  - `execute_dml(sql, max_affected_rows)`: runs in a writer transaction. If `rowcount > max_affected_rows` it rolls back and returns `{status: "aborted", affected_rows}`, otherwise it commits and returns `{status: "committed", affected_rows}`. `max_affected_rows` must be 1–`MCP_MAX_AFFECTED_ROWS` (default 10,000).
  - `execute_ddl(sql)`: allowlist only, in a transaction.

### MySQL server (`mcp_servers/mysql/`, `python -m mcp_servers.mysql`)
- **Driver:** **aiomysql**, pure Python. It avoids native DLLs, which Windows Smart App Control blocked for psycopg (ADR-002 amendment).
- **Read tools:** the PostgreSQL set without `list_partitions`. Reads run on the reader user (SELECT-only grants), in `START TRANSACTION READ ONLY`, with `MAX_EXECUTION_TIME` per statement.
- **Write tools:** `execute_dml` with the same semantics. `affected_rows` counts matched rows (`CLIENT_FOUND_ROWS`), so the ceiling stays conservative. MySQL has no DDL tool.
- **Connections:**
  - no default database, so every table must be schema-qualified;
  - multi-statements switched off per connection;
  - `sql_mode` pinned;
  - closed rather than reused after any error, timeout or cancellation, because `START TRANSACTION` implicitly commits an open transaction.
- **Write timeouts:** a DML statement past the timeout is stopped with `KILL QUERY` and reports "nothing was committed". A commit interrupted on the client side reports "commit outcome unknown" (invariant 6).

### Configuration (`MCP_*` only, so S06 autostart passes it to the child process)
| Key | Default | Notes |
|---|---|---|
| `MCP_POSTGRES_READER_DSN` / `MCP_POSTGRES_WRITER_DSN` | — (secret, no default) | Without a writer DSN, the PostgreSQL server exposes no write tools. |
| `MCP_MYSQL_READER_DSN` / `MCP_MYSQL_WRITER_DSN` | — (secret, no default) | Same behaviour for MySQL. |
| `MCP_POSTGRES_SCHEMAS` / `MCP_MYSQL_SCHEMAS` | `["dw"]` / `["sales"]` | Allowed schemas. Any other schema is refused. |
| `MCP_POSTGRES_PORT` / `MCP_MYSQL_PORT` | `8001` / `8002` | src §8 |
| `MCP_BIND_HOST` | `127.0.0.1` | Loopback only (`127.0.0.1`, `::1`, `localhost`) until S23 |
| `MCP_ROW_LIMIT` | `20` | NFR-006 (max 200) |
| `MCP_STATEMENT_TIMEOUT_MS` | `30000` | |
| `MCP_MAX_AFFECTED_ROWS` | `10000` | Server ceiling for `max_affected_rows` |
| `MCP_PII_COLUMNS` | `[]` | e.g. `["dw.customer.email","dw.customer.phone","sales.customer.email","sales.customer.phone"]` |

DSNs are `SecretStr`. They are never logged or echoed, and errors are generic.

### Registry
`config/tool_permissions.yaml` gets one entry per tool, generated with `uv run python tools/mcp_pin.py <url> <server>`. Each entry carries a human-chosen `access` and the `definition_sha256` pin. Server `postgresql` → `mcp_servers.postgres`, `mysql` → `mcp_servers.mysql`; the S06 default module setting is used.

### Dev data (`deploy/`)
- **Containers in `deploy/docker-compose.dev.yml`:**
  - `sales-db`: `mysql:8.4`, database `sales`, host port `${MYSQL_PORT:-13306}` (53306 falls in a Windows excluded port range);
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
- **S07-AC5** Results are capped at `MCP_ROW_LIMIT` rows with `truncated: true`, and statement timeouts are enforced. On PostgreSQL, a `pg_sleep` beyond the limit fails with a timeout error. On MySQL, a query running past `MAX_EXECUTION_TIME` fails with a timeout error, and `SLEEP()` is ended early (it returns 1, which is MySQL behaviour). *(Wording amended 2026-10-01, product owner.)*
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
| Lint / format | `uv run ruff check .` / `uv run ruff format --check .` | pass | |
| Types | `uv run mypy backend frontend tools mcp_servers` | pass | strict, 109 files. CI gains a separate `uv run mypy mcp_servers` step, so S01's command list is unchanged. |
| Unit + contract | `uv run pytest -m "not integration and not e2e and not live"` | 965 passed, 1 skipped | Covers validation (≥ 20 rejection cases per dialect), the PII policy including the review bypasses, identifiers (with a hypothesis property), settings, results, the factory and `/health`. Tool definitions are hashed against the committed registry pins. The skip is the S04 Windows symlink test. |
| Integration | `uv run pytest -m integration` | 115 passed (68 for S07) | Real `sales-db` and `warehouse-db`, re-seeded once per session. Write tests use scratch tables only. `test_registry_coverage.py` runs both servers plus a reader-only instance as subprocesses against the real S06 manager and the committed registry. |
| Traceability | `uv run python tools/trace.py --strict` | pass | S07-AC1..AC11 covered |
| Seed | `uv run python deploy/seed/canonical.py` (run twice) | pass | `dw.customer` holds 100,417 rows over 100,000 distinct keys; `sales.customer` holds 100,000 rows; all `dw` objects are owned by `dw_owner`. |
| Runtime | API with `MCP_AUTOSTART=true`, then `GET /mcp-status` | pass | Both servers healthy. postgresql: 7 read, 2 write; mysql: 6 read, 1 write; no unclassified, mismatched or schema-changed tools. `find_duplicates` and `get_row_count` through S06 `ToolHandle`s write a STARTED row and an outcome row per call, with no PII. |
| Reviews | code-reviewer, security-reviewer | findings addressed | Dispositions below. |

**Found while verifying.** S06 autostart's minimal child environment had no login name, and aiomysql calls `getpass.getuser()` at import, which has no fallback on Windows. The MySQL server therefore crashed on autostart. The fix adds `USERNAME`/`USER`/`LOGNAME` to the S06 allowlist. That is OS basics, as the S06 spec already promises, so no S06 AC changes.

**Review dispositions (2026-10-01).**
- **Fixed:**
  - SQL-in-a-string functions (`table_to_xml`, `query_to_xml_and_xmlschema`, `ts_stat`, …) bypassed the table and PII checks (security H1, code H2). The deny list became a function allowlist, and `EXECUTE` on those functions is revoked from `PUBLIC`.
  - A CTE name shadowed real tables outside its scope (security H2). Fixed with scope-aware resolution via sqlglot, and MySQL now connects without a default database.
  - Column alias lists renamed PII columns (security H3, code H1).
  - Quoted function names bypassed the deny list, and DDL was not checked (security M1, code L7).
  - MySQL buffered whole result sets (security M2, code M4). The row cap is now part of the query.
  - `MCP_BIND_HOST` was unvalidated (security M3). It is now loopback only until S23.
  - MySQL write timeouts and commit semantics (security M4, code M5). Fixed with `KILL QUERY`, and an interrupted commit reports "outcome unknown".
  - The `find_duplicates` result did not match the contract (code M3). It now carries `duplicate_rows`, plus `excess_rows`, documented above.
  - A `USING`/`NATURAL` join acted as a membership oracle (security L1).
  - `sql_mode` and `standard_conforming_strings` were not pinned (security L8).
  - The secret-leak tests did not check decoded passwords (security L6).
  - `/health` could exceed the S06 probe budget (code).
  - Registry pins were checked only in integration (code L10).
  - A key column could collide with the count alias, and MySQL composite unique keys were missing (code nits).
  - Spec drift: the MySQL port, the writer's privileges, AC5 wording (product owner) and the WHERE-rule intent (security L3/L7, code L8/L9).
- **Accepted as-is:**
  - No pool `close()` at shutdown (code L6): MCPServer's lifespan is per session, so the process exit closes the connections.
  - Duplication between the two tools classes (code nit): the result shape and SQL are shared in `common/duplicates.py`; the rest differs by driver.
  - PII moved by approved writes, partitions or views (security L2): writes need approval, and S11 adds the write guard; noted for S23/S25 metadata-driven PII.
  - FK cascade and trigger rows not counted against the ceiling (security L3): there are none in the dev schemas; S11 should re-verify.
  - DEFAULT partitions and index locks (security L4): S11/S19 SOP review.
  - Dev init scripts interpolate passwords and pass `-p` on the command line (security L5, code nit): dev containers only; the CI values are throwaway.
  - The aiomysql acquire-timeout leak (code, plausible): bounded by `maxsize=4`; revisit with S23 metrics.

## Changelog
- 2026-09-29 — created (DRAFT)
- 2026-10-01 — refined via `/slice-spec`.
  - Concrete validation rules per dialect, identifier existence checks, PII policy (reject references), result shapes and caps, roles and read-only transactions.
  - The aiomysql driver, `MCP_*` configuration, registry generation via `mcp_pin`, and dev containers with deterministic seeding.
  - AC1–AC7 sharpened (IDs kept); AC8–AC11 added.
  - Product owner decided that `run_select` rejects PII references.
  - Status READY.
- 2026-10-01 — approved by product owner ("approve S07 and implement it"). Status IN_PROGRESS.
- 2026-10-01 — implemented. Review findings fixed (dispositions above). The product owner amended AC5's wording (MySQL `SLEEP()` ends early instead of failing). Contract details recorded: the `find_duplicates` fields, the writer role model, the function allowlist, loopback binding and the MySQL port. Status IN_REVIEW.
- 2026-10-01 — accepted by product owner. Status DONE; merged to `main`.
