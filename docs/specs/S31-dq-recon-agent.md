---
id: S31
title: Data Recon Agent — cross-verification, alignment, consistency checks, error resolution
status: DRAFT
phase: 4
owner_agent: data-engineer
reviewers: [code-reviewer, evaluation-engineer, security-reviewer]
depends_on: [S26, S27]
blocked_by: []
traces: [FR-170, FR-171, FR-172, FR-173, NFR-070, NFR-073]
source_sections: ["user DQ suite brief: Data Recon Agent", "src §23.2 (RECONCILIATION)", "src §35.9"]
tags: [dq-algorithm, security-sensitive]
approved_by:
approved_on:
---

# S31 — Data Recon Agent

## Objective
Prove whether a source and a target agree. It compares counts and aggregates, aligns rows by key,
checks consistency with checksums, and classifies discrepancies into advisory resolution categories.
This is the reconciliation checkpoint for the canonical scenario.

## In scope
- Agent `recon`. Operation `reconcile{source: DatasetRef, target: DatasetRef, keys, compare_columns?,
  column_map?, tolerances?: {column: abs|rel}, partition_column?, mode: full|bucketed, buckets?}`.
- **Cross-verification:** row counts; per-column aggregates (count non-null, sum, min, max, and distinct count for keys),
  optionally per partition, all pushed down on each side and compared with tolerances.
- **Data alignment** (key based): stream `keys + compare_columns` from each side (chunked, read-only) into DuckDB.
  Compute normalized row hashes in DuckDB. Classify rows as `missing_in_target`, `extra_in_target`,
  `duplicate_keys_source`, `duplicate_keys_target` and `value_mismatch` (per column, honouring tolerances and type normalization:
  trimmed strings, decimal scale, timestamp UTC).
- **Consistency checks:** `bucketed` mode compares `count + xor/sum of row hashes` per `hash(key) % N` bucket and
  drills into mismatched buckets only. Results must equal `full` mode.
- **Error resolution (deterministic, advisory):** classify discrepancy patterns → `suggestion{category, rationale, evidence_refs}`
  using the metadata SOP categories: duplicate target keys with extra rows → `DUPLICATE_DATA`; missing rows concentrated in a
  partition → `MISSING_PARTITION` / `INCOMPLETE_LOAD`; value mismatches concentrated in one column → `TRANSFORMATION_DRIFT`;
  type/format-only mismatches → `SCHEMA_DRIFT`. No suggestion is executed. Remediation happens only through the platform's HITL flow.
- Findings: `ROW_COUNT_MATCH`, `AGGREGATE_MATCH:<col>`, `KEY_ALIGNMENT`, `VALUE_MATCH:<col>`, each with expected/actual,
  counts and masked samples of offending keys.

## Acceptance criteria
- **S31-AC1** Canonical: MySQL `sales.customer` (100,000) vs PG `dw.customer` (100,417) → CRITICAL `ROW_COUNT_MATCH` with diff +417. `KEY_ALIGNMENT` reports `duplicate_keys_target = 417`, `missing_in_target = 0` and `extra_in_target` (distinct keys) = 0. The suggestion category is `DUPLICATE_DATA`.
- **S31-AC2** On synthetic pairs with injected missing, extra, duplicate and mismatched rows, every count is exact.
- **S31-AC3** Cross-dialect normalization: values that are equal but formatted differently (DECIMAL(10,2) vs DOUBLE, `' a'` vs `'a'` with trim enabled, timestamps with and without time zone) are not reported as mismatches. Real differences beyond tolerance are.
- **S31-AC4** `bucketed` mode returns results identical to `full` mode on the synthetic suite, extracting only mismatched buckets (extraction-volume assertion).
- **S31-AC5** Suggestions are deterministic for the documented patterns, carry evidence refs, and the recon agent has no code path to any write connector.
- **S31-AC6** Performance: 1M keys MySQL → PG within the NFR-073 target (recorded).

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC3, AC6 | integration (compose DBs) | `tests/integration/dq_agents/test_recon_canonical.py` |
| AC2, AC4, AC5 | unit + integration | `tests/unit/dq_agents/recon/` |
| — | eval | E-DQ-RCN |

## Rollback
Revert.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-30 — created (DRAFT)
