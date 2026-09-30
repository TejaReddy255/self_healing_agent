---
id: S33
title: Anomaly Detection Agent — unsupervised/supervised learning, Isolation Forest, pattern recognition, scoring
status: DRAFT
phase: 4
owner_agent: data-engineer
reviewers: [code-reviewer, evaluation-engineer, security-reviewer]
depends_on: [S27, S28]
blocked_by: []
traces: [FR-190, FR-191, FR-192, FR-193, FR-194, NFR-070, NFR-072, NFR-073]
source_sections: ["user DQ suite brief: Anomaly Detection Agent", "src §17", "src §35.4"]
tags: [dq-algorithm, security-sensitive]
approved_by:
approved_on:
---

# S33 — Anomaly Detection Agent

## Objective
Detect anomalous records and metric patterns with machine learning. Training and scoring are separate,
models are versioned and signed, and each anomaly score comes with an explanation an operator can act on.

## In scope
- Agent `anomaly`. Operations:
  - `train{dataset, features, algorithm: isolation_forest|lof|gradient_boosting|random_forest, label_column?, params}` →
    a new model version in the registry (S27). Unsupervised algorithms need no labels. Supervised ones require `label_column`,
    use a stratified train/validation split (seeded) with `class_weight` for imbalance, and pick the decision threshold on
    validation to maximize F1 (or meet `target_precision`). Metrics are stored: precision, recall, F1, ROC-AUC, PR-AUC, confusion matrix.
  - `score{dataset, model_version, threshold?}` → per-row normalized score in [0, 1] (unsupervised: rank against the training
    score distribution; supervised: calibrated probability), flags above the threshold, summary counts, and top-N anomalies with
    per-feature explanations (robust z deviation per feature relative to training medians/MAD).
  - `detect_patterns{series: dataset with timestamp + metric columns | platform metric (e.g. daily row counts from dq history), period?}`
    → STL decomposition (when period known or detected) + robust z on residuals. Also rolling-median level-shift detection.
    Findings list anomalous timestamps with expected ranges.
- Feature schema is pinned in the model version. Scoring data with missing or extra features or type mismatches → ERROR `feature_schema_mismatch`.
  The preprocessing (imputation/scaling) used in training is stored as part of the model pipeline and reapplied at scoring.
- Artifacts are serialized by the service (joblib) **and HMAC-signed**. Load verifies the signature first (S27). No external model upload.
- Default schedule in platform mode: `SCHEDULED/DEEP` checkpoint only (S18). Never every monitoring cycle.

## Acceptance criteria
- **S33-AC1** Isolation Forest on the synthetic benchmark (1 % injected multivariate anomalies) reaches ROC-AUC ≥ 0.90. LOF ≥ 0.85.
- **S33-AC2** The supervised model on the synthetic labeled set reaches PR-AUC ≥ 0.80. The chosen threshold and metrics are persisted on the model version.
- **S33-AC3** Scoring with the pinned model version is deterministic. Scoring against the wrong feature schema → ERROR with the diff listed.
- **S33-AC4** A tampered model artifact is refused (signature check). No API accepts model binaries from clients.
- **S33-AC5** Explanations: for injected single-feature anomalies, the top-ranked contributing feature is the injected one in ≥ 90 % of cases.
- **S33-AC6** Pattern detection finds injected spikes and level shifts in a seasonal synthetic series (recall ≥ 0.9) with ≤ 2 % false-positive timestamps.
- **S33-AC7** Performance: train on 100k × 20 and score 100k within the NFR-073 targets (recorded).

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC2, AC5, AC6 | eval (deterministic, seeded) | `tests/evals/dq/test_anomaly.py` |
| AC3, AC4 | unit + integration | `tests/unit/dq_agents/anomaly/`, `tests/integration/dq_service/test_models.py` |
| AC7 | integration (perf) | `tests/integration/perf/test_anomaly_perf.py` |
| — | eval | E-DQ-ANO |

## Rollback
Revert. Model versions remain in the registry but are unused.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-30 — created (DRAFT)
