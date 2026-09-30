---
id: S09
title: Diagnostic service — read-only RCA (manual path)
status: DRAFT
phase: 2
owner_agent: ai-engineer
reviewers: [code-reviewer, security-reviewer, evaluation-engineer, data-engineer]
depends_on: [S04, S05, S06, S08]
blocked_by: []
traces: [FR-030, FR-031, FR-032, FR-033, FR-034, NFR-004, NFR-005, UC-AC2]
source_sections: ["src §4.4", "src §6", "src §14.1", "src §0.5", "src §33.9", "src §33.12"]
tags: [ai, security-sensitive, schema]
approved_by:
approved_on:
---

# S09 — Diagnostic service (read-only RCA)

## Objective
Given a failed run, produce and persist an evidence-grounded `StructuredAnalysis` using only read
tools. Deterministically decide whether its proposed remediation is executable.

## In scope
- Migration: `analyses` (id uuid, created_at, finished_at, trigger, pipeline_id, dag_id, run_id,
  task_id, incident_id nullable, status RUNNING/COMPLETED/FAILED, error_code, structured_analysis jsonb,
  executable_plan bool, plan_rejected_reasons jsonb, prompt_version, model, requested_by). Partial unique
  index on `(dag_id, run_id, coalesce(task_id,''))` where `status='RUNNING'`.
- `backend/metadata/context_builder.py` `PipelineContextBuilder.build(pipeline_id, run_id, task_id)` →
  `DiagnosticContext` with sections `current_verified_evidence` (Airflow run/task state, failed-task
  log tail), `reference_metadata` (pipeline, SLA, business rules, approved SOPs), and `historical_information`
  (known issues, runbooks, historical incidents). Every item carries `source`, `ref_id` and `fetched_at`.
  Rendering wraps all runtime, free-text and metadata content in `untrusted()`.
- Prompt `diagnostic@1` (agent-design §1). Agent built via S08 with `diagnostic_toolset()` plus Airflow read
  tools scoped to the pipeline's `dag_id`.
- `backend/services/diagnostic_service.py` `DiagnosticService.analyze(request, actor, incident_id=None)`:
  pipeline resolution (request `workflow_id` = `dag_id` → pipeline via metadata), row insert RUNNING
  (duplicate → `AnalysisInProgress` with the existing ID), agent run, N4/N5 validation (agent-design §1),
  persist, activity and counters.
- `backend/services/plan_validator.py` implementing the N5 rules (shared with S10/S19).
- APIs: `POST /agent` (operator; `PipelineFailureRequest`; sync with `AGENT_REQUEST_TIMEOUT_S`; 409
  `{analysis_id}` on an in-flight duplicate), `GET /analyses`, `/analyses/{id}`, `/latest-analysis`.
- Eval datasets E-RCA-01..05, E-RCA-E1..E4 and E-INJ-01..03 in replay mode (harness skeleton in `tests/evals/`).

## Out of scope
Review creation (S10 subscribes to `AnalysisCompleted`). Incident context (S16).

## Acceptance criteria
- **S09-AC1** With the fake model and fake tools, `POST /agent` for a failed run persists a COMPLETED analysis containing a valid `StructuredAnalysis`, and returns it.
- **S09-AC2** The constructed diagnostic agent's toolset contains zero WRITE/UNCLASSIFIED tools (asserted on the built agent). A scripted call to a write tool name is refused as `TOOL_NOT_ALLOWED`, audited, and never reaches MCP.
- **S09-AC3** The rendered prompt contains the three evidence sections in precedence order. All Airflow log, DB output and metadata free text sits inside `<untrusted>` blocks (snapshot test).
- **S09-AC4** A plan whose `target_system` or resource is outside the pipeline's permitted scope, or which lacks assertions, `max_affected_rows` for DML, or stop conditions, yields `executable_plan=false` with explicit reasons.
- **S09-AC5** Concurrent duplicate requests for the same scope → one agent run. The second request gets 409 with the existing `analysis_id` (DB-enforced, works across processes).
- **S09-AC6** Agent failure (limit, schema, model down) → analysis FAILED with `error_code`. No executable plan is produced.
- **S09-AC7** `agent_performance` and activities are updated. All model and tool calls carry `analysis_id` in the audit.
- **S09-AC8** Replay evals E-RCA and E-INJ-01..03 run in CI. The blocking metrics (0 write calls; 0 out-of-scope plans under injection) pass. Non-blocking metrics are reported.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC6, AC7 | integration | `tests/integration/services/test_diagnostic_service.py` |
| AC2 | unit | `tests/unit/services/test_diagnostic_toolset.py` |
| AC3 | unit | `tests/unit/metadata/test_context_rendering.py` |
| AC4 | unit | `tests/unit/services/test_plan_validator.py` |
| AC5 | integration | `tests/integration/api/test_agent_concurrency.py` |
| AC8 | eval (replay) | `tests/evals/test_rca.py` |

## Runtime verification
Dev compose with a seeded duplicate scenario and a failed fake run: call `POST /agent` with live Gemini once (`-m live`), then inspect the analysis and the audit trail for zero write calls.

## Security and eval considerations
Indirect prompt injection is the main risk. Toolset composition and plan validation must not depend on model output.

## Rollback
Revert code. The `analyses` table is additive.

## Open issues
- From S03 (decision 2026-09-30): "writes need ≥ 1 verification assertion" is enforced on `ApprovalPayload`, not on `RemediationPlan`. An RCA whose plan lacks assertions stays schema-valid, and N5 marks it `executable_plan=false` (S09-AC4 stands as written).
- From S03 review: `DiagnosticService` rejects `fixes_applied=true`, `pipeline_status="remediated"` and findings labelled `EXECUTED`/`VERIFIED_FIXED` from the read-only RCA. Contract models are `extra="forbid"`, so expect more repair retries; measure them in evals.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
