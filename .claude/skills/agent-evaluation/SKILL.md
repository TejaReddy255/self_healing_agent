---
description: Build and run an evaluation framework for LLM/agent behavior using golden, edge, adversarial, and failure scenarios with explicit metrics and release thresholds.
---

# Agent evaluation

Create/update `docs/evaluation-plan.md` and, where appropriate, versioned eval fixtures.

## Dataset categories
- representative happy paths
- ambiguous inputs
- edge cases
- malformed inputs
- tool/provider failures
- prompt injection
- unauthorized action attempts
- human approval/rejection
- resume/retry/idempotency
- cost/latency stress cases

## Metrics
Choose metrics tied to the use case:
- task success
- structured-output validity
- correct tool selection
- tool argument correctness
- groundedness/factual consistency
- policy/approval compliance
- recovery success
- latency
- model/tool calls
- tokens/cost

## Grading
Prefer deterministic graders. For model graders:
- provide a narrow rubric
- lock/version the grader config
- spot-check samples
- avoid circular grading by the same prompt under test where feasible

## Release threshold
Define explicit thresholds and a regression rule.

Every production model/prompt/tool-policy change should run the relevant regression evals.
