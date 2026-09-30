---
id: S12
title: Streamlit operator console v1
status: DRAFT
phase: 2
owner_agent: frontend-engineer
reviewers: [code-reviewer]
depends_on: [S05, S09, S10, S11]
blocked_by: []
traces: [FR-100, FR-101, FR-102, NFR-050]
source_sections: ["src §4", "src §4.2–§4.6", "src §21.11"]
tags: [ui]
approved_by:
approved_on:
---

# S12 — Streamlit operator console v1

## Objective
Deliver the baseline operator experience (Data-flow Monitoring, Agentic Self-Healing, Review, Governance)
entirely through FastAPI, including execution-job progress, so that the P2 "baseline parity" demo can run in the UI.

## In scope
- `frontend/api_client.py`: typed methods for all S01–S11 endpoints; per-call timeouts; bearer token;
  error mapping to user messages (401/403/404/409/422/5xx/timeout).
- Session state limited to UI continuity (src §4.2 key groups). No durable state in the UI.
- Pages:
  - **Data-flow Monitoring:** DAG list, recent failed runs (via `/airflow/*`), failure classification display
    (src §4.3 token heuristics as a *presentation* helper only), status/timeline charts from live data only
    (no synthetic fallback dataset).
  - **Agentic Self-Healing:** select a pipeline/DAG, trigger a run, track task states live, start RCA for a failed run,
    render the analysis (summary, findings with evidence status, recommendations, SOP evaluation, plan, executable flag and reasons).
  - **Review:** filter/list, review card (target, risk, revision, hash), revision editor (JSON of the SOP/remediation
    portion only), revision-job progress, approve (sends the displayed revision and hash) or reject, revision history, audit.
  - **Execution:** job stage timeline and recovery tracking.
  - **Governance:** performance, LLM costs, analyses history, review/audit summary.
- Activity feed: follow the S02 ordering contract. Subscribe to `/ws/activities` first, then fetch `GET /activities`,
  de-duplicate by `id`, and re-fetch after a reconnect (close code 1013 means the client was too slow).
- Reusable components in `frontend/components/` (review card, revision editor, HITL controls, audit table,
  stage timeline). S21 reuses them.

## Out of scope
Monitoring/incident/DQ/metadata pages (S21).

## Acceptance criteria
- **S12-AC1** `frontend/` imports nothing from `backend`, `httpx` Airflow clients, `asyncpg` or `mcp`, and uses only `AGENT_BASE_URL` (import-lint test).
- **S12-AC2** Against a backend on fakes: trigger run → see failure → start RCA → see analysis and review (AppTest flow).
- **S12-AC3** Approve is disabled while a revision job is PENDING or PROCESSING, and it sends the displayed revision and hash. A 409 shows "Review changed — reload" and does not retry automatically.
- **S12-AC4** The revision editor rejects invalid JSON client-side and cannot change fields outside the SOP/remediation payload.
- **S12-AC5** The execution view shows stage progression. "Succeeded" is shown only from the backend review/job status.
- **S12-AC6** Backend-down, timeout, 401 and 403 render actionable messages without stack traces.
- **S12-AC7** Statuses are shown with text labels, not color alone.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1 | unit | `tests/unit/frontend/test_boundaries.py` |
| AC2–AC7 | unit (AppTest + respx-mocked backend) | `tests/unit/frontend/test_pages_*.py` |

## Runtime verification
The P2 exit demo is run manually through the UI with screenshots or a recording referenced in the evidence.

## Rollback
Revert code.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
