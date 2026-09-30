---
description: Convert a raw application or agentic-AI idea into a production-oriented use-case specification with requirements, actors, constraints, success metrics, and acceptance criteria.
---

# Idea to specification

Use this when starting from a use-case idea.

## Procedure

1. Inspect existing `docs/` and repository context first.
2. State the problem in one paragraph.
3. Identify actors/personas and their goals.
4. Describe current workflow and proposed workflow.
5. Define in-scope and out-of-scope behavior.
6. Write numbered functional requirements (`FR-###`).
7. Write non-functional requirements (`NFR-###`) for:
   - security/privacy
   - availability/recovery
   - performance/latency
   - scale/concurrency
   - cost
   - auditability
   - accessibility/usability
8. Define data sources, sensitivity/classification, retention, and ownership.
9. For AI-assisted decisions, classify each as:
   - deterministic
   - LLM-assisted
   - human-approved
10. Define measurable product/AI success metrics.
11. Write testable acceptance criteria.
12. Record assumptions and open questions.
13. Save/update `docs/use-case-spec.md`.

Do not invent missing business rules. Use explicit assumptions where necessary.
