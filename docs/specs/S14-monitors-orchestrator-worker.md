---
id: S14
title: Monitors, Monitoring Orchestrator and worker (AM-04, AM-06, AM-07; META-05)
status: DRAFT
phase: 3
owner_agent: backend-engineer
reviewers: [code-reviewer, devops-sre]
depends_on: [S05, S06, S13]
blocked_by: []
traces: [FR-071, FR-072, FR-073, FR-074, FR-075, FR-076, NFR-004, NFR-010, NFR-021, UC-AC2, UC-AC7]
source_sections: ["src §21.2", "src §21.4", "src §21.5", "src §21.10", "src §22.1", "src §22.3", "src §33.10"]
tags: [runtime]
approved_by:
approved_on:
---

# S14 — Monitors, orchestrator and worker

## Objective
Detect Airflow, SLA, database and MCP problems deterministically for every monitoring-enabled pipeline,
persist the events, aggregate health, and run continuously in a worker with no UI dependency.

## In scope
- `backend/monitoring/base.py` `PipelineMonitor` Protocol (src §21.10) and `Clock` abstraction.
- `airflow_monitor.py`: failed run → `DAG_RUN_FAILED` CRITICAL; failed/upstream_failed task →
  `TASK_FAILED` CRITICAL (evidence: task_id, try_number, state, timestamps, log reference); running >
  `max_runtime_minutes` → `RUN_OVER_DURATION` WARNING, and > `critical_runtime_factor ×` → CRITICAL;
  queued/scheduled within the window → no event.
- `sla_monitor.py`: `expected_completion_time` in `sla.timezone` passed without a successful run for the
  logical date → `SLA_MISSED` (severity from metadata, default CRITICAL); missing expected schedule → `SCHEDULE_MISSED`.
- `database_monitor.py`: operational DB check → `DATABASE_UNAVAILABLE` CRITICAL (exception category only, no DSN).
- `mcp_monitor.py`: servers for the pipeline's source/target systems → `MCP_UNHEALTHY` CRITICAL.
- `dq_monitor.py` placeholder: returns `[]` and `monitor_status=skipped` until S18.
- `orchestrator.py` `MonitoringOrchestrator.check_pipeline(pipeline_id)` / `run_all()`: monitors selected from
  `pipeline.monitoring.*` flags only; per-monitor timeout; a monitor exception → `monitor_status=error` plus a
  `MONITOR_ERROR` WARNING event; persists via S13; CRITICAL (and configured WARNING classes) → `IncidentSink`
  (no-op until S15); aggregates `PipelineHealthResult`.
- Migration: `pipeline_health` (pipeline_id PK, overall_status, monitor_status jsonb, event_ids jsonb, evaluated_at), upserted.
- `backend/jobs/monitoring_worker.py`: loop with `MONITORING_INTERVAL_S` and jitter; leader election via
  `pg_try_advisory_lock`; graceful shutdown; its own liveness file/endpoint.
- APIs: `POST /monitoring/run`, `POST /monitoring/pipelines/{id}/check` (operator), `GET /monitoring/health`,
  `GET /monitoring/pipelines/{id}/health`, `GET /pipelines`, `GET /pipelines/{id}` (viewer).

## Out of scope
Incident creation (S15). DQ checks (S17/S18).

## Configuration
`MONITORING_INTERVAL_S=60`, `MONITORING_JITTER_S=5`, `MONITOR_TIMEOUT_S=20`,
`MONITORING_INCIDENT_WARNING_CLASSES=[]`. Pipeline metadata may add `monitoring.critical_runtime_factor` (default 1.5).

## Acceptance criteria
- **S14-AC1** Table-driven fixtures: each src §21.5 row yields the exact `event_type`, severity and required evidence fields.
- **S14-AC2** Queued or scheduled within the normal window → no WARNING/CRITICAL events.
- **S14-AC3** SLA: `max_runtime_minutes=45` with a live runtime of 68 → CRITICAL, with `expected` and `actual` populated. Completion-time checks honor `sla.timezone`, including across a DST boundary.
- **S14-AC4** Repeated cycles over the same failed run → no duplicate events (idempotency keys).
- **S14-AC5** A monitor raising an exception → `monitor_status=error` and a `MONITOR_ERROR` event. Other monitors still run and the cycle completes.
- **S14-AC6** Monitor selection follows metadata flags. A pipeline with `monitoring.sla.enabled=false` runs no SLA monitor. No pipeline IDs are hard-coded (grep test).
- **S14-AC7** Monitors are constructed with read-only clients only. There is no import path from `backend/monitoring` to `execution_toolset` or `backend/llm` (import-lint), and zero model calls occur during cycles (spy).
- **S14-AC8** The worker runs cycles with no API or Streamlit process. With two worker instances, exactly one runs cycles at a time.
- **S14-AC9** A cycle over 50 fake pipelines completes within `MONITORING_INTERVAL_S` (recorded measurement).

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1–AC3 | unit (fake clock, fake Airflow) | `tests/unit/monitoring/test_airflow_monitor.py`, `test_sla_monitor.py` |
| AC4, AC5, AC6 | integration | `tests/integration/monitoring/test_orchestrator.py` |
| AC7 | unit | `tests/unit/test_import_boundaries.py` |
| AC8, AC9 | integration | `tests/integration/jobs/test_monitoring_worker.py` |

## Rollback
Stop the worker. Revert code. The table is additive.

## Open issues
- From S04 (decision 2026-10-01): the monitoring worker process runs `backend.metadata.refresher.MetadataRefresher` so monitor selection follows metadata reloads within `METADATA_REFRESH_INTERVAL_S` (S14-AC6 relies on this).
- From S04 review: when metadata is EMPTY, skip the cycle and record a `metadata` monitor status rather than silently monitoring zero pipelines. `SCHEDULE_MISSED` takes the DAG schedule from live Airflow details via S05 (invariant 1), not from metadata. The worker builds `MetadataService` inside its running event loop and publishes its metadata version and state for skew visibility.
- From S05 refinement: `AirflowDag` exposes `next_dagrun`, the data-interval bounds and `schedule_interval`/`timetable_description` for `SCHEDULE_MISSED`. `AirflowDagRun.logical_date` drives `SLA_MISSED`. Use the S05 `AirflowClient` Protocol and `FakeAirflow`.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
