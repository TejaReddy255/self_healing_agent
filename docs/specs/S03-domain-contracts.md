---
id: S03
title: Shared domain contracts and canonical hashing
status: DONE
phase: 0
owner_agent: backend-engineer
reviewers: [code-reviewer, solution-architect, security-reviewer]
depends_on: [S01]
blocked_by: []
traces: [FR-031, FR-041, FR-081, NFR-060]
source_sections: ["src §6.2", "src §9.2", "src §21.3", "src §22.2", "src §23", "src §25", "src §35.6"]
tags: [contract, security-sensitive]
approved_by: product owner
approved_on: 2026-09-30
---

# S03 — Shared domain contracts and canonical hashing

## Objective
Freeze v1 of the cross-team contracts in `backend/domain/`, including the canonical approval-payload hash,
so parallel tracks can build against stable types. This is "contract freeze v1" for the P0 exit.

## Read first
`docs/architecture.md` §6, §7, §18 (C1, C6, C9), ADR-006, ADR-010; `docs/agent-design.md` §1, §4.

## In scope
- `backend/domain/common.py`: `SystemName` (postgresql, mysql, s3, mongodb), `Severity` (INFO, WARNING,
  CRITICAL), `EvidenceStatus` (OBSERVED, VERIFIED, INFERRED, PROPOSED, EXECUTED, VERIFIED_FIXED),
  `Confidence` (low, medium, high), UTC datetime type, ID aliases.
- `analysis.py`: `PipelineFailureRequest`, `Finding`, `Recommendation`, `AnalysisSummary`,
  `StructuredAnalysis` (fields per src §6.2 plus `evidence_refs` on findings).
- `remediation.py`: `RemediationPlan` {target_system, target_resources, risk_level, preconditions,
  stop_conditions, steps, expected_write_operations, write_tools?, max_affected_rows?,
  retry_airflow_after_success, precondition_assertions, verification_plan: {assertions}, verification_rules:
  list[DQRuleRef]}; `VerificationAssertion` discriminated union (`duplicate_count`, `row_count`,
  `sql_scalar`, `object_exists`, `object_count`, `document_count`) with `expected: {op, value}`.
- `sop.py`: `SOPProposal`, `SOPMatchStatus` (MATCHED, NO_MATCH, PARTIAL), `SOPReference`.
- `review.py`: `ApprovalPayload` {sop_match_status, matched_sop_reference, proposed_sop, proposed_remediation},
  `ReviewStatus` (§7.1), `ReviewRevisionResult`, `ApproveRequest{expected_revision, expected_payload_hash, comment}`.
- `hashing.py`: `canonical_json(obj) -> bytes` and `payload_hash(payload: ApprovalPayload) -> str`.
  Rules: `model_dump(mode="json", exclude_none=False)`; keys sorted; separators `(",", ":")`; UTF-8;
  `ensure_ascii=False`; NaN/Infinity rejected; datetimes ISO-8601 UTC with `Z`; Decimals as strings.
- `monitoring.py`: `MonitoringEvent` (union of src §21.3/§22.2 per C6, including `event_id`, `idempotency_key`,
  `resource`, `rule_id`), `PipelineHealthResult{pipeline_id, overall_status, events, monitor_status, evaluated_at}`.
- `incident.py`: `IncidentStatus`, `IncidentCategory` (AIRFLOW_FAILURE, DATA_QUALITY, SLA_BREACH,
  DEPENDENCY_OUTAGE, OTHER), `ALLOWED_TRANSITIONS` (architecture §7.4), `can_transition(a, b)`, `TERMINAL_STATUSES`.
- `dq.py`: `Checkpoint` (SOURCE, STAGING, TARGET, RECONCILIATION, SCHEDULED), `DQStatus` (PASS, WARNING,
  CRITICAL, ERROR), `DQRuleRef`, `DQExecutionContext`, `DQAgentResult`, `DQRunResult`.
- `jobs.py`: `JobStatus`, `RemediationStage` (architecture §7.3), `STAGE_TO_INCIDENT_STATUS` (§7.5).
- `python -m backend.domain.export_schemas` writes `docs/contracts/v1/*.schema.json`.

## Out of scope
Metadata document models (S04). Persistence. Behaviour.

## Public contracts
As listed. All models are `frozen=True` where they represent persisted facts (events, results, approved payloads).
`extra="forbid"` on all inbound API models.

## Persistence and migrations
None.

