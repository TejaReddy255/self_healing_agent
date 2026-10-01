---
id: S13
title: MonitoringEvent persistence and ingestion (AM-01, AM-03)
status: DRAFT
phase: 3
owner_agent: backend-engineer
reviewers: [code-reviewer, data-engineer, security-reviewer]
depends_on: [S02, S03, S04]
blocked_by: []
traces: [FR-070, NFR-012, NFR-020]
source_sections: ["src §21.3", "src §21.7", "src §21.8", "src §21.9", "src §22.2"]
tags: [schema, security-sensitive]
approved_by:
approved_on:
---

# S13 — MonitoringEvent persistence and ingestion

## Objective
Give every monitoring signal (from callbacks or monitors) one durable, append-only, idempotent
home, plus a fast ingestion endpoint that Airflow callbacks can call safely.

## In scope
- Migration: `monitoring_events` (event_id uuid PK, idempotency_key text UNIQUE, pipeline_id, dag_id,
  run_id, task_id, source, event_type, severity, resource, rule_id, expected, actual, evidence jsonb,
  detected_at, ingested_at). Indexes on (pipeline_id, detected_at desc), (severity, detected_at desc) and (run_id).
- `backend/repositories/monitoring_repository.py`: `save_many(events) -> list[SaveResult{event_id, duplicate}]`
  using `INSERT … ON CONFLICT (idempotency_key) DO NOTHING RETURNING`, then a lookup for duplicates. No update or delete methods.
- `backend/services/monitoring_event_service.py`: `ingest(event, actor)`. The default idempotency key is
  `sha256(source|event_type|pipeline_id|run_id|task_id|resource|rule_id|detected_at truncated to MONITORING_EVENT_BUCKET_S)`,
  or caller-supplied. It validates that `pipeline_id` exists in metadata and that `dag_id` matches the pipeline's `dag_id`.
  A post-persist hook (`EventSink` protocol) is a no-op until S15.
- APIs: `POST /monitoring/events` (`service` role; 202 `{event_id, duplicate}`), `GET /monitoring/events`
  (filters: pipeline_id, run_id, source, severity, from, to; limit ≤ 500; keyset pagination).
- `integrations/airflow/self_healing_callback.py`: an `on_failure_callback` factory that builds an event from the
  Airflow context and POSTs it with a 3 s timeout and a service token from an Airflow Connection/Variable. It never raises into the task.

## Out of scope
Monitors (S14). Incidents (S15).

## Configuration
`MONITORING_EVENT_BUCKET_S=300`, `INGEST_MAX_EVIDENCE_BYTES=32768`.

## Acceptance criteria
- **S13-AC1** A valid POST → 202, persisted, with `event_id` returned.
- **S13-AC2** The same idempotency key twice → a single row. The second response has `duplicate: true` and the same `event_id`.
- **S13-AC3** Unknown `pipeline_id`, a mismatched `dag_id`, or an invalid source/severity → 422. Oversized evidence → 413.
- **S13-AC4** Non-`service` callers get 403 on POST.
- **S13-AC5** Ingest p95 < 200 ms over 200 sequential requests against local PG (smoke measurement recorded in evidence).
- **S13-AC6** Callback helper: when the backend is down or slow, it returns within the timeout, logs a warning, and does not raise.
- **S13-AC7** The repository exposes no update/delete path (API surface test), and ingestion performs no LLM or MCP calls (spies).
- **S13-AC8** Query filters and pagination return correct, stable results.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1–AC4, AC8 | integration | `tests/integration/api/test_monitoring_events.py` |
| AC5 | integration (perf smoke) | `tests/integration/perf/test_ingest_latency.py` |
| AC6 | unit | `tests/unit/integrations/test_airflow_callback.py` |
| AC7 | unit | `tests/unit/repositories/test_monitoring_repo_surface.py` |

## Rollback
Revert code. The table is additive. Remove the callback from DAGs.

## Open issues
- From S03 review: reject a caller-supplied `event_id` at ingest (or use an inbound model without it). `MonitoringEvent.metric` exists for checks without a `rule_id` (fingerprint input, src §24.2). DQ `expected`/`actual` dicts are written as `canonical_json` strings in the event, with the structured copy in `evidence`.
- From S04 review: while `MetadataService.state == "EMPTY"`, ingest returns 503 (retryable), not 422, so Airflow callbacks are not lost. `get_pipeline_by_dag_id` also returns INACTIVE pipelines, so filter on `status`. Persist the metadata `version` on each event.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
