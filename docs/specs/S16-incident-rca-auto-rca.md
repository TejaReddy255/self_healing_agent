---
id: S16
title: Incident-driven RCA and auto_rca (AM-09; META-07, META-08)
status: DRAFT
phase: 3
owner_agent: ai-engineer
reviewers: [code-reviewer, evaluation-engineer, security-reviewer, devops-sre]
depends_on: [S09, S10, S15]
blocked_by: []
traces: [FR-030, FR-032, FR-083, NFR-005, NFR-010, UC-AC9]
source_sections: ["src §21.4 steps 81–82", "src §21.13", "src §24", "src §33.9", "src §33.12", "src §35.5"]
tags: [ai, schema, runtime, security-sensitive]
approved_by:
approved_on:
---

# S16 — Incident-driven RCA and auto_rca

## Objective
Route incidents into the existing DiagnosticService through a durable analysis job, either manually or
automatically per pipeline policy. Drive incident status from the analysis, review and job lifecycle.

## In scope
- `IncidentContextBuilder` (src §33.9) extends S09's builder. `current_verified_evidence` adds the
  linked monitoring events (by ID) and DQ results once S17 exists. `historical_information` items are labelled
  "reference only — not current evidence".
- `DiagnosticService.analyze_incident(incident_id, actor)` shares the S09 code path and differs only in context building.
- Migration: `analysis_jobs` (id, incident_id, status, attempts, lease…, analysis_id, error) with a partial unique
  index on `incident_id` where status in (PENDING, PROCESSING).
- `backend/jobs/analysis_worker.py` uses the S10 job base.
- `POST /incidents/{id}/analyze` (operator) → 202 `{analysis_job_id}` (the existing job if one is active).
- auto_rca: on incident creation with CRITICAL severity and `self_healing.auto_rca=true`, enqueue within the
  incident transaction. The monitoring cycle never awaits RCA.
- `IncidentLifecycleListener` subscribes to domain events: `AnalysisStarted` → INVESTIGATING; `AnalysisCompleted`
  → RCA_COMPLETED; `ReviewCreated` → AWAITING_APPROVAL; `ReviewApproved` → APPROVED; job stage events via
  `STAGE_TO_INCIDENT_STATUS` (S03); `AnalysisFailed` → FAILED(`RCA_FAILED`); `ReviewRejected` → REJECTED.
  All transitions go through `IncidentService.transition`.
- Reviews, analyses and jobs created from an incident carry `incident_id`.
- Evals E-INJ-04..06 (known-issue text, runbook text, DQ evidence sample).

## Out of scope
Verification/resolution (S19/S20).

## Acceptance criteria
- **S16-AC1** Manual (`/incidents/{id}/analyze`) and auto RCA both call `DiagnosticService.analyze_incident` (spy). Both produce the same context structure as S09 plus incident sections.
- **S16-AC2** `auto_rca=false` → the incident stays DETECTED and no job is enqueued. `auto_rca=true` → exactly one job, even when 10 duplicate CRITICAL events arrive concurrently.
- **S16-AC3** Known issues, runbooks and historical incidents never appear under `current_verified_evidence`, and are labelled as reference only (snapshot test).
- **S16-AC4** Table-driven lifecycle test: the incident status follows every hop from DETECTED to AWAITING_APPROVAL and on through the job-stage mapping. Invalid hops are rejected and logged, not forced.
- **S16-AC5** A monitoring cycle with auto_rca enabled completes in normal time while a slow fake agent runs (enqueue only).
- **S16-AC6** Replay evals E-INJ-01..06 meet the blocking metrics (0 out-of-scope tool calls or plans).
- **S16-AC7** A worker crash during an analysis job → the job is reclaimed and re-run after lease expiry (safe because the job is read-only). The incident does not get stuck in INVESTIGATING beyond `lease + max_attempts`.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC3 | unit + integration | `tests/integration/services/test_incident_rca.py`, `tests/unit/metadata/test_incident_context.py` |
| AC2, AC5, AC7 | integration | `tests/integration/jobs/test_analysis_jobs.py` |
| AC4 | integration | `tests/integration/services/test_incident_lifecycle.py` |
| AC6 | eval (replay) | `tests/evals/test_injection.py` |

## Rollback
Set `self_healing.auto_rca=false` in metadata (reload) to stop automatic RCA. Revert code.

## Open issues
- From S04 review: persist the metadata `version` used to build each incident's RCA context.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
