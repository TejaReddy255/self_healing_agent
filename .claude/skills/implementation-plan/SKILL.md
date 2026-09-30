---
description: Turn approved requirements and architecture into a dependency-aware vertical-slice implementation plan with test and verification gates.
---

# Implementation planning

Read the approved specs and architecture.

Create/update `docs/implementation-plan.md`.

Break delivery into vertical slices that each produce testable behavior.

For every slice define:
- objective
- files/components expected to change
- interfaces/contracts
- dependencies
- migration/config impact
- implementation tasks
- tests
- runtime verification
- security/eval considerations
- rollback strategy if risky
- completion evidence

Order slices to retire architecture/integration risk early.

Prefer:
1. skeleton + health/config
2. domain contracts/state
3. one end-to-end thin slice
4. external integrations
5. agent/model behavior
6. HITL/governance
7. observability/evals
8. hardening/deployment

Do not create hundreds of tiny tasks. Optimize for coherent, verifiable increments.
