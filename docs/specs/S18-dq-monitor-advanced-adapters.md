---
id: S18
title: DQ Monitor and platform adapters for profiling, statistical, anomaly and preprocessing
status: DRAFT
phase: 4
owner_agent: data-engineer
reviewers: [code-reviewer, data-engineer]
depends_on: [S14, S17, S28, S29, S32, S33]
blocked_by: []
traces: [FR-095, FR-096]
source_sections: ["src §21.12", "src §23.2", "src §35.4"]
tags: []
approved_by:
approved_on:
---

# S18 — DQ Monitor and platform adapters

## Objective
Replace the S14 DQ monitor placeholder with a real DQMonitor that runs scheduled or deep checkpoints through the
orchestrator. Add platform adapters for the remaining agents, running expensive checks only on their configured
schedule or trigger.

## In scope
- `backend/monitoring/dq_monitor.py`: for each pipeline with `monitoring.dq.enabled`, run due checkpoints. Due-ness comes
  from `dq_profile` entries' `schedule` (e.g. `daily@02:00`, `every 6h`) or `trigger` (`on_profile_change`, `on_warning`).
  The last run comes from `dq_runs`.
- Adapters (request building and mapping only): `profiling.py` (profile + compare against the last accepted baseline job),
  `statistical.py` (baseline = last accepted profile/sample), `anomaly.py` (score with the model version pinned in
  `dq_profile` params; pattern detection over platform metric series such as daily row counts from `dq_results`),
  `preprocessing.py` (runs only as a declared prerequisite).
- The `on_profile_change` trigger compares consecutive profile hashes (S29).
- A non-blocking WARNING creates an incident only if its class is in `MONITORING_INCIDENT_WARNING_CLASSES`.
- Baseline management: `dq_baselines` (pipeline_id, dataset, agent, job_ref, accepted_by, accepted_at). An operator
  accepts a profile as the baseline via `POST /dq/baselines` (operator role, audited).

## Acceptance criteria
- **S18-AC1** DQMonitor emits events only from orchestrated results (no recomputation). The event count equals the number of non-PASS findings.
- **S18-AC2** Statistical and anomaly checks do not run on every cycle. They honour schedule and trigger (fake-clock test across 48 simulated cycles).
- **S18-AC3** A non-blocking WARNING → a WARNING event, with no incident unless the class is configured.
- **S18-AC4** Each adapter passes consumer contract tests against the DQ service OpenAPI snapshot and recorded responses (PASS and FAIL).
- **S18-AC5** Preprocessing runs only when a selected downstream agent declares it as a prerequisite.
- **S18-AC6** Anomaly scoring in platform mode fails with an explicit ERROR (not PASS) when the pinned model version is missing or its schema mismatches.
- **S18-AC7** Baseline acceptance is role-protected and audited. Checks without an accepted baseline report `skipped{reason: no_baseline}`.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1–AC3, AC5, AC6 | unit (fake clock, fake DQ service) | `tests/unit/monitoring/test_dq_monitor.py` |
| AC4 | contract | `tests/contract/dq/test_advanced_adapters.py` |
| AC7 | integration | `tests/integration/api/test_dq_baselines.py` |

## Rollback
Remove the schedule entries from `dq_profile` and reload metadata. Revert code.

## Open issues
- From S04 refinement: `dq_profile` entries need a `trigger` field (`on_profile_change`, `on_warning`). Add it here as an additive optional field on the S03 `DQProfileEntry` (not part of the approval hash) and update the contract snapshot. Until then, S04 metadata validation rejects a `trigger` key as `SCHEMA_INVALID`.
- From S04 review: validate `dq_profile` entry `schedule` strings (e.g. `daily@02:00`, `every 6h`) in the metadata validator, so bad schedules fail at load time, not at runtime.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (BLOCKED on OQ-01)
- 2026-09-30 — unblocked by ADR-013. Adapters target the in-repo DQ service. Added baseline management.
