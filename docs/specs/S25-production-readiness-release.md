---
id: S25
title: Production readiness and release
status: DRAFT
phase: 6
owner_agent: release-manager
reviewers: [code-reviewer, devops-sre, security-reviewer]
depends_on: [S22, S23]
blocked_by: []                      # OQ-07 decided 2026-09-30: Docker Compose on a VM
traces: [NFR-061]
source_sections: ["src §18", "src §28", "src §29", "src §30"]
tags: [runtime]
approved_by:
approved_on:
---

# S25 — Production readiness and release

## Objective
Package, deploy, operate and release the platform with evidence. Uses the `production-readiness` and
`release` skills and the `release-manager` agent.

## In scope
- Dockerfiles: api, worker (queues configurable), monitoring-worker, streamlit, and MCP servers. Non-root user, pinned
  base images, health checks. **Docker Compose production stack for a VM** (OQ-07) matching the architecture §4 topology,
  with Kubernetes-compatible liveness/readiness endpoints; secrets via environment files outside git or a secret manager.
- Migration rollout procedure (`migrate verify` → `up` before deploy; the app refuses readiness while migrations are pending).
- Backup and restore procedure for the operational DB, with a restore drill.
- Retention purge job per use-case-spec §7 (dry-run mode, audited).
- Runbooks in `docs/runbooks/`: remediation job `INTERRUPTED_DURING_EXECUTION`, stuck recovery, MCP server down,
  metadata reload failure, Gemini outage/quota, rotating tokens, replaying a failed callback.
- Alerts: job queue age, remediation failures, verification failure spikes, monitoring cycle overrun, LLM cost budget.
- `docs/production-readiness.md` (PASS/FAIL/NOT VERIFIED/NOT APPLICABLE with evidence), `docs/release-checklist.md`, release notes.

## Acceptance criteria
- **S25-AC1** All images build in CI and start with health checks passing in a staging-like compose.
- **S25-AC2** A restore drill from backup succeeds, and a restored instance passes `migrate verify` and readiness.
- **S25-AC3** The retention job deletes only rows past retention (dry-run and real-run test), and audit rows are never deleted before 13 months.
- **S25-AC4** Each runbook is exercised once in staging (evidence: date, operator, outcome).
- **S25-AC5** `docs/production-readiness.md` shows no FAIL or NOT VERIFIED on blocking items. `docs/release-checklist.md` is complete, including the rollback trigger and procedure.
- **S25-AC6** Post-deploy verification of health, the canonical smoke (replay mode against staging) and dashboards is recorded before the release is declared successful.

## Rollback
The release checklist defines the rollback trigger and procedure (previous image tags; migrations are additive, so the previous code runs on the new schema — verified in S25-AC1 staging).

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
