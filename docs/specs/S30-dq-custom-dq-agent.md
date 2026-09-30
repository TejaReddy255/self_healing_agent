---
id: S30
title: Custom DQ Agent — incompleteness, inaccuracy, inconsistency, invalidity, quick validation
status: DRAFT
phase: 4
owner_agent: data-engineer
reviewers: [code-reviewer, evaluation-engineer, security-reviewer]
depends_on: [S26, S27, S04]
blocked_by: []
traces: [FR-160, FR-161, FR-162, FR-163, FR-164, NFR-070, NFR-073]
source_sections: ["user DQ suite brief: Custom DQ Agent", "src §23.2", "src §33.5 (business rules)", "src §35.9"]
tags: [dq-algorithm, security-sensitive]
approved_by:
approved_on:
---

# S30 — Custom DQ Agent

## Objective
Evaluate deterministic data-quality rules exactly, pushing them down to the data where it lives. The
rule catalog covers the four dimensions. It also provides a "quick validation" that works without predefined rules.
This is the primary blocking checkpoint agent for the canonical scenario.

## In scope
- Agent `custom_dq`. Operations `run_rules{rule_set | rule_ids}` and `quick_validate`.
- **Rule catalog** (rule_type → dimension). Every rule has `rule_id, rule_type, column(s), params, severity,
  blocking, expected, max_failed_ratio?`:

  | Dimension | Rule types |
  |---|---|
  | Incompleteness | `NOT_NULL`, `NOT_EMPTY`, `COMPLETENESS_RATIO{min_ratio}`, `REQUIRED_COLUMNS` |
  | Inaccuracy | `RANGE{min,max,inclusive}`, `ALLOWED_VALUES{values}`, `REFERENCE_MATCH{ref: DatasetRef, ref_column}` |
  | Inconsistency | `UNIQUE{columns}`, `CROSS_COLUMN{expression}` (safe DSL), `REFERENTIAL_INTEGRITY{ref, fk, pk}`, `FORMAT_CONSISTENCY{min_dominant_pattern_ratio}` |
  | Invalidity | `TYPE_CONFORMANCE{type}`, `REGEX{pattern}` (RE2-compatible subset; length-capped), `DATE_VALID{format}`, `SCHEMA_CONFORMANCE{columns: {name: type}}` |
  | Volume / timeliness | `ROW_COUNT{min,max}`, `FRESHNESS{timestamp_column, max_age}` |

- The **rule compiler** turns each rule into a counting query (failed-row count + total) and a bounded sample query, compiled
  per dialect (postgres, mysql, duckdb). `REFERENCE_MATCH`/`REFERENTIAL_INTEGRITY` across different systems
  extract key sets into DuckDB (bounded; bucketed if large).
- Rules come from the platform (the business rules from S04 metadata, passed in the request by the backend adapter) or
  from the workbench (ad-hoc rule sets). The **S04 business-rule schema and this catalog are the same model** — the S04
  validator is extended to use it (shared JSON Schema published by the DQ service `/v1/agents`).
- Result per rule: status (PASS if failed ≤ threshold, else rule severity), `expected`, `actual` (e.g. `{duplicate_count: 417}`),
  `failed_row_count`, `total_rows`, masked `sample_rows` (≤ `max_samples`), dimension.
- **Quick validation:** auto-generates checks from the inferred schema (S27) or a live profile. Nullability in key-like columns,
  duplicate id-like columns, type conformance, obvious ranges (negative counts/ages), empty strings and date validity.
  Findings are WARNING only (advisory; never blocking).
- `rule_ids` filter for verification reruns (FR-164).

## Acceptance criteria
- **S30-AC1** Canonical: on seeded `warehouse.dw.customer` (PG), `UNIQUE(customer_id)` → CRITICAL with `actual.duplicate_count = 417`. `NOT_NULL(customer_id)` → PASS. On the source (MySQL) both PASS. The work is executed by pushdown (query log shows aggregate queries; no full-table extraction).
- **S30-AC2** Every rule type produces exact failed counts on synthetic ground truth, on all three engines (postgres, mysql, duckdb), with identical results across engines for the same data.
- **S30-AC3** `rule_ids=[BR-CUST-002]` evaluates only that rule.
- **S30-AC4** `max_failed_ratio` thresholds behave at the boundaries (equal → PASS). `blocking` and `severity` are carried to findings.
- **S30-AC5** Malicious rule parameters (SQL in column names, a DSL escape attempt, a catastrophic regex) are rejected at validation. Identifiers are quoted per dialect.
- **S30-AC6** Samples are bounded and PII-masked. No sample is returned for PASS rules.
- **S30-AC7** Quick validation on synthetic data with injected issues finds each injected issue class and never marks a finding blocking.
- **S30-AC8** Performance: 10 rules on a 1M-row PG table within the NFR-073 target (recorded).

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC8 | integration (compose DBs, seeded) | `tests/integration/dq_agents/test_custom_dq_canonical.py` |
| AC2–AC7 | unit + integration | `tests/unit/dq_agents/custom_dq/`, `tests/integration/dq_agents/test_custom_dq_engines.py` |
| — | eval | E-DQ-CDQ |

## Rollback
Revert. Metadata rules are unaffected.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-30 — created (DRAFT)
