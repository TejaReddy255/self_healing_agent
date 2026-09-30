---
id: S17
title: DQ platform integration — adapters, registry, orchestrator, persistence (Custom DQ + Recon) (AM-05; META-06)
status: DRAFT
phase: 4
owner_agent: data-engineer
reviewers: [code-reviewer, data-engineer, security-reviewer]
depends_on: [S04, S13, S26, S30, S31]
blocked_by: []
traces: [FR-090, FR-091, FR-092, FR-093, FR-094, FR-095, NFR-006]
source_sections: ["src §17", "src §21.12", "src §23", "src §33.5 (business rules)", "src §33.11", "src §35.4"]
tags: [schema, security-sensitive]
approved_by:
approved_on:
---

# S17 — DQ platform integration (Custom DQ + Recon)

## Objective
Connect the governed platform to the in-repo DQ service (ADR-013) through adapters. Select agents by
pipeline DQ profile and checkpoint, persist results as platform evidence, and turn CRITICAL/WARNING outcomes
into MonitoringEvents. The backend contains **mapping and orchestration only** (invariant 10).

## In scope
- `backend/dq/contracts.py` re-exports the S03 DQ contracts. `DQAgentAdapter` Protocol (src §23.1).
- `backend/dq/registry.py` `DQAgentRegistry` (logical names per architecture §18 C9).
- `backend/dq/adapters/custom_dq.py`, `recon.py`: build an `AgentRunRequest` from pipeline metadata
  (dataset refs for source/target from `pipeline.source/target`, business rules from S04, keys/compare columns from
  `dq_profile` params), call `DQServiceClient` (S26), and map `AgentRunResult`/`DQFinding` → `DQAgentResult`.
- `backend/dq/orchestrator.py` `DQOrchestrator.run(pipeline_id, run_id, checkpoint, rule_ids=None, request_key)`:
  select enabled agents from `dq_profile[checkpoint]`; aggregate (any blocking CRITICAL → CRITICAL; ERROR never folds
  into PASS); persist; emit MonitoringEvents (`source=DQ`, `rule_id`, `expected`, `actual`, evidence with
  `dq_run_id`/`dq_result_id`/`dq_service_job_id` refs) via S13.
- Migration: `dq_runs` (id, request_key UNIQUE, pipeline_id, run_id, checkpoint, status, started_at, finished_at, error)
  and `dq_results` (id, dq_run_id, agent, rule_id, status, blocking, expected jsonb, actual jsonb, failed_row_count,
  evidence jsonb (bounded, masked), dq_service_job_id, report_ref, created_at).
- APIs: `POST /dq/run` (`service` or operator; `wait=true` default with `DQ_SYNC_TIMEOUT_S`, else 202),
  `GET /dq/runs`, `/dq/runs/{id}`, `/dq/runs/{id}/results`.
- `integrations/airflow/dq_checkpoint.py`: callable/operator that fails the Airflow task on a blocking CRITICAL.
- S04 validator extension: `dq_profile` agent names must be registered adapters. Rules must validate against the DQ
  service rule catalog schema (published by `/v1/agents`, snapshot committed for offline validation).
- `tests/fakes/dq_service.py` (respx) for downstream slices, plus consumer contract tests pinned to the DQ service OpenAPI snapshot.

## Out of scope
DQ Monitor scheduling and the other adapters (S18). Verification routing (S20). Workbench (S34).

## Acceptance criteria
- **S17-AC1** Canonical via the real DQ service on seeded compose DBs: the TARGET checkpoint yields a `DQAgentResult` CRITICAL for `BR-CUST-002` with expected `{duplicate_count: 0}` and actual `{duplicate_count: 417}`. The RECONCILIATION checkpoint yields CRITICAL with source 100,000, target 100,417 and diff 417.
- **S17-AC2** Adapters contain no DQ computation: they only build requests and map responses (code review checklist plus a test asserting that adapter modules import nothing from numpy/scipy/sklearn/duckdb).
- **S17-AC3** Agent selection strictly follows `dq_profile[checkpoint]`. Disabled agents do not run. Unknown agent names fail metadata validation.
- **S17-AC4** `dq_runs`/`dq_results` are persisted with rule, agent and DQ-service job references. Evidence is size-bounded and PII-masked.
- **S17-AC5** A blocking CRITICAL → one MonitoringEvent (`source=DQ`) → one incident (with S15), even when `/dq/run` is retried with the same request key.
- **S17-AC6** DQ service unavailable, timeout or job ERROR → `dq_run` ERROR and a `DQ_AGENT_ERROR` WARNING event. It is never PASS. The Airflow checkpoint helper fails closed for blocking checkpoints (configurable).
- **S17-AC7** `rule_ids=[…]` is forwarded and only those rules are evaluated (asserted on the request and the result).
- **S17-AC8** `POST /dq/run` is idempotent on `request_key`. `wait=false` returns 202 with a pollable `dq_run_id`.
- **S17-AC9** Consumer contract tests pass against the committed DQ service OpenAPI snapshot. A breaking API change fails CI.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1 | integration (compose: DBs + DQ service) | `tests/integration/dq/test_canonical_checkpoints.py` |
| AC2, AC3, AC7 | unit | `tests/unit/dq/test_adapters.py`, `test_orchestrator.py` |
| AC4–AC6, AC8 | integration | `tests/integration/dq/test_dq_run_api.py` |
| AC9 | contract | `tests/contract/dq/test_dq_service_contract.py` |

## Rollback
Disable DQ in the pipeline metadata (`monitoring.dq.enabled=false`) and remove the checkpoint tasks. Revert code.

## Open issues
- From S03 review: map `DQFinding.check_id` → `DQAgentResult.check_id` and set `MonitoringEvent.metric` for profiling/statistical/anomaly findings without a rule. `DQRunResult` rejects PASS with no results, and a blocking ERROR forces run status CRITICAL/ERROR.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (BLOCKED on OQ-01)
- 2026-09-30 — unblocked: OQ-01 resolved by ADR-013 (DQ suite built in-repo). Re-scoped to adapters over the DQ service (S26, S30, S31). Interface-assessment step removed.
