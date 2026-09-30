---
id: S34
title: DQ Workbench — backend gateway, Streamlit pages, grounded report narrator
status: DRAFT
phase: 4
owner_agent: frontend-engineer
reviewers: [code-reviewer, security-reviewer, evaluation-engineer]
depends_on: [S27, S28, S29, S30, S31, S32, S33, S08]
blocked_by: []
traces: [FR-200, FR-201, FR-101, NFR-050]
source_sections: ["user DQ suite brief (all cards)", "src §21.11"]
tags: [ui, ai, security-sensitive]
approved_by:
approved_on:
---

# S34 — DQ Workbench

## Objective
Give analysts one Streamlit workspace with a card for each capability (Data Preprocessing, Data Profiling,
File Manager, Data Recon, Custom DQ, Statistical Check, Anomaly Detection). It works through the backend only
(ADR-009) and offers an optional, clearly labelled, number-grounded AI explanation of any report.

## In scope
- **Backend gateway** (backend-engineer co-owns): `/dq/workbench/files` (upload streaming with size limit, list, detail,
  preview, delete), `/dq/workbench/jobs` (submit, status, result, report), `/dq/workbench/models`, `/dq/workbench/presets`,
  `/dq/workbench/agents`. Roles: viewer reads; operator uploads, runs jobs and manages presets; admin can hard-delete (if enabled).
  Every call is audited with the actor and forwarded with the service token. The gateway never exposes the DQ service URL or token.
- **W6 DQ Report Narrator** (backend, `dq_narrator@1`, no tools; `docs/agent-design.md` §5a): `POST /dq/workbench/jobs/{id}/narrative`
  → `DQNarrative{summary, key_findings[{text, cited_paths[]}], caveats}`. Deterministic validation: every number in the
  text must equal (within rounding) a value at a cited result path, or the narrative is regenerated once and then refused.
  Sample values are passed as `untrusted()`. The output is labelled "AI-generated explanation — numbers verified against the report".
- **Streamlit pages** (`frontend/pages/dq_*`):
  - **DQ Home:** the seven capability cards with descriptions and entry points.
  - **File Manager:** upload (with PII column declaration), list, versions and lineage, preview, delete; model registry
    (versions, metrics); presets (create, edit, run).
  - **Data Preprocessing:** step builder (cleaning / transformation / normalization / integration), dry-run preview, run → new version.
  - **Data Profiling:** run, report viewer (categorical, numerical, distributions, detailed report download), compare to baseline.
  - **Custom DQ:** dataset picker (file version, or pipeline dataset from metadata), rules from metadata (read-only) or an ad-hoc
    rule builder, quick validation, results table by dimension with masked failed samples.
  - **Data Recon:** source/target pickers, keys, compare columns, tolerances, mode; results plus resolution suggestions
    (labelled advisory, with a link to the incident/SOP category — no action button that writes).
  - **Statistical Check:** column and check selection, baseline selection, results with charts.
  - **Anomaly Detection:** train (algorithm, features, labels), model versions, score, top anomalies with explanations, pattern detection.
  - An "Explain" button on each report (W6).
- Reuse the S12 job-progress component.

## Acceptance criteria
- **S34-AC1** `frontend/` calls only backend `/dq/workbench/*` for DQ features. It has no DQ service URL or token (config and boundary test).
- **S34-AC2** The gateway enforces roles, streams uploads with the size limit, forwards the request key for idempotency, and audits each call.
- **S34-AC3** Every page has AppTest coverage for the happy path, empty state, job ERROR and backend/DQ-service-unavailable messages.
- **S34-AC4** The narrator refuses or regenerates when a number in the text is not found at its cited path (fixture with a hallucinated number). Injection text in sample values does not change the output structure (E-NAR).
- **S34-AC5** Recon suggestions and narratives are visibly labelled advisory / AI-generated. No workbench control can trigger a remediation or a write to a source/target system.
- **S34-AC6** The workbench demo (runtime): upload the canonical target extract → profile → Custom DQ (417 duplicates) → recon vs source → preprocessing dedupe → re-run Custom DQ → PASS.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC5 | unit | `tests/unit/frontend/test_boundaries.py`, `test_dq_pages_labels.py` |
| AC2 | integration | `tests/integration/api/test_dq_gateway.py` |
| AC3 | unit (AppTest) | `tests/unit/frontend/test_dq_pages.py` |
| AC4 | unit + eval | `tests/unit/agents/test_dq_narrator_grounding.py`, `tests/evals/test_narrator.py` |
| AC6 | runtime | recorded demo |

## Rollback
Hide the DQ pages via `FEATURE_DQ_WORKBENCH=false`. Revert the gateway.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-30 — created (DRAFT)
