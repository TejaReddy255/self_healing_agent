---
description: Perform the final release gate and prepare a controlled production deployment with evidence, release notes, migration/rollback steps, and post-deploy verification.
disable-model-invocation: true
---

# Release workflow

Do not release merely because implementation is finished.

1. Read the production-readiness report.
2. Re-run or inspect fresh evidence for critical checks.
3. Confirm:
   - version/tag strategy
   - environment config
   - secrets
   - migrations
   - backup/recovery where required
   - rollout strategy
   - rollback trigger and procedure
   - health/smoke checks
   - monitoring/alerts
4. Create/update `docs/release-checklist.md`.
5. Create release notes:
   - user-visible changes
   - internal changes
   - migrations/config
   - known limitations
   - rollback notes
6. Do not perform a production mutation unless the user explicitly authorizes it and the
   repository's deployment policy allows it.
7. After deployment, verify actual health and critical path behavior before calling it successful.