## Acceptance criteria
- **S03-AC1** Fixture files reproducing the examples in src §21.3, §23.3 and §33.5 (rule/SOP shapes as referenced by plans) validate against the models.
- **S03-AC2** `payload_hash` is identical across key order, whitespace, dict insertion order and separate processes. Golden vectors in `tests/contract/hash_vectors.json` match. Changing any single field changes the hash.
- **S03-AC3** `RemediationPlan` rejects an unknown `target_system`, unknown assertion kinds, and a missing `expected`. `ApprovalPayload` rejects a `proposed_remediation` with an empty `verification_plan.assertions` when `expected_write_operations` is non-empty. *(Amended 2026-09-30, product owner decision: the rule moved from `RemediationPlan` to `ApprovalPayload`, so an RCA with an incomplete plan keeps its findings and S09 N5 marks the plan non-executable, as S09-AC4 expects.)*
- **S03-AC4** `can_transition` encodes architecture §7.4 exactly (table-driven test over all pairs). `DETECTED→RESOLVED` and `EXECUTING→RESOLVED` are false.
- **S03-AC5** JSON Schemas are exported and snapshot-tested. Any schema change fails CI until the snapshot is updated deliberately.
- **S03-AC6** No module in `backend/domain` imports from other `backend.*` packages (import-lint test).
- **S03-AC7** `STAGE_TO_INCIDENT_STATUS` covers every `RemediationStage` value. *(Strengthened during review:)* every architecture §7.3 edge, including "any stage → FAILED | REPLAN_REQUIRED" and a skipped recovery, maps to a legal §7.4 incident transition.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC3 | contract | `tests/contract/test_models.py` |
| AC2 | contract | `tests/contract/test_hashing.py` (+ subprocess check) |
| AC4, AC7 | unit | `tests/unit/domain/test_transitions.py` |
| AC5 | contract | `tests/contract/test_schema_snapshots.py` |
| AC6 | unit | `tests/unit/test_import_boundaries.py` |

## Runtime verification
Run the schema export and review the diff in `docs/contracts/v1/` with the human approver (contract freeze sign-off).

## Security and eval considerations
The hash is the approval boundary. Any change to canonicalization is a breaking change that needs an ADR, because it invalidates stored approvals.

## Rollback
Revert. No persisted data depends on it yet.

## Contract decisions (made during implementation and review)
All are reflected in `docs/contracts/v1/` and tested.

**Product owner decisions (2026-09-30)**
1. **AC3 placement.** "Writes need assertions" is enforced by `ApprovalPayload` (via `remediation.check_approvable`), not `RemediationPlan`. `write_tools` without `expected_write_operations` is also rejected there.
2. **State machine amendment** (architecture §7.4/§7.5 updated):
   - New edge APPROVED → REPLAN_REQUIRED, for a failed PRECHECK (src §25).
   - With recovery skipped (`retry_airflow_after_success=false`), a COMPLETED job leaves the incident in RECOVERY_RUNNING until an operator confirms recovery (`jobs.incident_status_for(..., recovery_skipped=True)`). RESOLVED stays reachable only from RECOVERY_RUNNING.

**Implementation choices within the spec**
- **All models are strict.** Every contract model is `frozen` and `extra="forbid"`, which is stricter than the spec asked. Sequences are tuples, so they are deeply immutable. `dict` fields are the exception: approval checks must hash the persisted bytes (S10/S11 open issues).
- **Approval payload versioning.** `ApprovalPayload.schema_version: Literal[1]` is part of the hash, so a later contract version is explicit and dispatchable. `SOPReference.content_sha256` pins SOP content.
- **Canonicalisation guard.** `hashing.canonical_json` also canonicalises the Python-mode dump and requires it to equal the JSON-mode dump. This rejects values pydantic would silently coerce: NaN→null, sets→lists, int keys→strings. Free-form fields (`filter`, `evidence`, `params`, DQ `expected`/`actual`) are finite `JsonValue` objects.
- **Text hygiene.** Text is NFC-normalised. Control, format and surrogate characters (bidi overrides, zero-width) are rejected, except tab and newline. Text is size-capped: 256 for short text, 10,000 for long text.
- **Identifiers.** Assertion `resource`/`key_columns` must be plain qualified names, and S3 buckets follow AWS naming. Mongo filters reject `$where`, `$function` and `$accumulator`.
- **Strict scalars.** Booleans are `StrictBool`. Counts and `max_affected_rows` are strict ints ≤ 2^53−1.
- **Consistency rules:**
  - Assertion IDs are unique per plan.
  - `ReviewRevisionResult` ACCEPT requires `validated_payload`.
  - `ApprovalPayload`/`StructuredAnalysis` enforce SOP status and reference consistency.
  - `DQRunResult` rejects PASS with no results or with ERROR/CRITICAL results, and requires CRITICAL/ERROR for any blocking CRITICAL/ERROR.
