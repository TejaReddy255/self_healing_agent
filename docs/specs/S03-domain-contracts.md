---
id: S03
title: Shared domain contracts and canonical hashing
status: DRAFT
phase: 0
owner_agent: backend-engineer
reviewers: [code-reviewer, solution-architect, security-reviewer]
depends_on: [S01]
blocked_by: []
traces: [FR-031, FR-041, FR-081, NFR-060]
source_sections: ["src §6.2", "src §9.2", "src §21.3", "src §22.2", "src §23", "src §25", "src §35.6"]
tags: [contract, security-sensitive]
approved_by:
approved_on:
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
- **S03-AC3** `RemediationPlan` rejects an unknown `target_system`, an empty `verification_plan.assertions` (when `expected_write_operations` is non-empty), unknown assertion kinds, and a missing `expected`.
- **S03-AC4** `can_transition` encodes architecture §7.4 exactly (table-driven test over all pairs). `DETECTED→RESOLVED` and `EXECUTING→RESOLVED` are false.
- **S03-AC5** JSON Schemas are exported and snapshot-tested. Any schema change fails CI until the snapshot is updated deliberately.
- **S03-AC6** No module in `backend/domain` imports from other `backend.*` packages (import-lint test).
- **S03-AC7** `STAGE_TO_INCIDENT_STATUS` covers every `RemediationStage` value.

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

## Open issues
—

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
