---
id: S20
title: DQ verification router, recovery verification and incident resolution (AM-10)
status: DRAFT
phase: 5
owner_agent: backend-engineer
reviewers: [code-reviewer, data-engineer, devops-sre, security-reviewer]
depends_on: [S11, S15, S17, S19]
blocked_by: []
traces: [FR-014, FR-061, FR-062, FR-063, FR-064, UC-AC4]
source_sections: ["src §21.4 steps 83–84", "src §25", "src §25.1", "src §32", "src §35.6", "src §36 (7)"]
tags: [schema, runtime, security-sensitive]
approved_by:
approved_on:
---

# S20 — DQ verification, recovery verification and resolution

## Objective
Complete the verification chain: rerun only the relevant DQ rules before recovery; track the recovery
run durably; run the final DQ gate; and make RESOLVED reachable only through the full chain (invariant 7).

## In scope
- `backend/dq/verification_router.py` `DQVerificationRouter.verify(plan.verification_rules, pipeline_id, run_id)`
  → calls `DQOrchestrator.run(..., rule_ids=…)` with request key `verify:{job_id}`.
- Remediation stage `DQ_VERIFY`: runs the router when rules are declared. A required rule not PASS → REPLAN_REQUIRED.
  With no rules declared and no required rules for the pipeline → NOT_APPLICABLE, recorded explicitly.
- Migration: `recovery_runs` (id, incident_id, review_id, job_id, dag_id, original_run_id, recovery_run_id,
  status TRIGGERED|RUNNING|SUCCESS|FAILED|TIMEOUT, final_dq_status PASS|FAIL|NOT_APPLICABLE|PENDING,
  final_dq_run_id, triggered_at, terminal_at, lease columns).
- `backend/jobs/recovery_worker.py`: RECOVERY_VERIFY moves from the remediation worker to a dedicated
  recovery job (lease, resume without re-trigger). Then the **final DQ gate**: rerun required TARGET and
  RECONCILIATION rules for the recovery run (request key `final:{recovery_run_id}`).
- Resolution: only `RecoveryService.complete(recovery_run)` with status SUCCESS and final DQ PASS (or a recorded
  NOT_APPLICABLE) may call `IncidentService.transition(→RESOLVED)`, set job COMPLETED and set review SUCCEEDED.
  Enforced by a service guard **and** a DB check: the incident can only become RESOLVED with a `recovery_runs` row
  in SUCCESS with an acceptable `final_dq_status` (trigger or constraint function).
- Policy: recovery FAILED → incident REPLAN_REQUIRED (default) or FAILED (`self_healing.on_recovery_failure`).
  Final DQ FAIL → REPLAN_REQUIRED.
- `GET /recovery-runs/{id}`. Timeline entries for DQ verification, recovery and final DQ.

## Acceptance criteria
- **S20-AC1** DQ_VERIFY invokes the orchestrator with exactly `plan.verification_rules` (spy on arguments).
- **S20-AC2** Recovery is never triggered when DQ_VERIFY fails or errors (negative test across CRITICAL, WARNING-blocking and ERROR).
- **S20-AC3** Recovery run FAILED or TIMEOUT → incident is not RESOLVED; REPLAN_REQUIRED/FAILED per policy.
- **S20-AC4** Recovery SUCCESS with final DQ FAIL → not RESOLVED; REPLAN_REQUIRED.
- **S20-AC5** Any attempt to set RESOLVED without a qualifying `recovery_runs` row is rejected by both the service guard and the DB guard (direct SQL test).
- **S20-AC6** A recovery worker restart mid-poll resumes polling on the same `recovery_run_id`. The Airflow fake records exactly one trigger.
- **S20-AC7** The happy path with fakes: incident → … → RESOLVED, with `resolved_at` set, review SUCCEEDED and job COMPLETED, in that order in the timeline.
- **S20-AC8** NOT_APPLICABLE final DQ is allowed only when the pipeline's `dq_profile` has no required rules for TARGET/RECONCILIATION, and it is recorded with a reason.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC2 | integration | `tests/integration/jobs/test_dq_verify_stage.py` |
| AC3, AC4, AC6, AC8 | integration | `tests/integration/jobs/test_recovery_worker.py` |
| AC5 | integration | `tests/integration/db/test_resolution_guard.py` |
| AC7 | integration | `tests/integration/flows/test_resolution_chain.py` |

## Rollback
Revert code. If rolled back, S11's in-worker RECOVERY_VERIFY remains the fallback. The tables are additive.

## Open issues
- From S03 (decision 2026-09-30, architecture §7.5): with `retry_airflow_after_success=false`, a COMPLETED job leaves the incident in RECOVERY_RUNNING. Only a recorded operator recovery confirmation moves it to RESOLVED. Build that confirmation (API + audit) here.
- From S05 refinement: `wait_for_terminal` is stateless by `dag_run_id`, so resume after a restart by calling it again. `trigger_recovery_run` raising `AirflowConflict` (a logical-date collision or a foreign run with the same ID) needs an operator decision; never auto-retry the trigger.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
