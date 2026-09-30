---
description: Design an agentic workflow with typed state, explicit nodes, tools, permissions, structured outputs, human gates, guardrails, retries, checkpointing, observability, and termination.
---

# Agent design

Create/update `docs/agent-design.md`.

For each workflow:

## Goal
Define success and terminal failure conditions.

## State
Specify a typed state schema. Separate:
- user/request data
- workflow status
- evidence
- model outputs
- tool results
- approval state
- error/retry counters

## Nodes / agents
For each node specify:
- responsibility
- deterministic vs model-assisted
- input
- output
- allowed tools
- error transition

## Tools
Create a matrix:
`tool | purpose | read/write | scope | validation | timeout | idempotent | approval required`

Keep read and write tools distinct when possible.

## Models
Define:
- task -> model class
- structured output schema
- prompt version
- retry/fallback
- max calls
- token/context budget

## Safety
Define:
- untrusted input boundaries
- prompt injection treatment
- argument validation
- approval boundary
- max steps/model calls/tool calls
- prohibited actions

## Resilience
Define:
- checkpointing
- resume behavior
- retries
- compensation/rollback
- deduplication/idempotency

## Observability
Trace at least:
`run_id, user/request_id, node, model, prompt_version, tool, latency, tokens/cost,
approval_decision, outcome, error_code`.

## Evaluation
List normal, edge, adversarial, and failure scenarios before implementation.
