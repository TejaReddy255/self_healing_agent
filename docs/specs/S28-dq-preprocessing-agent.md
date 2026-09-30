---
id: S28
title: Data Preprocessing Agent — cleaning, transformation, normalization, integration
status: DRAFT
phase: 4
owner_agent: data-engineer
reviewers: [code-reviewer, evaluation-engineer, security-reviewer]
depends_on: [S27]
blocked_by: []
traces: [FR-140, FR-141, FR-142, FR-143, FR-144, NFR-070]
source_sections: ["user DQ suite brief: Data Preprocessing"]
tags: [dq-algorithm, security-sensitive]
approved_by:
approved_on:
---

# S28 — Data Preprocessing Agent

## Objective
Produce clean, transformed, normalized or integrated **derived datasets** from file versions, driven by
a validated declarative step spec executed in DuckDB, with full lineage. Preprocessing never writes to
source or target systems.

## In scope
- Agent `preprocessing`, operation `run`, with `params.steps` (ordered). Step types:
  - **Cleaning:** `drop_duplicates{keys?, keep: first|last}`, `trim{columns}`, `normalize_whitespace`,
    `handle_nulls{column, strategy: drop|constant|mean|median|mode, value?}`, `coerce_types{column, type, on_error: null|fail}`
    (reports the coerced-failure count), `standardize_case{columns, case}`.
  - **Transformation:** `rename`, `select`, `cast`, `derive{name, expression}` (safe DSL, S26), `filter{expression}`,
    `parse_datetime{column, format}`, `split{column, delimiter, into}`.
  - **Normalization:** `min_max`, `z_score`, `robust{median, iqr}`, `log1p` — fitted parameters are recorded, or
    `params_from` a previous job's fitted parameters for reuse on new data.
  - **Integration:** `join{right: DatasetRef, on, how: inner|left|full}`, `union{others, align: by_name}`. Reports
    unmatched counts and column conflicts.
- Inputs: file versions and S3 objects. Table inputs are allowed read-only (materialized through a bounded extraction).
- Output: a new file version (Parquet) for the target file (`output_file_id` or new file), with lineage
  `{inputs: fingerprints, steps, params_hash, agent_version}`. Also a step report (rows in/out per step, nulls filled,
  duplicates removed, coercion failures) as findings with status PASS/WARNING.
- `dry_run=true`: returns the step report and a 50-row preview without storing output.

## Acceptance criteria
- **S28-AC1** Each step type produces the expected output on fixture data (golden Parquet comparison), including edge cases (all-null column, empty dataset, unicode whitespace).
- **S28-AC2** `drop_duplicates` on the canonical-shaped fixture (417 duplicate keys) removes exactly 417 rows. `handle_nulls` reports exact fill counts.
- **S28-AC3** The normalization fitted parameters are recorded, and applying `params_from` to new data uses the stored parameters (not refitted). An inverse-transform property test holds for min-max, z-score and robust.
- **S28-AC4** A `derive`/`filter` expression outside the DSL allowlist is rejected before execution.
- **S28-AC5** The output version's lineage references the exact input fingerprints and steps. Re-running the same request yields a byte-identical output hash (NFR-070).
- **S28-AC6** No preprocessing code path opens a write-capable connection to PostgreSQL, MySQL or S3 source buckets (connector-level test and import check). Output goes only to the `dq-files` bucket.
- **S28-AC7** `dry_run` stores nothing.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1–AC4, AC7 | unit | `tests/unit/dq_agents/preprocessing/` |
| AC5, AC6 | integration | `tests/integration/dq_service/test_preprocessing_jobs.py` |
| — | eval | E-DQ-PRE |

## Rollback
Revert. Derived versions remain as files.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-30 — created (DRAFT)
