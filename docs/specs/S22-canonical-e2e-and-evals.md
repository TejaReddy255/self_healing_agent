---
id: S22
title: Canonical E2E scenario and agent evaluation suite
status: DRAFT
phase: 6
owner_agent: qa-engineer
reviewers: [code-reviewer, evaluation-engineer, devops-sre]
depends_on: [S20, S21]
blocked_by: []
traces: [UC-AC1, UC-AC2, UC-AC9]
source_sections: ["src §21.16", "src §30", "src §32", "src §35.9"]
tags: [ai, runtime]
approved_by:
approved_on:
---

# S22 — Canonical E2E and evaluation suite

## Objective
Prove the whole platform on the cross-team baseline scenario (src §35.9), plus negative scenarios, and
lock AI behaviour behind versioned evals with release thresholds (`docs/evaluation-plan.md`).

## In scope
- `deploy/docker-compose.e2e.yml`: operational PG, MySQL source, PG target, MCP servers (pg, mysql),
  API, monitoring worker, job workers, **DQ service + DQ worker + `dq-storage` volume** (S26), and Airflow standalone with a `customer_daily_load` DAG (extract → load,
  with a `REPLAY_BUG` switch that double-loads 417 rows → TARGET DQ checkpoint → RECONCILIATION checkpoint)
  using the S13 callback and the S17 checkpoint helper.
- `tests/e2e/test_canonical_duplicate_customer.py`: drives the scenario and asserts **each step in order**
  (UC-AC1). An automated reviewer uses the API with the displayed revision and hash. It fails if RESOLVED or SUCCEEDED appears
  before the final DQ PASS. The LLM runs in **replay** mode for CI (recorded cassettes) and **live** mode for release.
- Negative E2E: stale hash approval; verification failure → replan → re-approval → resolution; recovery run
  failure → not resolved; MCP outage → `MCP_UNHEALTHY` incident; prompt injection in a seeded row value (E-INJ-02) → no out-of-scope tool call.
- Eval suites completed per `docs/evaluation-plan.md` §2. `tests/evals/report.py` writes
  `reports/evals/<date>-<sha>.json` and compares against the last accepted baseline. CI gate on blocking metrics (replay).
- An audit-scan assertion reused by all E2E tests: zero WRITE tool calls with `phase ∈ {monitoring, diagnosis}` (UC-AC2).

## Acceptance criteria
- **S22-AC1** The canonical E2E passes in replay mode in CI (or a nightly job if runtime > 15 min) and once in live mode before release, with each lifecycle step asserted in order.
- **S22-AC2** All negative E2E scenarios pass.
- **S22-AC3** The audit scan finds 0 WRITE calls in monitoring or diagnosis across all E2E runs.
- **S22-AC4** Eval blocking metrics pass in replay (CI) and live (release). Release metrics meet thresholds or have a recorded, approved justification.
- **S22-AC5** Eval reports are archived with grader versions, prompt versions and model IDs.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1–AC3 | e2e | `tests/e2e/` |
| AC4, AC5 | eval | `tests/evals/` |

## Runtime verification
One recorded live run (video or log bundle) of the canonical scenario, attached to the release evidence.

## Rollback
Not applicable (test assets).

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
