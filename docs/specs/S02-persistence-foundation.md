---
id: S02
title: Persistence foundation, migrations, audit and activity
status: IN_REVIEW
phase: 0
owner_agent: data-engineer
reviewers: [code-reviewer, data-engineer]
depends_on: [S01]
blocked_by: []
traces: [FR-110, NFR-001, NFR-040]
source_sections: ["src §0.1-8", "src §5.1", "src §11", "src §12 (/activities, /ws/activities)", "src §35.8"]
tags: [schema]
approved_by: product owner
approved_on: 2026-09-30
---

# S02 — Persistence foundation, migrations, audit and activity

## Objective
Provide the async PostgreSQL access layer, the numbered-migration runner (ADR-003), and the
cross-cutting audit, activity and counter persistence that every later slice writes to.

## Read first
`docs/architecture.md` §8, §9, §14, ADR-003; S01 evidence.

## In scope
- `backend/db/pool.py`: asyncpg pool (ADR-002 amended) built from `DatabaseSettings` (`PG_HOST`, `PG_PORT`,
  `PG_DATABASE`, `PG_USER`, `PG_PASSWORD: SecretStr`, `PG_POOL_MAX`, `PG_STATEMENT_TIMEOUT_MS`), with `connection()`
  and `transaction()` helpers. The pool is lazy (`min_size=0`), so the API starts while the DB is down.
- `backend/db/migrate.py`: CLI `python -m backend.db.migrate up|status|verify`. Creates
  `schema_migrations(version int PK, name text, checksum text, applied_at timestamptz)`. Applies
  `migrations/NNNN_*.sql` in order, each in its own transaction, under `pg_advisory_lock`. `verify`
  recomputes checksums and fails on drift.
- Migration: `operation_audit` (id bigserial, occurred_at, phase, event_type, operation, tool_name,
  server, status, duration_ms, actor_subject, actor_role, request_id, pipeline_id, dag_id, run_id,
  task_id, analysis_id, review_id, job_id, incident_id, input jsonb, output jsonb, details jsonb) with
  indexes on (review_id), (incident_id), (analysis_id), (job_id), (occurred_at); `activities`
  (id, created_at, type, details jsonb, incident_id, pipeline_id); `agent_performance` (single row id=1:
  completed_analyses, tool_executions, issues_detected, total_activities).
- `backend/repositories/audit_repository.py`, `activity_repository.py`, `performance_repository.py`.
- `backend/services/audit_service.py`: `record(event: AuditEvent)`, which redacts via the S01 filter
  logic and truncates `input/output/details` beyond `AUDIT_MAX_PAYLOAD_BYTES` with a `"_truncated": true` marker.
- `backend/services/activity_service.py` + an in-process broadcaster. `GET /activities?limit&before_id`
  (viewer); WebSocket `/ws/activities` (auth via token query param or subprotocol header), which pushes activities
  persisted after connection.
- Readiness checks `db` (SELECT 1) and `migrations` (pending count = 0), registered into S01's registry.
- The lifespan opens and closes the pool. **The app never executes DDL.**
- Extend `deploy/docker-compose.dev.yml` if needed. Integration test fixture: a fresh schema per test session.

## Out of scope
Domain tables (added by their slices); retention purge (S25); cross-process activity fan-out (single
API instance in v1; noted in S23).

