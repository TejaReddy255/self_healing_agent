---
id: S08
title: LLM and agent runtime foundation
status: DRAFT
phase: 2
owner_agent: ai-engineer
reviewers: [code-reviewer, evaluation-engineer, security-reviewer]
depends_on: [S02, S03, S06]
blocked_by: []
traces: [FR-012, FR-111, NFR-005, NFR-011, NFR-022, NFR-030]
source_sections: ["src §6", "src §13 (Gemini, agent limits, LLM cost)", "src §15 (limits)"]
tags: [ai, security-sensitive]
approved_by:
approved_on:
---

# S08 — LLM and agent runtime foundation

## Objective
Provide one way to build a bounded, audited, structured-output agent on Gemini, with versioned
prompts, untrusted-content delimiting, cost accounting, and a scripted fake model so that every
later agent slice is testable without network access.

## In scope
- `backend/llm/factory.py` `build_llm(agent_role)`: `ChatGoogleGenerativeAI` in Vertex or API-key mode
  (`GOOGLE_GENAI_USE_VERTEXAI`), per-role model keys (agent-design §7), timeout, transient retry.
- `backend/llm/callbacks.py`: `LLMUsageAuditHandler` (tokens, estimated cost from
  `GEMINI_INPUT_USD_PER_1M`/`GEMINI_OUTPUT_USD_PER_1M`, latency, `prompt_version`) and `ToolAuditHandler`,
  both writing `operation_audit` via S02.
- `backend/agents/factory.py` `build_agent(AgentSpec) -> BoundedAgent`, where `AgentSpec` = {role, prompt_id,
  tools: list[ToolHandle], output_model: type[BaseModel], limits: {max_model_calls, max_tool_calls,
  recursion_limit, max_input_tokens, wall_clock_s}}. `BoundedAgent.run(inputs, ctx) -> AgentResult[T]`
  with typed errors `LimitExceeded`, `SchemaInvalid`, `ModelUnavailable`, `ToolError`. One repair
  attempt on schema-invalid output. The LangChain specifics are hidden behind this module (ADR-002).
- Tool adapter: wraps `ToolHandle` (S06) and Airflow tool functions (S05) as agent tools. Tool outputs
  are wrapped with `untrusted()` before reaching the model.
- `backend/prompts/registry.py`: `get_prompt(id) -> Prompt{id, version, text, sha256}`. `shared_safety@1`
  per agent-design §0. A snapshot test binds `(id, version) → sha256`.
- `backend/prompts/untrusted.py`: `untrusted(source, ref, content) -> str` producing
  `<untrusted source="…" ref="…">…</untrusted>`, with escaping of any `</untrusted` inside the content.
- `tests/fakes/chat_model.py`: a scripted fake (sequence of tool calls and final outputs; can emit invalid JSON or errors).
- APIs: `GET /llm-costs` (aggregate by day/agent/model), `GET /performance`.

## Out of scope
Concrete agents (S09+).

## Configuration
`GEMINI_MODEL` (+ per-role overrides), `GOOGLE_PROJECT_ID`, `GOOGLE_CLOUD_LOCATION`,
`GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_API_KEY: SecretStr | None`, `GEMINI_INPUT_USD_PER_1M`,
`GEMINI_OUTPUT_USD_PER_1M`, `GEMINI_PRICING_REFERENCE`, `LLM_TIMEOUT_S=120`, per-role limit keys.

## Acceptance criteria
- **S08-AC1** An agent with the scripted fake model executes tool calls and returns a validated `output_model` instance.
- **S08-AC2** Exceeding the model-call, tool-call, recursion or token-budget limit stops the run with `LimitExceeded(kind)`, which is audited. No partial output is returned as success.
- **S08-AC3** Invalid structured output triggers exactly one repair attempt. A second failure raises `SchemaInvalid`.
- **S08-AC4** Every model call writes an audit row with agent, model, prompt `id@version`, input and output tokens, estimated cost and latency. `/llm-costs` aggregates them correctly (fixture data).
- **S08-AC5** `untrusted()` neutralizes delimiter spoofing: content containing `</untrusted>` cannot close the block (test).
- **S08-AC6** Changing a prompt's text without bumping its version fails the snapshot test.
- **S08-AC7** No API key or credential appears in logs or audit rows during a run (log capture test).
- **S08-AC8** An agent's toolset is fixed at build time. A model request for a tool not in the set yields a tool error, audited as `TOOL_NOT_ALLOWED`, and no invocation.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1–AC3, AC8 | unit | `tests/unit/agents/test_bounded_agent.py` |
| AC4 | integration | `tests/integration/llm/test_usage_audit.py` |
| AC5, AC6 | unit | `tests/unit/prompts/` |
| AC7 | unit | `tests/unit/llm/test_no_secret_leak.py` |
| — | eval | E-RES-03, E-RES-04 (replay) |

## Runtime verification
A single live smoke call with the real Gemini config (`-m live`) that returns a trivial structured output. Record the tokens and cost row.

## Rollback
Revert code.

## Open issues
- From S05: wrap `backend.airflow.tools.AirflowReadTools`, which holds only the read-only `AirflowReader` Protocol. `get_task_logs` already redacts off the event loop and returns untrusted text, so wrap `TaskLog.content` in `untrusted()`. The tools reject DAGs outside their allowlist with `AirflowToolDenied`, which should be returned to the model as a tool error.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
