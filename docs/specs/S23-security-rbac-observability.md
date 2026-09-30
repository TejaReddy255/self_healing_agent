---
id: S23
title: Security, RBAC and observability hardening
status: DRAFT
phase: 6
owner_agent: backend-engineer
reviewers: [code-reviewer, security-reviewer, devops-sre]
depends_on: [S20]
blocked_by: []                      # OQ-05 decided 2026-09-30: OIDC/SSO for humans here; IdP details needed at refine
traces: [FR-112, NFR-001, NFR-002, NFR-003, NFR-061]
source_sections: ["src §16 (CORS, credentials)", "src §29", "src §36 (12)"]
tags: [security-sensitive, runtime]
approved_by:
approved_on:
---

# S23 — Security, RBAC and observability hardening

## Objective
Close the release-blocking security and operability gaps: production identity, complete role
coverage, rate limits, admin log access, metrics and tracing, dependency audit, and a formal security review.

## In scope
- OIDC for the API and Streamlit per OQ-05 (the `Actor` abstraction from S01 is preserved). Static tokens are kept only for the `service` role.
- Route-inventory test: every FastAPI route declares a role dependency. The allowlist of unauthenticated routes = `/health`.
- Rate limits on `POST /monitoring/events`, `POST /agent`, `POST /incidents/{id}/analyze` and `POST /dq/run`.
- `GET /logs` (admin): redacted tail, bounded size.
- `/metrics` (Prometheus) with the metrics in architecture §14. Optional OpenTelemetry traces with the log correlation attributes.
- Cross-process activity fan-out (PG `LISTEN/NOTIFY`) so WebSocket clients work with more than one API instance.
- `pip-audit` (or equivalent) in CI. Licence check of the dependency list.
- `security-review` skill → `docs/security-review.md` (threat model plus findings table). All High/Critical findings fixed or formally accepted.

## Acceptance criteria
- **S23-AC1** The route-inventory test passes. Adding an undeclared route fails CI.
- **S23-AC2** The role matrix (architecture §10) is enforced for every route (parameterized test over role × route).
- **S23-AC3** OIDC login works for Streamlit and the API in the dev IdP (or `NOT VERIFIED` with the reason). Static user tokens are disabled in `prod`.
- **S23-AC4** Rate limits return 429 with `Retry-After`. Legitimate Airflow callback volume (NFR-020) is not throttled.
- **S23-AC5** `/metrics` exposes the listed metrics. Values change as expected in the integration flows.
- **S23-AC6** `/logs` returns redacted content only to admins (secret-pattern test).
- **S23-AC7** The dependency audit has no known High/Critical vulnerabilities, or has documented exceptions.
- **S23-AC8** `docs/security-review.md` exists with no open High/Critical findings.

## Rollback
Feature flags for the OIDC and rate-limit middleware. Revert code.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
