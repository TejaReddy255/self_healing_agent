---
id: S15
title: Incidents — correlation, deduplication, state machine, timeline (AM-08)
status: DRAFT
phase: 3
owner_agent: backend-engineer
reviewers: [code-reviewer, data-engineer, solution-architect]
depends_on: [S13, S14]
blocked_by: []
traces: [FR-080, FR-081, FR-082, FR-084, FR-085, NFR-012, NFR-040, UC-AC6]
source_sections: ["src §21.6", "src §21.7", "src §21.8", "src §24", "src §35.6"]
tags: [schema, contract]
approved_by:
approved_on:
---

# S15 — Incidents

## Objective
Make the incident the first-class, deduplicated owner of an operational issue's lifecycle, with
enforced transitions and a reference-based timeline.

## In scope
- Migration: `incidents` (id uuid, incident_key, fingerprint_version, pipeline_id, dag_id, run_id,
  category, severity, status, status_reason, current_analysis_id, current_review_id, current_job_id,
  recurrence_of, detected_at, last_seen_at, resolved_at, version); partial unique index on `incident_key`
  where status NOT IN (RESOLVED, REJECTED); `incident_events` (incident_id, monitoring_event_id,
  relation_type TRIGGER|RELATED, attached_at; PK pair); `incident_transitions` (id, incident_id,
  from_status, to_status, actor_subject, actor_role, reason, occurred_at).
- Add FKs (`NOT VALID` then `VALIDATE`) from `incident_id` on analyses, remediation_reviews,
  remediation_jobs, operation_audit and activities, **for each of these tables that exists at merge
  time**. Any slice that later creates one of these tables adds its own FK to `incidents` (parallel-track rule).
- `backend/services/incident_service.py`:
  - `create_or_update(event)`: fingerprint `fp_v1 = sha256(pipeline_id|run_id or time_bucket|source|event_type|resource|rule_id or metric)`
    (src §21.6/§24.2). Category mapping from source/event_type. Attach event. Update `last_seen_at`.
    Severity escalates and never downgrades automatically. On a unique-violation race → retry the lookup.
  - When the matching incident is terminal → new incident with `recurrence_of`.
  - `transition(incident_id, to, actor, reason)`: validated by `can_transition` (S03), CAS on `version`, history row.
- `IncidentSink` implementation wired into the S14 orchestrator and the S13 ingest hook (CRITICAL plus configured WARNING classes).
- APIs: `GET /incidents` (filters: status, severity, category, pipeline_id; pagination), `GET /incidents/{id}`
  (with linked event IDs and current analysis/review/job IDs), `GET /incidents/{id}/timeline` (ordered, typed
  entries: event, transition, analysis, review, revision, job stage, audit summary; references plus short
  labels, not copied payloads), `POST /incidents/{id}/dismiss` (operator; DETECTED/RCA_COMPLETED → REJECTED with a reason).

## Out of scope
Triggering RCA (S16). Resolution chain (S20).

## Configuration
`INCIDENT_TIME_BUCKET_S=3600` (for run-less events).

## Acceptance criteria
- **S15-AC1** 10 distinct events with the same fingerprint → 1 incident, 10 `incident_events` links, and `last_seen_at` = the latest event time.
- **S15-AC2** A different `run_id` or `rule_id` → separate incidents.
- **S15-AC3** Invalid transitions (e.g. DETECTED→RESOLVED, EXECUTING→RESOLVED) → 409/`InvalidTransition` with no change. Every valid transition writes an `incident_transitions` row with the actor and reason.
- **S15-AC4** 20 concurrent `create_or_update` calls for the same new fingerprint → exactly 1 incident.
- **S15-AC5** The same fingerprint after RESOLVED/REJECTED → a new incident with `recurrence_of` set.
- **S15-AC6** The timeline returns entries in chronological order with IDs for every referenced object, and no raw evidence copies beyond labels.
- **S15-AC7** Severity escalates WARNING→CRITICAL on a later CRITICAL event and never downgrades.
- **S15-AC8** Dismiss requires the operator role and a non-empty reason, and is allowed only from DETECTED or RCA_COMPLETED.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC2, AC5, AC7 | integration | `tests/integration/services/test_incident_dedup.py` |
| AC3, AC8 | integration | `tests/integration/services/test_incident_transitions.py` |
| AC4 | integration | `tests/integration/services/test_incident_concurrency.py` |
| AC6 | integration | `tests/integration/api/test_incident_timeline.py` |

## Rollback
Revert code. Tables and FKs are additive (drop the FKs first in a compensating migration if needed).

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
