---
description: Implement one planned vertical slice completely, including production code, tests, error handling, observability, documentation updates, and verification.
---

# Implement a vertical slice

1. Identify the exact slice from `docs/implementation-plan.md`.
2. Inspect existing code and tests before editing.
3. Confirm contracts and architecture affected.
4. Implement the smallest complete change.
5. Add/update:
   - validation
   - error handling
   - structured logs/traces where needed
   - tests
   - config/example config
   - migration if required
6. Run the narrowest fast checks first.
7. Run broader relevant checks after the slice works.
8. If a check fails, fix the cause; do not mask or delete the test.
9. Update docs/plan status with verification evidence.
10. Request code review for meaningful changes.

Never mark a slice complete only because code compiles.
