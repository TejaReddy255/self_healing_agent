---
description: Design a production-ready application architecture from an approved use-case specification, including components, interfaces, persistence, security, reliability, observability, and deployment.
---

# Architecture workflow

Read `docs/use-case-spec.md` and relevant repository files.

Produce/update `docs/architecture.md` with:

1. Architectural goals and constraints.
2. System context and external dependencies.
3. Component diagram (Mermaid is acceptable) and responsibility table.
4. Request/event flows for critical journeys.
5. Domain/API contracts.
6. Persistence/state and schema ownership.
7. Async/background processing if justified.
8. Authentication and authorization.
9. Agent/model/tool boundaries.
10. Configuration and secret management.
11. Reliability:
    - timeout
    - retry
    - idempotency
    - circuit breaking if needed
    - checkpoint/recovery
12. Observability:
    - logs
    - metrics
    - traces
    - audit events
13. Deployment topology and environments.
14. Scaling and cost considerations.
15. Threats and mitigations.
16. Alternatives rejected.
17. ADRs for decisions that would be expensive to reverse.

Prefer simple, explicit designs over premature microservices.
