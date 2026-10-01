---
id: S05
title: Airflow client, live context provider, operator APIs (META-09)
status: IN_REVIEW
phase: 1
owner_agent: backend-engineer
reviewers: [code-reviewer, security-reviewer]
depends_on: [S03, S04]
blocked_by: []
traces: [FR-010, FR-011, FR-012, FR-013, FR-014, NFR-001, NFR-002, NFR-011, NFR-012]
source_sections: ["src §4.1", "src §7", "src §33.8", "src §33.9", "src §35.3"]
tags: [security-sensitive]
approved_by: product owner
approved_on: 2026-10-01
---

# S05 — Airflow integration

## Objective
Provide one backend Airflow integration that serves:
- live runtime context (invariant 1: Airflow live state beats static metadata);
- agent read tools;
- operator actions for the UI (ADR-009: Streamlit never talks to Airflow);
- an idempotent recovery trigger and terminal wait for workers (ADR-011).

The platform's Airflow credentials only ever reach DAGs registered in metadata.

## Read first
- `docs/architecture.md`: §6 (`/airflow/*`, `/pipelines/{id}/context`), §10 RBAC, §13 timeouts and retries, ADR-009, ADR-011, ADR-012.
- S04 `MetadataReader`, `PipelineMetadata.airflow.dag_id` and `get_pipeline_by_dag_id`.
- S01 redaction (`redact_text`), `require_role`, readiness, `ApiError`.
- S02 `AuditService`.
- S03 `RunId`/`DagId`/`TaskId`.

## In scope

### Settings (`backend/airflow/settings.py`, `AirflowSettings`, pydantic-settings, `.env`)
| Key | Default | Notes |
|---|---|---|
| `AIRFLOW_BASE_URL` | — (unset = not configured) | Validated as an http(s) URL with no credentials, query or fragment. **prod requires `https://`**. |
| `AIRFLOW_USERNAME` | — | Required when the base URL is set. |
| `AIRFLOW_PASSWORD` | — (`SecretStr`, no default) | Required when the base URL is set; never logged, echoed or returned. |
| `AIRFLOW_API_VERSION` | `v1` | `AirflowApiVersion` seam. Only `v1` (Airflow 2.x stable REST) exists (OQ-03). |
| `AIRFLOW_CONNECT_TIMEOUT_S` / `AIRFLOW_READ_TIMEOUT_S` | `5` / `30` | Architecture §13 |
| `AIRFLOW_GET_RETRIES` | `3` | Exponential backoff (0.5 s base, ×2, jitter, capped at 8 s) on connect/read timeouts, 5xx and 429 (honours `Retry-After` ≤ 30 s). GETs only. |
| `AIRFLOW_LOG_MAX_CHARS` | `20000` | Tail kept for agents and the UI |
| `AIRFLOW_LOG_MAX_DOWNLOAD_BYTES` | `2097152` | Memory bound: log responses are streamed into a **rolling tail** of at most this many bytes. Must be ≥ 2 × `AIRFLOW_LOG_MAX_CHARS`. |
| `AIRFLOW_LOG_DOWNLOAD_CEILING_BYTES` | `33554432` | Time and traffic bound: reading stops here, and the result is marked "not the end of the log" *(decision 2026-10-01)*. |
| `AIRFLOW_CONTEXT_RUN_LIMIT` | `5` | Recent runs included in a pipeline context |
| `RECOVERY_POLL_SECONDS` | `30` | `wait_for_terminal` poll interval |

**Not configured** (`AIRFLOW_BASE_URL` unset) is allowed outside prod:
- the readiness component `airflow` is `degraded` with detail `not configured`;
- the context endpoint returns `airflow.unavailable = true` (`error: "not configured"`);
- `/airflow/*` routes return 503 `AIRFLOW_UNAVAILABLE`.

In prod, startup fails fast (`ConfigurationError`) when the base URL, username or password is missing or the base URL isn't https.

