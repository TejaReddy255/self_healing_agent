---
id: S10
title: HITL reviews, revisions and SOP governance
status: DRAFT
phase: 2
owner_agent: backend-engineer
reviewers: [code-reviewer, security-reviewer, evaluation-engineer, data-engineer, devops-sre]
depends_on: [S09]
blocked_by: []
traces: [FR-040, FR-041, FR-042, FR-043, FR-044, FR-045, FR-046, NFR-010, NFR-040, NFR-041, UC-AC3]
source_sections: ["src §4.6", "src §9", "src §11", "src §14.2", "src §15", "src §16 (revision restart recovery)"]
tags: [ai, security-sensitive, schema, runtime]
approved_by:
approved_on:
---

# S10 — HITL reviews, revisions and SOP governance

## Objective
Turn executable analyses into governed reviews. Bind approval to the exact revision the reviewer saw
(ADR-006). Process human edits as durable, restart-safe revision jobs validated by the Review
Revision Agent. Govern SOP matching, generation and installation (ADR-008).

## In scope
- Migration: `remediation_reviews` (id, analysis_id, pipeline_id, dag_id, run_id, task_id, incident_id,
  scope_key, status, current_revision, payload_hash, approved_payload_hash, approved_by, approved_at,
  approval_comment, rejected_by/at/reason, target_system, risk_level, created_at, updated_at, version);
  partial unique index `(scope_key)` where status in (PENDING, APPROVED, EXECUTING);
  `remediation_review_revisions` (review_id, revision, payload jsonb, payload_hash, source HUMAN|AI|REPLAN,
  ai_summary, ai_notes, created_by, created_at; PK (review_id, revision); trigger blocks UPDATE/DELETE);
  `review_revision_jobs` (id uuid, review_id, base_revision, submitted_payload, status, stage, attempts,
  max_attempts, lease_owner, lease_expires_at, available_at, result, error, timestamps);
  `approved_sops` (sop_id, review_id, title, content jsonb, sha256, risk_level, target_system, approved_by, approved_at).
- `backend/jobs/base.py`: the generic claim/heartbeat/reaper pattern (architecture §9), reused by S11/S16/S20.
- `backend/services/review_service.py`: `create_from_analysis` (listens for `AnalysisCompleted` with `executable_plan`),
  `approve(review_id, ApproveRequest, actor)` (CAS on status, revision and hash; 409 if a revision job is PENDING/PROCESSING),
  `reject`, `apply_accepted_revision` (insert revision N+1, new hash, clear approval, status PENDING).
  The approve response in this slice is `{review, execution_job_id: null}`. S11 enqueues in the same transaction.
- `backend/services/revision_service.py` + `backend/jobs/revision_worker.py`: W2 deterministic pre-checks,
  the Review Revision Agent (`review_revision@1`), W3 SOP Generation Agent (`sop_generation@1`) when NO_MATCH.
- `backend/sop/resolver.py` `SOPResolver`: static APPROVED SOPs (S04) ∪ `approved_sops`. Resolution
  order: explicit reference → unique reference in the analysis → none (the legacy single-document fallback is dropped).
  `install_on_approval(review)` runs in the approval transaction.
- APIs per architecture §6 (reviews, revisions, audit, revision-jobs, approve, reject).
- Evals E-REV-01..08 and E-SOP-01..03 (replay).

## Out of scope
Execution (S11). Replan (S19).

## Configuration
`REVISION_JOB_LEASE_S=300`, `REVISION_JOB_MAX_ATTEMPTS=3`, `JOB_REAPER_INTERVAL_S=30`,
`REQUIRE_DISTINCT_APPROVER=false` (OQ-11).

## Acceptance criteria
- **S10-AC1** An analysis with an executable plan creates a PENDING review at revision 1 whose `payload_hash` equals `payload_hash(ApprovalPayload)` of the persisted revision.
- **S10-AC2** Approve with a stale `expected_revision` or `expected_payload_hash` → 409, with the state unchanged and the attempt audited.
- **S10-AC3** Concurrent approvals of the same review → exactly one succeeds.
- **S10-AC4** Approve while a revision job for the review is PENDING or PROCESSING → 409.
- **S10-AC5** An accepted revision inserts revision N+1 with a new hash, clears `approved_payload_hash`, and sets status PENDING. Existing revision rows cannot be updated or deleted (DB trigger test).
- **S10-AC6** Unsafe edits are rejected by deterministic pre-checks without a model call where possible (scope widening, removed stop conditions, raised `max_affected_rows`, swapped target, edits outside the payload fields). Replay evals E-REV meet their thresholds.
- **S10-AC7** Killing the worker mid-job → after lease expiry the job is reclaimed and completes. Attempts are counted. Exceeding the maximum → FAILED with an error.
- **S10-AC8** A second active review for the same scope is prevented by the unique index. The service returns the existing review ID.
- **S10-AC9** A proposed SOP is installed exactly once, in the approval transaction, with SHA-256 and approver. `SOPResolver` then returns it, and static metadata files are untouched.
- **S10-AC10** Revise, approve and reject require the reviewer role. Every decision is in `operation_audit` with the actor, review ID and revision.
- **S10-AC11** With `REQUIRE_DISTINCT_APPROVER=true`, an approval by the RCA requester, or by the submitter of the active revision, → 403 `DISTINCT_APPROVER_REQUIRED`. With the default `false`, the same approval succeeds and is audited.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC5, AC8, AC9 | integration | `tests/integration/services/test_review_service.py` |
| AC2–AC4 | integration | `tests/integration/api/test_approve_cas.py` |
| AC6 | unit + eval | `tests/unit/services/test_revision_prechecks.py`, `tests/evals/test_revision.py` |
| AC7 | integration | `tests/integration/jobs/test_revision_worker_recovery.py` |
| AC10 | integration | `tests/integration/api/test_review_rbac.py` |

## Security and eval considerations
The approval CAS and the revision immutability are release-blocking safety controls. The security review focuses on TOCTOU between approval and revision jobs.

## Rollback
Revert code. The tables are additive. Pending reviews remain readable.

## Open issues
- From S03 review (ADR-006): persist the canonical bytes (`canonical_json`) with each revision. Verify `sha256(stored bytes)` and `canonical_json(parse(stored)) == stored`, and fail closed. Store the payload as bytes or `json`, not only `jsonb`, because jsonb can renormalise numbers such as `1e20`.
- From S03 review: a W2 ACCEPT's `validated_payload` must be byte-identical (canonical) to the submitted human edit, so the model cannot silently rewrite it. Add `RejectRequest{reason}`, `RevisionSubmitRequest{base_revision, payload}` and a revision `source` enum as additive contracts.
- From S03: `SOPReference.content_sha256` exists. Fill it from `approved_sops.sha256` or the YAML content hash when a review is created.
- From S04 (decision 2026-10-01): the job worker process runs `backend.metadata.refresher.MetadataRefresher` so its metadata snapshot follows reloads within `METADATA_REFRESH_INTERVAL_S`.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
- 2026-09-30 — OQ-06 (DB-stored SOPs) and OQ-11 (configurable distinct approver) decided; added config and S10-AC11.
