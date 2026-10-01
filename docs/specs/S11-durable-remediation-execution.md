---
id: S11
title: Durable remediation execution with recovery trigger and terminal wait
status: DRAFT
phase: 2
owner_agent: backend-engineer
reviewers: [code-reviewer, security-reviewer, evaluation-engineer, data-engineer, devops-sre]
depends_on: [S05, S07, S10]
blocked_by: []
traces: [FR-013, FR-014, FR-050, FR-051, FR-052, FR-053, FR-054, FR-062, FR-064, NFR-010, NFR-012, NFR-013, NFR-020, UC-AC3]
source_sections: ["src §10", "src §14.3", "src §16 (sync approval)", "src §25", "src §35.6"]
tags: [ai, security-sensitive, schema, runtime]
approved_by:
approved_on:
---

# S11 — Durable remediation execution

## Objective
Approval enqueues a durable job. A worker re-verifies authorization, executes only the approved plan
through the target-scoped, write-guarded Healing Agent, applies the structural verification gate,
triggers recovery idempotently, and waits for the recovery run's terminal state. A review is
SUCCEEDED only then.

## In scope
- Migration: `remediation_jobs` (id uuid, review_id, revision, approved_payload_hash, status, stage,
  stage_results jsonb, attempts, lease_owner, lease_expires_at, available_at, error_code, error,
  recovery_dag_run_id, incident_id, created_at, updated_at, version; unique (review_id, revision)).
- The approve transaction (S10) inserts the job → response 202 `{review, execution_job_id}`.
- `backend/jobs/remediation_worker.py` + `backend/services/remediation_service.py` with stages:
  - **PRECHECK:** review APPROVED; `payload_hash(current revision) == job.approved_payload_hash == review.approved_payload_hash`;
    SOP resolved; `execution_toolset(target)` valid; precondition assertions (evaluated by S19 once
    available; recorded NOT_APPLICABLE until then).
  - **EXECUTING:** Healing Agent (`healing@1`) with `execution_toolset(target)` + Airflow read tools,
    wrapped by the **write guard** (agent-design §4: prior read on the same server; count ≤ declared;
    tool ∈ `write_tools` if declared; re-hash before the first write). `pipeline_status="blocked"` → REPLAN_REQUIRED with evidence.
  - **TECHNICAL_VERIFY (v1, structural gate):** model output `fixes_applied && pipeline_status=="remediated"` AND
    the audit shows a successful WRITE followed later by a successful READ on the target server (src §10.2).
    S19 adds the assertions.
  - **DQ_VERIFY:** recorded NOT_APPLICABLE (S20).
  - **RECOVERY_TRIGGER:** if `retry_airflow_after_success`, `trigger_recovery_run` (S05, idempotent), with
    `conf = {recovery_of, review_id, revision, job_id}`.
  - **RECOVERY_VERIFY:** `wait_for_terminal` with timeout from metadata (architecture §13), with heartbeats. On success → COMPLETED.
- Review status transitions (architecture §7.1). Crash rule (§7.3): lease expiry during EXECUTING →
  `FAILED(INTERRUPTED_DURING_EXECUTION)` plus a high-severity activity. Other stages resume.
- APIs: `GET /execution-jobs/{id}` (stage timeline, per-stage results, errors),
  `GET /reviews/{id}/execution-jobs`.
- Evals E-EXE-01..06 (replay).

## Out of scope
Semantic assertions and replan (S19). DQ verification, final DQ, `recovery_runs` table and incident resolution (S20).

## Configuration
`REMEDIATION_JOB_LEASE_S=600`, `REMEDIATION_WORKER_CONCURRENCY=2`, `RECOVERY_DEFAULT_TIMEOUT_MIN=90`.

## Acceptance criteria
- **S11-AC1** Approve returns 202 with a job ID. No model or tool call happens in the request path (spy). p95 < 1 s locally.
- **S11-AC2** A revision/hash mismatch at PRECHECK (tampered DB row) → job FAILED(`HASH_MISMATCH`) with zero tool calls.
- **S11-AC3** The Healing Agent's tools ⊆ the target server's tools ∪ Airflow read tools. No trigger or DQ capability is present.
- **S11-AC4** The write guard blocks: a write before any read on that server; writes beyond the declared count; undeclared write tools. Each block is audited as `WRITE_BLOCKED`.
- **S11-AC5** The structural gate fails when the model claims a fix but the audit lacks a write followed by a read → FAILED(`VERIFICATION_FAILED`), and no recovery is triggered.
- **S11-AC6** Recovery is triggered only after TECHNICAL_VERIFY passes. A worker retry after the trigger creates no second Airflow run.
- **S11-AC7** The review becomes SUCCEEDED only after the recovery run reaches `success`. A recovery `failed` or timeout → FAILED with a code.
- **S11-AC8** Worker crash in EXECUTING → never re-executed; FAILED(`INTERRUPTED_DURING_EXECUTION`) with an activity. Crash in RECOVERY_VERIFY → another worker resumes polling without re-triggering.
- **S11-AC9** A stop condition observed by the agent → REPLAN_REQUIRED with evidence. No further writes follow.
- **S11-AC10** Every stage transition and tool/model call carries `job_id`, `review_id` and revision in the audit. `GET /execution-jobs/{id}` shows the ordered stage timeline.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1 | integration | `tests/integration/api/test_approve_enqueue.py` |
| AC2, AC5–AC9 | integration (fakes: MCP, Airflow, chat model) | `tests/integration/jobs/test_remediation_worker.py` |
| AC3, AC4 | unit | `tests/unit/agents/test_write_guard.py`, `test_healing_toolset.py` |
| AC10 | integration | `tests/integration/jobs/test_remediation_audit.py` |
| — | eval (replay) | E-EXE-01..06 |

## Runtime verification
Dev compose with seeded duplicates and a fake Airflow (or real): approve a review for a duplicate-removal plan
(`max_affected_rows=417`). Observe the stages, the target row count of 100,000, the recovery run, and SUCCEEDED.

## Security and eval considerations
This slice is the highest-risk slice. The security review must cover TOCTOU (hash re-check before the first write), guard bypass
through tool aliasing, and crash semantics.

## Rollback
Feature flag `REMEDIATION_EXECUTION_ENABLED` (default true in dev, false until P2 sign-off in shared envs). When false,
approve returns 202 but jobs remain QUEUED.

## Open issues
- From S03 review: at PRECHECK and before the first write, recompute the hash from the persisted canonical bytes (not an in-memory model: `frozen` is shallow for dicts). Refuse unknown `ApprovalPayload.schema_version`. Drain APPROVED jobs before any deploy that changes the payload contract.
- From S03 (decision 2026-09-30): incident updates use `incident_status_for(stage, recovery_skipped=…)` through `can_transition`. A failed PRECHECK → REPLAN_REQUIRED is now a legal APPROVED → REPLAN_REQUIRED edge.
- From S03 review: consider a separate `HealingReport` for W4 output (observed post-state, stop-condition evidence). It is additive.
- From S04 review: PRECHECK re-checks `PipelineMetadata.effective_permitted_systems()` against the current snapshot (fail closed if the target system is no longer permitted) and checks that the pinned SOP (`SOPReference`) is not DEPRECATED. ADR-016 keeps the hash stable across catalogue edits.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