## Public contracts
- `AuditEvent` model (fields mirror the table; `input/output/details: dict[str, Any] | None`).
- `AuditService.record(event, *, conn=None) -> int` (pass `conn` to write inside the caller's transaction);
  `ActivityService.publish(type, details, **ids) -> Activity`.
- `GET /activities` → `list[Activity]`, `limit` 1..500 (default 50). 503 `DATABASE_UNAVAILABLE` when the
  database is not configured **or unreachable**.

## Persistence and migrations
- `0001_audit_activity_performance`: `operation_audit`, `activities`, `agent_performance` (seed row id=1).
- `0002_audit_append_only_and_indexes` (review follow-up): server-stamped `recorded_at`; `operation_audit`
  is append-only via triggers (UPDATE/TRUNCATE rejected; DELETE only when `recorded_at` is older than 13
  months); correlation indexes are partial `(id, occurred_at) WHERE id IS NOT NULL`.
- Correlation-ID columns are nullable and have no FK. **No FKs will be added on `operation_audit`** (it must
  record events for entities whose creation rolled back, and FKs would conflict with the append-only
  triggers). S15 adds FKs on the other tables only.

## Configuration
`PG_*`, `PG_POOL_MAX=10`, `PG_STATEMENT_TIMEOUT_MS=30000`, `AUDIT_MAX_PAYLOAD_BYTES=65536`. There is no
`PG_POOL_MIN`: the pool is always lazy. The dev compose host port defaults to `55432` (`PG_PORT`) because a
locally installed PostgreSQL often owns 5432. In `ENV=prod`, `PG_PASSWORD` is required (fail fast); in
dev/test, without it persistence is disabled and `/activities` returns 503 `DATABASE_UNAVAILABLE`.

## Contract decisions made during implementation (2026-09-30)
- **Entity IDs are UUIDs** across the platform (`analysis_id`, `review_id`, `job_id`, `incident_id`), so the
  S15 foreign keys line up.
- **WebSocket auth** takes the token from `Authorization: Bearer …` or subprotocols `["bearer", "<token>"]`
  (the server replies with the `bearer` subprotocol). **Query-string tokens are not supported**, because they leak
  into proxy and access logs. Human roles viewer and above; the `service` role is rejected (close code 1008).
- Activity `details` are redacted and size-capped by the same `prepare_payload` used for audit.
- `publish` notifies subscribers only after the transaction commits. A slow subscriber drops its oldest
  queued item (queue of 100) instead of blocking.
- The migration CLI takes `--dir` (used by tests and later by the S26 DQ service). Connection settings
  come only from `PG_*` variables, never from command-line arguments.
- **Migration connections** use their own profile: no statement timeout, `lock_timeout` =
  `PG_MIGRATION_LOCK_TIMEOUT_MS` (default 5 s) for DDL. The runner-vs-runner advisory lock wait is
  unbounded. Out-of-order pending versions (below the highest applied) are rejected.
- **Audit payloads are JSONB-safe:** NaN/±Infinity are stored as strings and NUL characters are stripped
  (payloads and text columns). `occurred_at` must be timezone-aware and not more than 5 minutes in the future.
- **Activity feed ordering contract (for S12/S21):** delivery order can differ from id order under
  concurrent publishes. There is no replay from an id. A client must **subscribe to the WebSocket first, then
  fetch `GET /activities`, and de-duplicate by `id`**. A slow client can miss items (drop-oldest); a client
  that cannot receive for 10 s is closed with code 1013 and should reconnect and re-fetch.

## Acceptance criteria
- **S02-AC1** `migrate up` on an empty DB applies all migrations. A second run is a no-op. `status` lists applied and pending migrations.
- **S02-AC2** If an already-applied migration file is modified, `up` and `verify` fail with a checksum error and apply nothing.
- **S02-AC3** Two concurrent `migrate up` processes result in each migration applied exactly once.
- **S02-AC4** API startup executes no DDL (asserted by a PostgreSQL event trigger that records every DDL command during startup and readiness checks). `/health/ready` reports `db` and `migrations` component status and returns 503 when the DB is down or migrations are pending.
- **S02-AC5** `AuditService.record` persists all correlation columns, redacts sensitive keys, and truncates oversized payloads with a marker.
- **S02-AC6** A WebSocket subscriber receives a newly published activity within 2 s. A disconnected client does not break publishing.
- **S02-AC7** `GET /activities` enforces the limit bounds and keyset pagination. A viewer can call it; an unauthenticated caller gets 401.
- **S02-AC8** `agent_performance` increments are atomic under 50 concurrent increments.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1–AC3 | unit + integration | `tests/unit/test_migration_files.py`, `tests/integration/test_migrate.py` |
| AC4 | integration | `tests/integration/test_startup_no_ddl.py` |
| AC5 | unit + integration | `tests/unit/test_audit_redaction.py`, `tests/integration/test_audit_repo.py` |
| AC6, AC7 | integration | `tests/integration/test_activities.py` |
| AC8 | integration | `tests/integration/test_performance_counters.py` |

## Runtime verification
`docker compose -f deploy/docker-compose.dev.yml up -d`, `migrate up`, start the API, open a WS client, publish an activity via a test route or service call.

## Security and eval considerations
The audit table is a data-leak surface: redaction must be applied in the service, not by callers.

## Rollback
Drop the created tables in a new down-migration only in non-prod. In prod: revert code; tables are additive.

## Open issues
Deferred from the S02 reviews (recorded in the target specs):
- **S15:** add no FK on `operation_audit`; put `ADD CONSTRAINT … NOT VALID` and `VALIDATE CONSTRAINT` in
  **separate** migration files (the runner wraps each file in one transaction); index `activities.incident_id`.
- **S26:** migration tracking per schema (`--schema`, schema-qualified tracking table) before the DQ service
  runs its own migrations against the same database.
- **S25:** partitioning trigger for `operation_audit` (e.g. more than ~50M rows or a purge window over 5 min);
  activities retention period; a `-- migrate:no-transaction` directive for `CREATE INDEX CONCURRENTLY`;
  separate owner and runtime DB roles (a trigger does not stop a superuser).
- **S23:** cross-process activity fan-out (LISTEN/NOTIFY).
- **Later:** CHECK constraints for `phase`/`status` once the vocabulary settles (S03/S10).

## Verification evidence
Run on 2026-09-30, Windows 11 (Smart App Control enforcing), Python 3.12.10, Docker 29.8.1, PostgreSQL
16.4 container on 127.0.0.1:55432, branch `slice/S02-persistence-foundation`.

| Check | Command | Result | Evidence / notes |
|---|---|---|---|
| Lint / format / types | `ruff check .`, `ruff format --check .`, `mypy backend frontend tools` | PASS | strict mypy: no issues in 47 source files |
| Unit tests | `uv run pytest -m "not integration and not e2e and not live"` | PASS | 100 passed (S01 suite + S02 unit: payload redaction/truncation, broadcaster, prod requires DB) |
| Integration tests | `uv run pytest -m integration` (real Postgres) | PASS | 31 passed in ~10 s; each test gets its own throwaway database; 0 leftover `sh_test_*` databases |
| Full suite (first pass) | `uv run pytest` | PASS | 133 passed |
| Traceability | `uv run python tools/trace.py --strict` | PASS | S02-AC1…AC8 each covered by ≥ 1 test |
| Migration CLI | `python -m backend.db.migrate status → up → verify → up → status` on the dev DB | PASS | `not initialised / pending 0001` → `applied 0001_audit_activity_performance` → `ok: 1 applied, 0 pending, no drift` → `nothing to apply` → `applied 0001 … <timestamp>` |
| Real server + WebSocket | scratch script: uvicorn.Server on :8767 + `websockets` client, default `ProactorEventLoop` | PASS | `/health/ready` 200 with `db: ok`, `migrations: ok (1 applied)`; WS subprotocol `bearer`; published activity received in 11 ms; `/activities` returned it; WS without a token rejected with HTTP 403 |
| No DDL at startup | catalog snapshot before/after app start on an empty DB | PASS | identical `(0, '')` snapshot; readiness 503 `migrations: not initialised` (`test_startup_no_ddl.py`) |
| Code review | `code-reviewer` agent on 09ec309 | PASS (no High) | 3 Medium + 7 Low. Fixed: DB-down `/activities` now 503 `DATABASE_UNAVAILABLE` without traceback; migrations use their own timeout profile (no statement timeout, 5 s DDL lock timeout, unbounded runner wait); no-DDL check replaced by an event trigger that records any DDL (with a self-test); WS handler cancels its helper tasks, sends time out after 10 s (close 1013), narrower exceptions; lock taken before `try`, unlock errors cannot mask failures; out-of-order versions rejected; BOM-tolerant checksums plus `.gitattributes`; `occurred_at` must be timezone-aware; more env isolation. Documented: activity ordering contract. Deferred: `--schema` to S26 |
| Data-engineer review | `data-engineer` agent on 09ec309 | PASS after fix (1 High) | H1 fixed: NaN/Infinity/NUL no longer break audit writes (unit and integration tests). M1 `record(conn=...)`; M2 as above; M3 append-only triggers plus server-stamped `recorded_at` (also back-dating-proof on INSERT); M5 partial composite indexes; L2, L7 fixed; M4, L1, L3–L6, L8 recorded in S15/S25/S26 and Open issues |
| Re-run after fixes | lint, format, mypy, `pytest` (unit + integration), trace | PASS | 149 passed in ~21 s; mypy strict clean; `trace.py --strict` OK |
| Dev DB upgrade | `python -m backend.db.migrate status → up → verify` | PASS | `pending 0002` → `applied 0002_audit_append_only_and_indexes` → `ok: 2 applied, 0 pending, no drift`; 4 audit triggers present |
| Runtime after fixes | real uvicorn + websockets check | PASS | readiness `migrations: ok (2 applied)`; WS delivery 23 ms; no-token WS rejected with 403 |
| CI integration job | `.github/workflows/ci.yml` job `integration` (postgres:16.4 service, `REQUIRE_INTEGRATION_DB=1`) | NOT VERIFIED (remote) | Workflow structure asserted by `tests/unit/test_ci_workflow.py`. No GitHub remote yet |

## Changelog
- 2026-09-29 — created (DRAFT)
- 2026-09-30 — APPROVED by product owner; implemented. The driver changed from psycopg 3 to asyncpg (ADR-002
  amended; product owner decision after Windows Smart App Control blocked psycopg's bundled DLL). Dev DB host
  port 55432. Latent S01 issue fixed along the way: an empty `API_TOKENS_FILE=` / `API_TOKENS=` now means
  "not set" (previously became the path `.`), with unit tests. CI gained an `integration` job. IN_REVIEW.
- 2026-09-30 — review fixes applied (code-reviewer and data-engineer): migration 0002, NaN/NUL-safe audit, `record(conn=)`, migration timeout profile, 503 on DB down, event-trigger no-DDL test, WS hardening. AC4 wording updated to the event-trigger method. Deferred items recorded in S15/S25/S26. Awaiting human acceptance.