### Models (`backend/airflow/models.py`, S03 `Contract` base, tolerant of Airflow additions)
- **`AirflowDag`**: `dag_id`, `is_paused`, `is_active`, `schedule_interval: JsonObject | str | None` (Airflow returns an object or a string), `timetable_description?`, `next_dagrun?`, `next_dagrun_data_interval_start?`, `next_dagrun_data_interval_end?`, `tags: tuple[str, ...]`. S14 uses `next_dagrun` and the schedule for `SCHEDULE_MISSED`.
- **`AirflowDagRun`**: `dag_id`, `dag_run_id`, `state` (string), `run_type?`, `logical_date?`, `data_interval_start?`, `data_interval_end?`, `start_date?`, `end_date?`, `external_trigger?`, `conf: JsonObject = {}`.
- **`AirflowTaskInstance`**: `task_id`, `map_index = -1`, `state?`, `try_number`, `start_date?`, `end_date?`, `duration?`, `operator?`.
- **`AirflowHealth`**: `metadatabase`, `scheduler` (each `healthy|unhealthy|unknown`) and `latest_scheduler_heartbeat?`.
- **`TaskLog`**: `dag_id`, `dag_run_id`, `task_id`, `try_number`, `map_index`, `content` (redacted tail), `truncated: bool`, `original_chars?`.
- **`AirflowRuntimeContext`**:
  - `dag_id`, `source: Literal["airflow"]`, `fetched_at`, `unavailable: bool`, `error?`;
  - `dag: AirflowDag?`, `runs: tuple[AirflowDagRun, ...]` (newest first, up to `AIRFLOW_CONTEXT_RUN_LIMIT`), `selected_run?`;
  - `task_instances` and `failed_tasks` (state ∈ `{failed, upstream_failed}`) of the selected run.
  - It carries no logs.

  `error` is a short category (e.g. `timeout`, `unreachable`, `http_503`, `auth_failed`, `not configured`), never a response body.
- Datetimes are timezone-aware UTC. States stay strings, so a newer Airflow state never fails parsing; the known-state constants live in one module.
- **Known Airflow states:**
  - `DAG_RUN_TERMINAL = {success, failed}`;
  - `FAILED_TASK_STATES = {failed, upstream_failed}`.

### Client (`backend/airflow/client.py`)
- **`AirflowClient` Protocol** (ADR-012): `health`, `list_dags(dag_ids)`, `get_dag`, `patch_dag(dag_id, is_paused)`, `list_dag_runs(dag_id, limit)` (newest first, `order_by=-execution_date`), `get_dag_run`, `trigger_dag_run(dag_id, dag_run_id, conf)`, `list_task_instances(dag_id, dag_run_id)`, `get_task_log(dag_id, dag_run_id, task_id, try_number, map_index=-1)`.
- **`HttpAirflowClient`** implements it on a shared `httpx.AsyncClient`:
  - basic auth and the configured timeouts;
  - `follow_redirects=False`, so credentials are never sent to another host;
  - **every path segment URL-encoded** with `quote(segment, safe="")`, closing the S03 open issue for run IDs containing `/`, `?`, `#` or `..`;
  - JSON responses validated into the models above.
- **Errors** (`backend/airflow/errors.py`, subclasses of `AirflowError`). Messages carry the HTTP method, path template and status, never response bodies or credentials.

  | Exception | Raised for |
  |---|---|
  | `AirflowUnavailable` | timeout, connect error, 5xx, 429 after retries |
  | `AirflowNotFound` | 404 |
  | `AirflowConflict` | 409 |
  | `AirflowAuthError` | 401, 403 |
  | `AirflowRequestError` | other 4xx |
  | `AirflowProtocolError` | a response that fails model validation |

- **Retries:** only GETs, per the settings above. `POST` (trigger) and `PATCH` (pause) are **never** retried (NFR-011).

