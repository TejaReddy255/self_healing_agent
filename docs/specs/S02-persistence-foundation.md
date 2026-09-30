---
id: S02
title: Persistence foundation, migrations, audit and activity
status: DRAFT
phase: 0
owner_agent: data-engineer
reviewers: [code-reviewer, data-engineer]
depends_on: [S01]
blocked_by: []
traces: [FR-110, NFR-001, NFR-040]
source_sections: ["src §0.1-8", "src §5.1", "src §11", "src §12 (/activities, /ws/activities)", "src §35.8"]
tags: [schema]
approved_by:
approved_on:
---

# S02 — Persistence foundation, migrations, audit and activity

## Objective
Provide the async PostgreSQL access layer, the numbered-migration runner (ADR-003), and the
cross-cutting audit, activity and counter persistence that every later slice writes to.

## Read first
`docs/architecture.md` §8, §9, §14, ADR-003; S01 evidence.

## In scope
- `backend/db/pool.py`: psycopg 3 `AsyncConnectionPool` built from settings (`PG_HOST`, `PG_PORT`,
  `PG_DATABASE`, `PG_USER`, `PG_PASSWORD: SecretStr`, pool sizes, `PG_STATEMENT_TIMEOUT_MS`), with a `transaction()` helper.
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
- `AuditService.record(event) -> int`; `ActivityService.publish(type, details, **ids) -> Activity`.
- `GET /activities` → `list[Activity]`, `limit` 1..500 (default 50).

## Persistence and migrations
One migration: `operation_audit`, `activities`, `agent_performance` (seed row id=1).
Correlation-ID columns are nullable and have no FK (FKs are added in S15 when the targets exist).

## Configuration
`PG_*`, `PG_POOL_MIN/MAX`, `PG_STATEMENT_TIMEOUT_MS=30000`, `AUDIT_MAX_PAYLOAD_BYTES=65536`.

## Acceptance criteria
- **S02-AC1** `migrate up` on an empty DB applies all migrations. A second run is a no-op. `status` lists applied and pending migrations.
- **S02-AC2** If an already-applied migration file is modified, `up` and `verify` fail with a checksum error and apply nothing.
- **S02-AC3** Two concurrent `migrate up` processes result in each migration applied exactly once.
- **S02-AC4** API startup executes no DDL (asserted by comparing `pg_catalog` before and after startup). `/health/ready` reports `db` and `migrations` component status and returns 503 when the DB is down or migrations are pending.
- **S02-AC5** `AuditService.record` persists all correlation columns, redacts sensitive keys, and truncates oversized payloads with a marker.
- **S02-AC6** A WebSocket subscriber receives a newly published activity within 2 s. A disconnected client does not break publishing.
- **S02-AC7** `GET /activities` enforces the limit bounds and keyset pagination. A viewer can call it; an unauthenticated caller gets 401.
- **S02-AC8** `agent_performance` increments are atomic under 50 concurrent increments.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1–AC3 | integration | `tests/integration/db/test_migrate.py` |
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
—

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
