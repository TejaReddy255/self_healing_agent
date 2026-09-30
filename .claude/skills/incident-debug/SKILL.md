---
description: Diagnose production or integration failures systematically using evidence, bounded hypotheses, safe read-only inspection first, and verified remediation.
---

# Incident/debug workflow

1. Capture symptom, impact, environment, start time, and correlation/run IDs.
2. Preserve evidence before changing state.
3. Inspect read-only signals:
   - logs
   - traces
   - metrics
   - config metadata
   - dependency health
   - recent changes
4. Build a short ranked hypothesis list.
5. Test the cheapest/highest-signal hypothesis first.
6. Identify root cause vs secondary errors.
7. Propose remediation and rollback.
8. Require approval before high-impact writes.
9. Apply the smallest safe fix.
10. Verify with the same signal that demonstrated failure plus an end-to-end check.
11. Add regression test/monitoring improvement.
12. Record concise incident notes.

Never claim "fixed" only because a mutation succeeded; verify the user-facing/system outcome.
