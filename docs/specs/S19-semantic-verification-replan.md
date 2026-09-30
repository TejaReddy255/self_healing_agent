---
id: S19
title: Semantic technical verification and execution replan
status: DRAFT
phase: 5
owner_agent: backend-engineer
reviewers: [code-reviewer, security-reviewer, evaluation-engineer, data-engineer]
depends_on: [S11]
blocked_by: []
traces: [FR-033, FR-052, FR-055, FR-060, UC-AC5]
source_sections: ["src §10.2 (interpretation)", "src §21.17", "src §25", "src §25.1", "src §35.6"]
tags: [ai, security-sensitive, schema]
approved_by:
approved_on:
---

# S19 — Semantic technical verification and replan

## Objective
Verify the actual post-remediation state against the typed assertions that the human approved (ADR-010),
deterministically and without an LLM. When a precondition or verification fails, produce a replanned
revision that requires fresh approval.

## In scope
- `backend/services/verification_service.py` `VerificationService.evaluate(assertions, ctx) -> list[AssertionResult]`:
  executes each assertion kind via **read** tools of the target server (S06 `diagnostic_toolset` subset,
  never the execution toolset), compares with `expected.op` (`eq, ne, lt, lte, gt, gte`), and records
  `expected`, `actual`, `status` and `evidence`.
- Assertion kinds → tools: `duplicate_count` → `find_duplicates`; `row_count` → `get_row_count`;
  `sql_scalar` → `run_select` (single scalar; read-only validated); `object_exists`/`object_count` → S3 read
  tools; `document_count` → Mongo `count` (S24 required for these kinds; otherwise plan validation rejects them).
- Migration: `verification_results` (id, job_id, stage PRECHECK|TECHNICAL_VERIFY|DQ_VERIFY|FINAL_DQ,
  assertion_id or rule_id, status, expected jsonb, actual jsonb, evidence jsonb, evaluated_at).
- Remediation stages: PRECHECK evaluates `precondition_assertions` (mismatch → REPLAN_REQUIRED before any
  write). TECHNICAL_VERIFY = structural gate (S11) **AND** all assertions PASS.
- Plan validator (S09 N5) is tightened: an executable plan requires ≥ 1 verification assertion whose kind is supported by an available server.
- Execution Replan Agent (`execution_replan@1`, no tools): input = approved payload + stage results +
  verification evidence. Output passes W2 pre-checks and N5, then `ReviewService.apply_replan` →
  revision N+1 (`source=REPLAN`), new hash, approval cleared, review PENDING, incident AWAITING_APPROVAL
  (via S16 listener if present).
- Evals E-RPL-01..03 and E-EXE updates.

## Acceptance criteria
- **S19-AC1** Assertion evaluation performs zero model calls (spy) and only READ tool invocations (audit check).
- **S19-AC2** Canonical: after deduplication, `duplicate_count(dw.customer, [customer_id]) eq 0` and `row_count(dw.customer) eq 100000` → PASS → the job proceeds. With one duplicate left → REPLAN_REQUIRED and no recovery trigger.
- **S19-AC3** A precondition mismatch at PRECHECK (e.g. expected 417 duplicates, found 0) → REPLAN_REQUIRED before any write tool call.
- **S19-AC4** A `sql_scalar` assertion that is not read-only is rejected at plan validation and again at evaluation.
- **S19-AC5** A replan creates revision N+1 with a new hash, `approved_payload_hash = NULL` and status PENDING. The previous approval cannot execute the new revision (attempted enqueue → HASH_MISMATCH).
- **S19-AC6** `verification_results` rows are persisted for every assertion and appear in `GET /execution-jobs/{id}` (and in the incident timeline when S15 is present).
- **S19-AC7** Replay evals E-RPL meet their thresholds. The replan never proposes a different target system than the approved plan unless the evidence names it, and even then W2 pre-checks flag it for the reviewer.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC4 | unit | `tests/unit/services/test_verification_service.py` |
| AC2, AC3, AC6 | integration (compose DBs or fakes) | `tests/integration/jobs/test_semantic_verification.py` |
| AC5 | integration | `tests/integration/services/test_replan_revision.py` |
| AC7 | eval (replay) | `tests/evals/test_replan.py` |

## Rollback
Revert code. Plans remain valid (assertions are already part of the S03 contract).

## Open issues
- From S03 review: assertion `resource`/`key_columns` match an identifier pattern, but tools must still quote identifiers and check them against pipeline metadata. Deny `$where`, `$function` and `$accumulator` in Mongo filters server-side too (the contract already rejects them). Assertion IDs are unique per plan, which is the `verification_results` key.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
