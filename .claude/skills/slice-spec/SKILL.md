---
description: Refine a vertical-slice spec in docs/specs/ (spec-driven development) into an implementation-ready contract — exact interfaces, testable acceptance criteria, test mapping and reviewer routing — then hand it to a human for approval. Use before implement-slice; never writes production code.
---

# Slice spec (SDD step 1)

Use when a slice in `docs/specs/` is DRAFT or BLOCKED and is next in `docs/implementation-plan.md`,
or when an APPROVED spec must change. Argument: the slice ID (e.g. `S04`).

## Procedure

1. Read `docs/sdd-workflow.md` (§2 greenfield rules, §4 slice loop, §8 precedence), the slice
   file `docs/specs/<ID>-*.md`, every `source_sections` reference in
   `self_healing_platform_architecture_coding_agent.md`, the relevant sections and ADRs in `docs/architecture.md`,
   and `docs/agent-design.md` / `docs/evaluation-plan.md` if the slice is tagged `ai`.
   For DQ slices (S17, S18, S26–S34), also read `docs/dq-agents-design.md`.
2. For each `depends_on` slice, read its **Verification evidence** and inspect the actual code it
   delivered. Trust the code and evidence over status labels. Record any gap as an open issue.
3. Check `blocked_by` open questions in `docs/use-case-spec.md` §12. If a blocking question is still
   unanswered and has no accepted default, set `status: BLOCKED`, list precisely what is needed, and stop.
4. Make the contracts concrete: model fields and types, function signatures, endpoints (method, path,
   role, request, response, status codes), tables/columns/indexes/constraints, config keys with
   defaults (never for secrets).
5. Make every acceptance criterion observable and testable. Keep existing AC IDs stable. Add missing
   failure-path, idempotency and safety ACs. Each AC maps to a test layer in the Test plan.
6. Consult the relevant lens (you may delegate read-only analysis):
   - `ai` → `agent-architect` (tools/limits/state) and `evaluation-engineer` (eval cases and IDs)
   - `schema` → `data-engineer` (migration, indexes, locking)
   - `security-sensitive` → `security-reviewer` (threats specific to this slice)
   - `contract` → `solution-architect` (compatibility with other slices)
   - `dq-algorithm` → `evaluation-engineer` (E-DQ ground-truth cases and thresholds)
7. Check for conflicts with invariants, ADRs and other specs. If you find any, record them under "Open issues" and ask.
   Never resolve one silently.
8. Set `status: READY`, add a changelog line, and update the Status column in
   `docs/implementation-plan.md` §2.
9. Reply to the human with: a 5-line summary of the slice, any contract decisions made, open
   questions needing an answer, and a request to set `status: APPROVED` with `approved_by`/`approved_on`.

## Rules

- Do not write production code or tests in this skill.
- Do not expand scope. Move discovered work to a later spec (or propose a new one) and note it.
- Do not change another slice's ACs. Record cross-slice impacts as open issues in both specs.
- Specs describe behaviour and contracts. Implementation notes are guidance, not requirements.
- Next step after approval is `/implement-slice <ID>`.