- **Added optional fields for downstream slices:** `MonitoringEvent.metric` and `DQAgentResult.check_id` (fingerprints for checks without a rule), and `DQProfile`/`DQProfileEntry` (C9 canonical shape, reused by S04).
- **Deviations from src examples (AC1):**
  - src §23.3 spells the agent `reconciliation`. C9 fixes it to `recon`, so the fixture uses `recon`, and a test shows the verbatim example is rejected with guidance.
  - The src §33.5 SOP's `sop_id`/`status` are catalogue fields (S04). The test maps them to `SOPReference` and validates the rest as `SOPProposal`.
  - The business-rule example is validated through `DQRuleRef` and `DQAgentResult.expected`. The rule-set document model is S04.
- **Naming.** `PipelineFailureRequest.workflow_id` is the Airflow `dag_id` (per S09; documented in the schema).

## Open issues
Handed to downstream specs (recorded in each spec's Open issues):
- **S05:** URL-encode run IDs.
- **S09:** N5 owns incomplete plans; reject healing-only claims from RCA.
- **S10:** persist canonical bytes; W2 ACCEPT must not rewrite the human edit; fill `content_sha256`; extra request models.
- **S11:** re-hash from persisted bytes; refuse unknown `schema_version`; `incident_status_for` via `can_transition`; optional `HealingReport`.
- **S13:** reject caller `event_id`; `metric`; DQ expected/actual as canonical JSON strings.
- **S17:** `check_id`/`metric` mapping.
- **S19:** identifier quoting and Mongo operator denial in tools.
- **S20:** operator recovery confirmation.
- ~~Contract freeze sign-off~~: **signed off by the product owner on 2026-10-01** (see Changelog).

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|
| Lint | `uv run ruff check .` / `uv run ruff format --check .` | pass | 89 files |
| Types | `uv run mypy backend frontend tools` | pass | strict, 58 files |
| S03 tests | `uv run pytest tests/contract tests/unit/domain tests/unit/test_import_boundaries.py` | 263 passed | AC1 10, AC2 11, AC3 9, AC4 5, AC5 4, AC6 2, AC7 4 tagged tests (+ parametrised cases) |
| Unit + contract suite | `uv run pytest -m "not integration and not e2e and not live"` | 372 passed | no regressions in S01/S02 |
| Integration | `uv run pytest -m integration` | 40 passed | dev Postgres on :55432 (S02 unaffected) |
| Traceability | `uv run python tools/trace.py --strict` | pass | S03-AC1..AC7 covered |
| Schema export | `uv run python -m backend.domain.export_schemas --check` | pass | 16 files in `docs/contracts/v1/` (15 models + `StateMachines`) |
| Cross-process hash | `test_hash_is_identical_in_separate_processes` | pass | PYTHONHASHSEED 0 and 4242 |
| Reviews | code-reviewer, security-reviewer, solution-architect | findings addressed | See review dispositions below |

**Review dispositions.**
- **Fixed:**
  - Canonicalisation bypass for model `Any` fields (code H1, security M1).
  - Stage-map vs transition-table conflict (code H2, architect M1; product owner decision).
  - Shallow freeze (tuples; code M1, security L4).
  - Vacuous or hidden DQ PASS (code M2, architect M2).
  - Payload versioning (code M3, architect H1).
  - Lax booleans (code M4, security L3).
  - Look-alike Unicode (security M2).
  - `write_tools` semantics (security M3).
  - Identifier, bucket and Mongo operator constraints (security M4, architect L2).
  - Size caps and the 2^53 limit (security L5).
  - AC3 placement (architect H2; product owner decision).
  - SOP content pin (architect H3).
  - `check_id`/`metric` (architect M3).
  - Unique assertion IDs (architect M4).
  - AC1 fixture deviations made explicit and tested (code/architect L).
- **Handed downstream:** see Open issues. Covers security L1, L2, L6; architect H1 persistence, M5, L1, L3, L7; code L float/jsonb.
- **Accepted as-is:**
  - Validation-mode-only schema snapshots.
  - The import lint not catching `importlib`.
  - `audit.py` style consistency (architect L8).

## Changelog
- 2026-09-29 — created (DRAFT)
- 2026-09-30 — approved by product owner ("approve S03 and implement it"). Status IN_PROGRESS.
- 2026-09-30 — implemented. Reviews by code-reviewer, security-reviewer and solution-architect were addressed. Product owner decided both conflicts: AC3 moved to `ApprovalPayload`, and architecture §7.4/§7.5 were amended. Status IN_REVIEW, pending acceptance and the contract freeze sign-off.
- 2026-10-01 — **Contract freeze v1 signed off** by the product owner. Confirmed as frozen: `retry_airflow_after_success` defaults to true; incident categories stay at the 5 v1 values; verification assertions stay at the 6 v1 kinds. From here, any change to `ApprovalPayload` or its nested models needs a new `schema_version` and an ADR.
- 2026-10-01 — accepted by product owner. Status DONE; merged to `main`.
