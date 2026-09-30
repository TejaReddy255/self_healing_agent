---
description: Perform an application and agentic-AI security review with threat model, evidence-based findings, remediation, and release blockers.
---

# Security review

Create/update `docs/security-review.md`.

## Threat model
Identify:
- assets
- actors
- trust boundaries
- entry points
- privileged actions
- external integrations

## Review
Check:
- authentication/session
- authorization/tenant isolation
- secrets
- injection classes
- filesystem/network boundaries
- SSRF
- unsafe deserialization
- supply chain
- dependency risk
- log/data leakage
- PII handling
- prompt injection
- indirect prompt injection
- tool argument injection
- tool privilege escalation
- human approval bypass
- excessive autonomy
- cross-run state leakage
- MCP/tool trust
- model output validation

For each finding:
`ID | severity | evidence | impact | precondition | remediation | status`

Finish with blockers and residual risk. Do not label hypothetical issues as confirmed defects
without evidence.