### Context provider (`backend/airflow/context_provider.py`)
`AirflowContextProvider(client).get_pipeline_context(dag_id, run_id=None) -> AirflowRuntimeContext`:
- It fetches the DAG and the recent runs. The selected run is `run_id`, or the newest run when none is given. It then fetches the task instances of the selected run.
- Any `AirflowError` gives `unavailable=true` with the error category, and whatever was fetched before the failure is kept. It never raises for Airflow failures.
- An unknown `run_id` gives `selected_run=None` with `error="run not found"`.

### Agent read tools (`backend/airflow/tools.py`)
- The tools are plain async functions on an `AirflowReadTools(client, allowed_dag_ids, settings)` object; S08 wraps them as agent tools.
- `get_workflow_run_details(dag_id, run_id)` returns the run plus a task-state summary.
- `get_failed_tasks_in_run(dag_id, run_id)` returns the failed and upstream_failed tasks.
- `get_task_logs(dag_id, run_id, task_id, try_number=None)` returns a `TaskLog`. It defaults to the task's latest try, keeps the last `AIRFLOW_LOG_MAX_CHARS` characters with a leading `[… truncated N chars …]` marker, and redacts the result with S01 `redact_text`.
- Every tool rejects a `dag_id` outside `allowed_dag_ids` with `AirflowToolDenied`, before any HTTP call. The tools never trigger or patch.

### Recovery (`backend/airflow/recovery.py`, worker code only, ADR-011)
- **`trigger_recovery_run(client, dag_id, review_id, revision, recovery_of_run_id) -> AirflowDagRun`:**
  - `dag_run_id = f"recovery__{review_id}__r{revision}"`;
  - the conf is fixed: `{"self_healing": {"review_id", "revision", "recovery_of": recovery_of_run_id}}`;
  - on 409 it fetches the run with the same `dag_run_id` and returns it when its conf carries the same `review_id` and `revision`. Otherwise it raises `AirflowConflict` (a logical-date collision or a foreign run).
- **`wait_for_terminal(client, dag_id, dag_run_id, timeout_s, poll_s, clock, sleep) -> RecoveryOutcome{status: success|failed|timeout, run?}`:**
  - it is stateless and polls by `dag_run_id`, so S20 can resume after a restart;
  - `AirflowUnavailable` during polling is retried until the timeout;
  - `AirflowNotFound` gives `failed` with the reason `run disappeared`.
- **Not exposed as an agent tool, nor through any route** (S11-AC3).

### API (`backend/api/airflow.py`, `backend/api/pipelines.py`)
Every route resolves `dag_id` against **registered DAGs only** *(decision 2026-10-01)*: the `airflow.dag_id` of an ACTIVE pipeline in the S04 snapshot. Any other `dag_id` gets 404 `AIRFLOW_DAG_NOT_REGISTERED`, and Airflow is not called.

| Method | Path | Role | Request / response |
|---|---|---|---|
| GET | `/airflow/dags` | viewer | `{items: [AirflowDag]}`, for registered DAGs only, sorted by `dag_id`. A registered DAG missing in Airflow appears in `missing: [dag_id]`. |
| GET | `/airflow/dags/{dag_id}/runs?limit=1..100 (25)` | viewer | `{items: [AirflowDagRun]}` |
| GET | `/airflow/dags/{dag_id}/runs/{run_id}/tasks` | viewer | `{items: [AirflowTaskInstance]}`; 404 `AIRFLOW_NOT_FOUND` |
| POST | `/airflow/dags/{dag_id}/runs` | operator | Body `{}`. Extra fields are rejected: **no `conf` in v1** *(decision 2026-10-01)*. `dag_run_id = manual__console__{uuid4}`. Returns 201 `AirflowDagRun`. |
| PATCH | `/airflow/dags/{dag_id}` | operator | Body `{is_paused: bool}`; returns 200 `AirflowDag` |
| GET | `/pipelines/{pipeline_id}/context?run_id=` | viewer | 200 `{pipeline_id, static_metadata: PipelineMetadata, airflow: AirflowRuntimeContext}`. The `dag_id` comes only from metadata, and an unknown pipeline gives 404 `METADATA_NOT_FOUND`. It is still 200 when Airflow is down. |

