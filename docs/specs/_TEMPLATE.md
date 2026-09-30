---
id: Sxx
title: <short title>
status: DRAFT            # DRAFT | BLOCKED | READY | APPROVED | IN_PROGRESS | IN_REVIEW | DONE
phase: <0-6>
owner_agent: <backend-engineer | ai-engineer | data-engineer | frontend-engineer | devops-sre | qa-engineer | release-manager>
reviewers: [code-reviewer]          # plus tag-driven reviewers (docs/sdd-workflow.md §5)
depends_on: []
blocked_by: []                      # open-question IDs (OQ-xx)
traces: []                          # FR-/NFR-/UC-AC IDs
source_sections: []                 # "src §x.y" references
tags: []                            # ai | security-sensitive | schema | runtime | ui | contract
approved_by:
approved_on:
---

# Sxx — <title>

## Objective
<One outcome, one paragraph.>

## Read first
- `docs/sdd-workflow.md`, `docs/architecture.md` (relevant §/ADRs), this spec's `source_sections`
- Verification evidence of each dependency spec
- Existing code/tests in the affected modules

## In scope
-

## Out of scope
-

## Public contracts
<Models (fields/types), functions/protocols, endpoints (method, path, role, request, response, status codes), CLI commands.>

## Persistence and migrations
<Tables/columns/indexes/constraints. Migration number is assigned at merge time.>

## Configuration
<New settings: key, type, default (no secret defaults), validation.>

## Implementation notes (guidance, not requirements)
-

## Acceptance criteria
- **Sxx-AC1** …
- **Sxx-AC2** …

## Test plan
| AC | Layer (unit/contract/integration/e2e/eval) | Test(s) |
|---|---|---|
| Sxx-AC1 | | |

## Runtime verification
<Commands to run and what to observe, beyond automated tests.>

## Security and eval considerations
-

## Rollback
<How to revert safely (code revert, down-migration or compensating migration, feature flag).>

## Open issues
-

## Verification evidence
<Filled by test-and-verify. Use statuses PASS / FAIL / NOT VERIFIED / NOT APPLICABLE.>

| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- YYYY-MM-DD — created (DRAFT)
