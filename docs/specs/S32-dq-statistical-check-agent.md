---
id: S32
title: Statistical Check Agent — MAD, outliers, p-value tests, skew/kurtosis, PCA, KNN, factor analysis
status: DRAFT
phase: 4
owner_agent: data-engineer
reviewers: [code-reviewer, evaluation-engineer]
depends_on: [S28, S29]
blocked_by: []
traces: [FR-180, FR-181, FR-182, FR-183, FR-184, NFR-070, NFR-073]
source_sections: ["user DQ suite brief: Statistical Check Agent", "src §17", "src §35.4 (not every run)"]
tags: [dq-algorithm]
approved_by:
approved_on:
---

# S32 — Statistical Check Agent

## Objective
Categorize and test data with basic and advanced statistics, giving each result a statistic, a threshold and
enough evidence to explain the verdict. Keep false alarms controlled: on large samples, p-values alone are not
treated as evidence of a practically meaningful change.

## In scope
- Agent `statistical`. Operation `run_checks{dataset, columns?, checks: [...], baseline?: BaselineRef}`.
  It declares `requires_preprocessing` for multivariate checks (standardization plus missing-value handling via S28 steps,
  executed as an internal prerequisite and recorded in lineage).
- **Univariate**
  - `mad`: robust z = 0.6745·(x − median)/MAD. Flag |z| > `threshold` (default 3.5). MAD = 0 → fall back to the
    mean absolute deviation, and flag `degenerate_mad`.
  - `outliers{method: iqr|zscore|mad|consensus}`: IQR k = 1.5 (mild) / 3.0 (extreme); z > 3. `consensus` = flagged by ≥ 2 methods.
  - `skew_kurtosis`: bias-corrected skewness and excess kurtosis with thresholds (|skew| > 1, |kurt| > 3 by default).
    Change vs baseline when provided.
- **Baseline tests** (`pvalue_tests`): numeric → two-sample KS and Mann-Whitney U; categorical → χ² on aligned categories
  (rare categories pooled so expected counts are ≥ 5). Benjamini–Hochberg correction across columns (`alpha` = 0.05).
  **A finding requires both q < alpha and effect size ≥ `min_effect`** (KS D ≥ 0.1, Cramér's V ≥ 0.1, rank-biserial ≥ 0.1),
  with a minimum sample size per side.
- **Multivariate** (standardized numeric columns, seeded sample ≤ `DQ_MAX_SAMPLE_ROWS`)
  - `pca`: components for ≥ 90 % explained variance; reconstruction-error outliers above the `quantile` (default 0.99)
    of the baseline or current distribution; explained-variance shift vs baseline.
  - `knn`: mean distance to k nearest neighbours (k = 10) as the outlier score; flag above the quantile threshold.
  - `factor_analysis`: n_factors (config or Kaiser criterion); loadings. Compared to baseline with Tucker's congruence
    coefficient per factor (after best-match alignment); flag < 0.85.
- Every finding carries the statistic, threshold, method, sample size, seed and, for row-level checks,
  flagged-row count plus masked top-N rows with per-column robust z contributions.

## Acceptance criteria
- **S32-AC1** MAD/IQR/z/consensus flag exactly the injected gross outliers (≥ 6σ) on synthetic data: recall ≥ 0.95. The false-positive rate on clean N(0,1) with n = 10,000 is ≤ 1 % for MAD at 3.5.
- **S32-AC2** Baseline tests over 200 seeded no-shift trials give a false-alarm rate ≤ 7 % at alpha 0.05 (after BH and effect-size gating). A 0.5σ mean shift with n = 5,000 is detected in ≥ 95 % of trials. Large-n trivial shifts below `min_effect` are not flagged.
- **S32-AC3** χ² pools rare categories correctly and never runs with expected counts < 5. It reports `insufficient_data` instead.
- **S32-AC4** PCA, KNN and factor analysis detect an injected structure break (a correlated block decorrelated) in ≥ 90 % of seeded trials, and stay silent on ≥ 90 % of unchanged trials.
- **S32-AC5** Results are identical for identical requests (seeded). Statistics match a scipy/sklearn reference to 1e-9 where deterministic.
- **S32-AC6** Missing values, constant columns and non-numeric columns are handled with explicit `skipped{reason}` entries, never exceptions.
- **S32-AC7** Performance: 100k rows × 20 columns within the NFR-073 target (recorded).

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1–AC6 | unit / statistical eval | `tests/unit/dq_agents/statistical/`, `tests/evals/dq/test_statistical.py` |
| AC7 | integration (perf) | `tests/integration/perf/test_statistical_perf.py` |
| — | eval | E-DQ-STAT |

## Rollback
Revert.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-30 — created (DRAFT)