**Error mapping:**
- `AirflowUnavailable` and not configured → 503 `AIRFLOW_UNAVAILABLE`;
- `AirflowNotFound` → 404 `AIRFLOW_NOT_FOUND`;
- `AirflowConflict` → 409 `AIRFLOW_CONFLICT`;
- `AirflowAuthError` → 502 `AIRFLOW_AUTH_FAILED` (a platform misconfiguration, not the caller's fault);
- `AirflowRequestError` and `AirflowProtocolError` → 502 `AIRFLOW_BAD_RESPONSE`.

**Audit:** every trigger and pause/resume attempt, whether it succeeded or failed, writes one `operation_audit` row:
- phase `airflow`, event_type `AIRFLOW_TRIGGER` or `AIRFLOW_PAUSE`/`AIRFLOW_RESUME`, and status `OK` or the error code;
- the actor's subject and role, `dag_id`, `run_id` (for triggers) and `duration_ms`.

Without a database (dev only), it logs a warning `audit unavailable`.

### Readiness
Component `airflow`:
- `ok` when `/api/v1/health` reports a healthy metadatabase and scheduler;
- `degraded` when either is unhealthy, Airflow is unreachable, or Airflow is not configured.

It is **never** `down`: Airflow outages must not take the control plane out of rotation. The check has a 5 s timeout and no retries.

### Fake (`tests/fakes/airflow.py`)
`FakeAirflow`, a respx-based scenario builder for downstream slices. It follows Airflow 2.x `/api/v1` response shapes. Builders:
- `add_dag`, `add_run`, `add_task` and `set_log`;
- `fail_next(path, status|timeout, times)`;
- trigger behaviour: create the run, or answer 409 for an existing `dag_run_id`;
- a `calls` log for asserting retries and "no call" cases.

## Contract decisions (made during implementation and review)
- **Log downloads (product owner, 2026-10-01):** a rolling tail bounded in memory, with a separate download ceiling (`AIRFLOW_LOG_DOWNLOAD_CEILING_BYTES`, 32 MiB) and an explicit "not the end of the log" note when it is hit. AC3 is amended.
- **Shared redaction module (`backend/observability/redaction.py`, S01):**
  - every pattern is bounded or possessive and anchored at token starts, and patterns are skipped when their marker substring is absent;
  - key=value detection matches whole keys (so `aws_secret_access_key=` is caught);
  - added patterns: AWS key IDs, JWTs, PEM private keys, an empty URL user, private_key/fernet_key/passphrase;
  - all S01/S02 redaction tests still pass.
- **`AirflowReader`:** a read-only Protocol (reads plus logs) for agent tools (S08) and monitors (S14). `AirflowClient` adds the writes.
- **Run `conf` is redacted** (`redact_value`) when parsed, so viewers and agents never see secrets other systems put in conf. Recovery adoption keys are not sensitive.
- **Readiness:** the Airflow probe has its own 3 s bound (inside the registry's 5 s), maps every failure to `degraded`, and is cached for 10 s.
- **Operator-action audit:**
  - every attempt is recorded, including `DENIED` (unregistered DAG) and `OUTCOME_UNKNOWN` (unexpected error);
  - a failing audit write after a successful trigger is logged loudly and the run is still returned, so the operator does not trigger twice;
  - role denials (403) happen before the service and are not audited (S23).
- **Client:**
  - task-instance paging continues until an empty page or `total_entries`, surviving a small `maximum_page_limit`;
  - unknown health statuses become `unknown`;
  - non-JSON write responses become `AirflowProtocolError`;
  - log retries honour `Retry-After`;
  - registered DAGs are fetched four at a time.
- **Tools:** `get_task_logs` takes `map_index` and, without explicit values, picks the failed instance and its latest try.
- **Fake fidelity:** `FakeAirflow` honours `order_by`, `Accept` (it answers JSON unless `text/plain` is asked for), `map_index`, a configurable `max_page_limit`, and chunked log streams.

## Out of scope
- Airflow callbacks (S13), DQ checkpoint helpers (S17), monitoring rules (S14).
- Agent tool wrapping (S08) and the combined runtime context builder for RCA (S09, `backend/services/`).
- Recovery job, lease and resolution (S20).
- Airflow 3 / `/api/v2` (OQ-03 seam only).
- A real Airflow container (S22).

## Public contracts
- Exports for S08, S09, S11, S14 and S20: the `AirflowClient` Protocol, `HttpAirflowClient`, the models above, `AirflowContextProvider`, `AirflowReadTools`, `trigger_recovery_run`, `wait_for_terminal` and `RecoveryOutcome`.
- The errors and the routes as tabled above.

## Persistence and migrations
None. Audit rows use `operation_audit` (S02).

## Acceptance criteria
- **S05-AC1** With the fake Airflow, `GET /pipelines/customer_daily_load/context` returns `static_metadata` and `airflow`. The `airflow` section has recent runs (newest first, ≤ limit), the selected run's task states, `failed_tasks` (failed and upstream_failed only), `fetched_at` and `source: "airflow"`. `?run_id=` selects that run, and an unknown `run_id` gives `selected_run=null` with an error.
- **S05-AC2** Connect and read timeouts, 5xx and 429 map to `AirflowUnavailable` after `AIRFLOW_GET_RETRIES` GET retries with backoff, honouring `Retry-After`. POST and PATCH are attempted exactly once. A 404/409/401/403/other 4xx maps to its error type without retry. When Airflow is down or not configured, the context endpoint returns 200 with `airflow.unavailable = true` and an error category, and no response bodies are echoed.
- **S05-AC3** *(amended 2026-10-01, product owner: rolling tail)*
  - Task logs keep only the last `AIRFLOW_LOG_MAX_CHARS` characters, with a truncation marker.
  - Downloads keep a rolling tail of at most `AIRFLOW_LOG_MAX_DOWNLOAD_BYTES` in memory, so the real end of the log is kept, and stop at `AIRFLOW_LOG_DOWNLOAD_CEILING_BYTES` (within one chunk). A capped result says it is not the end of the log.
  - The first partial line of a trimmed window is dropped. ANSI and control characters are stripped.
  - Credential patterns are redacted: passwords in URLs (including an empty user), bearer and basic tokens, keys containing password/secret/token/api_key/private_key/fernet_key/passphrase, AWS key IDs, JWTs and PEM private keys.
  - Redaction stays linear: a 3 MB single line is handled in under 2 s, and it runs off the event loop in the tools.
- **S05-AC4** `trigger_recovery_run` is idempotent: two calls with the same review/revision create one Airflow run and return the same `dag_run_id`. A 409 for a run whose conf belongs to a different review raises `AirflowConflict`.
- **S05-AC5** `wait_for_terminal` returns `success`, `failed` or `timeout` correctly (fake clock and sleep). It tolerates transient `AirflowUnavailable` until the timeout, and reports `failed` when the run disappears.
- **S05-AC6** Viewers can read `/airflow/*` and get 403 on trigger and pause. Operators can trigger and pause/resume. Each attempt (success or failure) writes exactly one audit row with the actor, `dag_id` and outcome. A trigger body with `conf` or any other field gets 422.
- **S05-AC7** Agent read tools reject a `dag_id` not in their allowlist with `AirflowToolDenied`, with zero HTTP calls. They expose no trigger or patch capability (API-surface test).
- **S05-AC8** An unknown pipeline gives 404 on `/pipelines/{id}/context`. No route response, error message or log record contains the Airflow password or the basic-auth header value (assertion over response bodies and captured logs).
- **S05-AC9** *(new, decision 2026-10-01)* Every `/airflow/*` route given a `dag_id` not linked to an ACTIVE pipeline returns 404 `AIRFLOW_DAG_NOT_REGISTERED` with zero Airflow calls. `GET /airflow/dags` lists only registered DAGs.
- **S05-AC10** *(new; strengthened in review)* Path segments are URL-encoded: a run ID such as `manual__2026-10-01T00:00:00+00:00` reaches Airflow as one encoded segment.
  - Identifiers outside Airflow's allowed set (`^[A-Za-z0-9_.~:+-]{1,250}$`), and `.`, `..` or `~`, are rejected before any request: by the client (`AirflowRequestError`) and at the API edge (422). Encoding alone is not enough, because proxies often decode `%2F`.
  - A response describing a different DAG or run than requested is rejected.
  - Redirects are not followed.
- **S05-AC11** *(new)* Configuration:
  - prod refuses to start without `AIRFLOW_BASE_URL`, username and password, or with a non-https base URL;
  - a base URL containing credentials, a query or a fragment is rejected;
  - outside prod, an unconfigured Airflow gives readiness `airflow: degraded "not configured"` and route 503s.
- **S05-AC12** *(new)* Readiness `airflow` is `ok` with a healthy metadatabase and scheduler, and `degraded` (never `down`) when unhealthy or unreachable. `/health/ready` stays 200 when only Airflow is degraded.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC8, AC9 | unit (API + fake Airflow) | `tests/unit/api/test_pipeline_context.py`, `tests/unit/api/test_airflow_routes.py` |
| AC2, AC3, AC10 | unit | `tests/unit/airflow/test_client.py`, `tests/unit/airflow/test_logs.py` |
| AC4, AC5 | unit | `tests/unit/airflow/test_recovery.py` |
| AC6 | unit (roles, validation) + integration (audit rows, real DB) | `tests/unit/api/test_airflow_routes.py`, `tests/integration/api/test_airflow_audit.py` |
| AC7 | unit | `tests/unit/airflow/test_tools.py` |
| AC11, AC12 | unit | `tests/unit/airflow/test_settings.py`, `tests/unit/api/test_airflow_routes.py` |

## Runtime verification
**NOT VERIFIED against a real Airflow** *(decision 2026-10-01)*: no Airflow instance is available yet. Verification uses the
fake, whose responses follow the Airflow 2.x `/api/v1` OpenAPI shapes, plus a manual API run against it. S22 adds Airflow
standalone with the `customer_daily_load` DAG and verifies against it.

## Security and eval considerations
- **Credentials:** credentials only come from the environment. The client is the only holder, the base URL can't embed credentials, redirects are not followed, prod requires https, and credentials appear in no response, log or audit row.
- **Least privilege:** routes reach registered DAGs only, and operator triggers carry no conf, so the platform can't be used as a general Airflow console.
- **Agent tools:** they are read-only and allowlisted. Logs are untrusted text: redacted and truncated here, then wrapped in `untrusted()` by S08/S09.
- **No agent trigger path:** recovery runs only through worker code (ADR-011).

## Rollback
Revert the code. No data is written apart from audit rows.

## Open issues
- **OQ-03 (Airflow version):** decided as Airflow 2.x `/api/v1`. The `AirflowApiVersion` seam keeps v2 possible.
- **From the S03 review:** `RunId` allows `/`, `?`, `#` and `..`. Path segments are URL-encoded (AC10).
- **Decided 2026-10-01 (product owner):**
  1. Routes reach registered DAGs only (AC9).
  2. No `conf` on operator triggers in v1.
  3. Runtime verification uses the fake; the real-Airflow check moves to S22.
- **Decided 2026-10-01 (product owner, review):**
  4. Log downloads keep a rolling tail with a 32 MiB ceiling (AC3 amended).
- **Cross-slice:**
  - **S14:** use `AirflowDag.next_dagrun` and the schedule fields for `SCHEDULE_MISSED`, and the runs' `logical_date` for `SLA_MISSED`.
  - **S20:** resume `wait_for_terminal` by `dag_run_id`, and treat `AirflowConflict` from `trigger_recovery_run` as "needs operator". Writes are never auto-retried.
  - **S22:** verify the client against real Airflow.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|
| Lint / format | `uv run ruff check .` / `uv run ruff format --check .` | pass | |
| Types | `uv run mypy backend frontend tools` | pass | strict, 80 files |
| Unit + contract | `uv run pytest -m "not integration and not e2e and not live"` | 582 passed, 1 skipped | The skip is the S04 Windows symlink test |
| Integration | `uv run pytest -m integration` | 43 passed | Operator-action audit rows (OK, HTTP_503, DENIED, pause/resume) in a migrated throwaway DB. The S02 startup test now allows the new `airflow: degraded` component. |
| Traceability | `uv run python tools/trace.py --strict` | pass | S05-AC1..AC12 covered |
| Redaction performance | `test_redaction_work_is_bounded_on_one_huge_line` | pass | 3 MB line in under 2 s (before: quadratic, seconds per 100 KB) |
| Runtime vs real Airflow | — | **NOT VERIFIED** | Decision 2026-10-01: no Airflow instance yet. Verified through ASGI tests against `FakeAirflow` (Airflow 2.x `/api/v1` shapes). S22 runs the real check. |
| Reviews | code-reviewer, security-reviewer | findings addressed | See dispositions below |

**Review dispositions.**
- **Fixed:**
  - Readiness could report `down` (code H1).
  - Health parsing crash on unknown statuses (code M1).
  - Paging stopped early (code M2).
  - Log ceiling vs spec (code M3, product-owner decision, chunked tests).
  - Log retries ignored `Retry-After` (code M4).
  - Mapped-task logs (code M5).
  - Fake fidelity (code M6).
  - Audit gaps (code M7, security M2).
  - Read-only reader Protocol (code L1).
  - Run deleted mid-request (code L2).
  - Safe identifiers against `%2F` decoding proxies (security M1).
  - Redaction ReDoS and coverage (security M3).
  - Conf redaction (security L4).
  - Partial first line (security L5, code L5).
  - Denied attempts audited (security L7).
  - DAG fetch concurrency limit (security L9).
  - Readiness cache (security L10).
  - ANSI and control characters (security L11).
- **Handed downstream (Open issues):**
  - recovery adoption hardening (security L8, code L3);
  - per-request budget and circuit breaker (security L9 remainder);
  - auditing role denials (security L7 remainder);
  - Airflow < 2.10 `try_number` semantics (code M6).
- **Accepted as-is:**
  - The context route also serves INACTIVE pipelines (security L6). The dag_id still comes only from metadata, and the registered-DAG decision covers `/airflow/*`.
  - Plain http outside prod (security L11), for dev Airflow.
  - The owned client is created in `create_app` (code L4) and closed in the lifespan.

## Changelog
- 2026-09-29 — created (DRAFT)
- 2026-10-01 — refined via `/slice-spec`.
  - Concrete settings, models, client Protocol, error mapping, retry policy, context provider, tools, recovery and routes.
  - Prod https requirement, URL encoding, no redirects, log download cap, never-`down` readiness and the fake builder.
  - AC1–AC8 sharpened (IDs kept); AC9–AC12 added.
  - Product owner decided: registered DAGs only, no operator conf, verification with the fake.
  - Status READY.
- 2026-10-01 — approved by product owner ("approve S05 and implement it"). Status IN_PROGRESS.
- 2026-10-01 — implemented. Reviews by code-reviewer and security-reviewer were addressed. Product owner chose the rolling log tail with a ceiling (AC3 amended). AC10 strengthened. Status IN_REVIEW.
