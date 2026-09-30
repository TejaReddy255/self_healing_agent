---
id: S21
title: Streamlit monitoring, pipeline health, incident, DQ and metadata views (AM-11; META-10)
status: DRAFT
phase: 6
owner_agent: frontend-engineer
reviewers: [code-reviewer]
depends_on: [S12, S14, S15, S16, S17, S20]
blocked_by: []
traces: [FR-100, FR-101, FR-103, NFR-050]
source_sections: ["src §21.11", "src §33.15 (META-10)"]
tags: [ui]
approved_by:
approved_on:
---

# S21 — Streamlit monitoring and incident experience

## Objective
Make the backend the operator's single source of truth for monitoring and incidents. Show every stage
of the lifecycle with its evidence, source and freshness. Reuse the S12 HITL components.

## In scope
- **Dashboard:** counts of healthy/warning/critical pipelines, open incidents by status, DQ failures (24 h), SLA
  breaches (24 h), pending reviews, and incidents resolved today (from backend aggregates).
- **Pipeline Health:** per-monitor status and latest events; manual check (operator).
- **Incident page** tabs: Overview, Monitoring, DQ Evidence, Airflow, RCA, Remediation, Review (reuses S12 components),
  Execution, Verification, Recovery, Audit, plus Timeline. Dismiss and Analyze actions per role.
- **Metadata/Settings:** metadata health, documents by type, reload (admin), MCP status, and tool classification view (read-only).
- Context panels label each section with `source` and `fetched_at` (e.g. "Airflow · live · 12 s ago", "Metadata · v3f2a…").
- Data-flow Monitoring switches from the S12 Airflow scan to backend monitoring APIs. The S12 failure-scan logic is removed.

## Acceptance criteria
- **S21-AC1** No page calls Airflow proxy routes for monitoring data (only for explicit operator actions). The import boundary from S12 still holds.
- **S21-AC2** Against a fixture backend representing the canonical scenario, the incident page shows the full ordered timeline from DETECTED to RESOLVED, with links to the analysis, review, job, verification and recovery.
- **S21-AC3** Every context panel shows its source label and freshness. Stale data (older than the configured threshold) is visibly marked with text.
- **S21-AC4** The HITL review UI on the incident page uses the S12 components (no duplicate implementation; code review item plus import check).
- **S21-AC5** Role-restricted actions (dismiss, analyze, reload, approve) are hidden or disabled for insufficient roles, and backend 403s are handled.
- **S21-AC6** AppTest coverage for the dashboard, pipeline health, incident page and settings, including empty and error states.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC4 | unit | `tests/unit/frontend/test_boundaries.py` |
| AC2, AC3, AC5, AC6 | unit (AppTest + mocked backend) | `tests/unit/frontend/test_incident_page.py` and others |

## Optional review
`design:accessibility-review` skill on the incident page.

## Rollback
Revert code.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
