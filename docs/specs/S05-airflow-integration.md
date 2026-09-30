---
id: S05
title: Airflow client, live context provider, operator APIs (META-09)
status: DRAFT
phase: 1
owner_agent: backend-engineer
reviewers: [code-reviewer, security-reviewer]
depends_on: [S03, S04]
blocked_by: []
traces: [FR-010, FR-011, FR-012, FR-013, NFR-001, NFR-011]
source_sections: ["src §4.1", "src §7", "src §33.8", "src §33.9", "src §35.3"]
tags: [security-sensitive]
approved_by:
approved_on:
---

# S05 — Airflow integration

## Objective
Provide one backend Airflow integration that serves live runtime context, agent read tools,
operator actions for the UI (ADR-009), and an idempotent recovery trigger for workers.

## In scope
- `backend/airflow/client.py` `AirflowClient` (httpx.AsyncClient): `health`, `list_dags`, `get_dag`,
  `patch_dag(is_paused)`, `list_dag_runs`, `get_dag_run`, `trigger_dag_run(dag_run_id, conf)`,
  `list_task_instances`, `get_task_log(try_number)`. Stable REST `/api/v1` with basic auth. A
  `AirflowApiVersion` seam allows adding `/api/v2` (OQ-03).
- Error mapping: `AirflowUnavailable` (timeout, connect, 5xx), `AirflowNotFound`, `AirflowConflict`,
  `AirflowAuthError`. GETs retried per architecture §13. POST/PATCH are not retried.
- `backend/airflow/context_provider.py` `AirflowContextProvider.get_pipeline_context(dag_id, run_id=None)
  -> AirflowRuntimeContext{dag: {is_paused}, runs: [...], selected_run, task_instances, failed_tasks,
  fetched_at, source: "airflow", unavailable: bool, error?}`. No logs by default.
- `backend/airflow/tools.py`: plain async functions (wrapped as agent tools in S08):
  `get_workflow_run_details`, `get_failed_tasks_in_run`, `get_task_logs`. Each validates `dag_id`
  against an allowlist passed at construction (the pipeline's `airflow.dag_id`). Logs keep the
  last `AIRFLOW_LOG_MAX_CHARS` characters and are redacted.
- `backend/airflow/recovery.py`: `trigger_recovery_run(dag_id, review_id, revision, conf)` with
  `dag_run_id = f"recovery__{review_id}__r{revision}"`. On 409, fetch and return the existing run.
  `wait_for_terminal(dag_id, dag_run_id, timeout, poll)` → `success | failed | timeout`. **Not exposed as an agent tool.**
- API routes: `/airflow/*` (architecture §6) and `GET /pipelines/{id}/context` →
  `{pipeline_id, static_metadata: PipelineMetadata, airflow: AirflowRuntimeContext}`; `dag_id` comes only from metadata.
- `tests/fakes/airflow.py`: respx-based fake with a scenario builder (DAGs, runs, task states, logs, trigger behaviour).
- Readiness component `airflow`, which is degraded (not failed) when Airflow is down.

## Out of scope
Airflow callbacks (S13), DQ checkpoint helpers (S17), monitoring rules (S14).

## Configuration
`AIRFLOW_BASE_URL`, `AIRFLOW_USERNAME`, `AIRFLOW_PASSWORD: SecretStr` (no defaults),
`AIRFLOW_API_VERSION=v1`, `AIRFLOW_CONNECT_TIMEOUT_S=5`, `AIRFLOW_READ_TIMEOUT_S=30`,
`AIRFLOW_LOG_MAX_CHARS=20000`, `RECOVERY_POLL_SECONDS=30`.

## Acceptance criteria
- **S05-AC1** With the fake Airflow, `/pipelines/customer_daily_load/context` returns `static_metadata` and `airflow` sections. The `airflow` section has run and task states, failed tasks, `fetched_at` and `source: "airflow"`.
- **S05-AC2** Timeouts and 5xx map to `AirflowUnavailable`. GETs retry up to the configured count with backoff. POST/PATCH are never retried. When Airflow is down, the context endpoint returns 200 with `airflow.unavailable = true` rather than failing the whole response.
- **S05-AC3** Task logs are truncated to the tail limit with a truncation marker, and credential patterns are redacted.
- **S05-AC4** `trigger_recovery_run` is idempotent: two calls with the same review/revision create one Airflow run and both return the same `dag_run_id`.
- **S05-AC5** `wait_for_terminal` returns `success`, `failed` or `timeout` correctly (fake clock).
- **S05-AC6** Viewer can read `/airflow/*`. Viewer gets 403 on trigger and pause. Operator actions are audited with the actor.
- **S05-AC7** Agent read tools reject a `dag_id` not in their allowlist without calling Airflow.
- **S05-AC8** Unknown pipeline → 404 on `/pipelines/{id}/context`. The response never contains Airflow credentials.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC8 | integration (fake Airflow) | `tests/integration/api/test_pipeline_context.py` |
| AC2, AC3 | unit | `tests/unit/airflow/test_client.py`, `test_log_redaction.py` |
| AC4, AC5 | unit | `tests/unit/airflow/test_recovery.py` |
| AC6 | integration | `tests/integration/api/test_airflow_routes.py` |
| AC7 | unit | `tests/unit/airflow/test_tools_allowlist.py` |

## Runtime verification
Against a real dev Airflow (if available): list DAGs, fetch context, trigger a test DAG. Otherwise record `NOT VERIFIED` with the reason.

## Rollback
Revert code.

## Open issues
- OQ-03 (Airflow version).
- From S03 review: `RunId` allows `/`, `?`, `#` and `..` (Airflow run IDs contain `:` and `+`). The Airflow client must URL-encode path segments with `quote(run_id, safe="")`.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
