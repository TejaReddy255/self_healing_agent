---
id: S29
title: Data Profiling Agent — categorical/numerical statistics, distributions, detailed report, baseline drift
status: DRAFT
phase: 4
owner_agent: data-engineer
reviewers: [code-reviewer, evaluation-engineer]
depends_on: [S26, S27]
blocked_by: []
traces: [FR-150, FR-151, FR-152, FR-153, FR-154, NFR-070, NFR-073]
source_sections: ["user DQ suite brief: Data Profiling Agent", "src §21.12", "src §23.2"]
tags: [dq-algorithm]
approved_by:
approved_on:
---

# S29 — Data Profiling Agent

## Objective
Produce an exact, reproducible statistical report of a dataset covering categorical and numerical statistics,
distributions and a detailed report, plus drift against a baseline profile. This feeds monitoring
(`on_profile_change`), statistical checks and the workbench.

## In scope
- Agent `profiling`. Operations `profile` and `compare{baseline: job_id | file_version}`.
- **Type detection** per column: numeric, integer, boolean, datetime, categorical (distinct ratio ≤ `categorical_max_ratio`
  or distinct ≤ `categorical_max_distinct`), text, id-like (distinct ratio ≈ 1).
- **Categorical statistics:** count, missing (null and empty), distinct, distinct ratio, mode, top-k values with frequency
  and percentage (PII masked), rare categories (< `rare_threshold`), Shannon entropy, string length min/mean/max, pattern
  classes (e.g. `Aaaa-9999`) top-k.
- **Numerical statistics:** count, missing, mean, std, variance, min, max, sum, zeros, negatives, infinities, quantiles
  (p1, p5, p25, p50, p75, p95, p99), IQR, skewness (Fisher, bias-corrected), excess kurtosis, coefficient of variation.
- **Distribution analysis:** histogram (Freedman–Diaconis bin count, capped at `max_bins`), datetime histograms by period,
  normality test (D'Agostino–Pearson K² on a seeded sample ≤ 5,000) with p-value, and a distribution-shape hint
  (normal, skewed, heavy-tailed, bimodal) from deterministic rules.
- **Dataset-level:** row count, column count, duplicate-row count, memory/size, correlation matrix (Pearson and Spearman) for numeric columns (≤ `max_corr_columns`).
- **Execution:** exact aggregates in DuckDB (files) or pushdown (tables). Quantiles are exact for files and
  approximate for tables above `exact_quantile_max_rows` (documented tolerance and flagged in the result).
- **Detailed report:** canonical JSON plus rendered HTML (autoescaped template, embedded SVG charts) and Markdown via `/v1/jobs/{id}/report`.
- **Baseline comparison:** PSI per column (numeric using the baseline bin edges; categorical using frequencies; ε-smoothing),
  two-sample KS on samples, schema changes (added/removed/type-changed columns), missing-rate delta. Findings are WARNING
  above `psi_warn` (0.1) and CRITICAL above `psi_crit` (0.25) (configurable).
- **Profile hash:** SHA-256 over schema plus rounded key statistics, used by the S18 `on_profile_change` trigger.

## Acceptance criteria
- **S29-AC1** Numerical and categorical statistics match a numpy/pandas/scipy reference implementation on synthetic fixtures: exact within 1e-9 relative for moments and counts; quantiles exact on files; approximate quantiles within the documented rank-error tolerance.
- **S29-AC2** Type detection matches the synthetic ground truth for ≥ 95 % of columns across the fixture suite, with 0 id-like columns misclassified as numeric measures.
- **S29-AC3** The histogram bin count follows Freedman–Diaconis with the cap. Bin counts sum to the non-null count.
- **S29-AC4** The HTML report escapes all data values (an XSS fixture value renders inert). PII columns are masked in top-k lists and patterns.
- **S29-AC5** Compare: no-shift synthetic pairs yield PSI < 0.1 in ≥ 95 % of 100 seeded trials. An injected 1σ mean shift yields PSI ≥ 0.25. Schema changes are reported exactly.
- **S29-AC6** Identical requests produce identical results and profile hash. Changing one value in a column changes the hash only if the rounded key stats change (documented rounding).
- **S29-AC7** Performance: 1M rows × 30 columns from Parquet within the NFR-073 target on the reference machine (recorded).

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1–AC6 | unit | `tests/unit/dq_agents/profiling/` |
| AC7 | integration (perf) | `tests/integration/perf/test_profiling_perf.py` |
| — | eval | E-DQ-PRF |

## Rollback
Revert. Reports already stored remain readable.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-30 — created (DRAFT)
