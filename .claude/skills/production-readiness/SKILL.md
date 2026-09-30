---
description: Audit an application against production-readiness gates across functionality, testing, AI evals, security, reliability, deployment, observability, operations, and cost.
---

# Production readiness review

Create/update `docs/production-readiness.md`.

Use statuses only:
- PASS
- FAIL
- NOT VERIFIED
- NOT APPLICABLE

Review:
1. requirements/acceptance criteria
2. architecture consistency
3. code quality/static checks
4. automated tests
5. AI/agent eval thresholds
6. security review
7. secrets/configuration
8. migrations/data recovery
9. resilience/timeouts/retries/idempotency
10. health checks
11. logs/metrics/traces/audit
12. dashboards/alerts
13. CI/CD
14. rollout/rollback
15. runbook/ownership
16. privacy/retention
17. capacity/performance
18. token/model/tool cost controls
19. dependency/license policy as applicable
20. release notes/known limitations

Every PASS requires evidence. Unknown is NOT VERIFIED, not PASS.
