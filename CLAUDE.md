# Self-Healing Pipeline Monitor — agent guide

Greenfield build of the platform in
[`self_healing_platform_architecture_coding_agent.md`](self_healing_platform_architecture_coding_agent.md)
("src"), delivered with **spec-driven development (SDD)**. Process: [`docs/sdd-workflow.md`](docs/sdd-workflow.md).

## Before any code change

1. Find the slice you are working on in [`docs/implementation-plan.md`](docs/implementation-plan.md).
   Its spec is `docs/specs/Sxx-*.md`.
2. **Its status must be `APPROVED` (or later).** If it is DRAFT, READY or BLOCKED, run `/slice-spec Sxx` and stop for human approval.
3. Implement only what the spec's scope and acceptance criteria require. Discovered work goes into a later spec.
4. Tag tests with `@pytest.mark.spec("Sxx-ACn")`, then record verification evidence in the spec (`/test-and-verify`).
5. Get the reviews the spec's `tags` require (`docs/sdd-workflow.md` §5).

## Source of truth (highest first)

1. Invariants below (src §36) and safety rules in src §0.1.
2. ADRs in [`docs/architecture.md`](docs/architecture.md) §19.
3. The APPROVED slice spec.
4. [`docs/use-case-spec.md`](docs/use-case-spec.md), [`docs/agent-design.md`](docs/agent-design.md), `docs/architecture.md` body.
5. The source architecture body.

If two sources conflict, stop and ask. Do not pick one silently.

## Greenfield rule (ADR-001)

There is no pre-existing implementation. Source "CURRENT" = baseline capability to build. Source
"known gaps" (src §16) are built directly in their target form. "Preserve legacy X" does not apply
unless `docs/architecture.md` §6 adopts X. Jira and Confluence are not built.

**DQ agents (ADR-013):** the seven DQ capabilities (File Manager, Preprocessing, Profiling, Custom DQ, Recon,
Statistical Check, Anomaly Detection) are built here as an independent DQ service in `dq_agents/` + `dq_service/`
(design: [`docs/dq-agents-design.md`](docs/dq-agents-design.md)). `backend/` never contains DQ algorithms and reaches the
service only through adapters. No imports cross in either direction. The DQ service has no LLM dependency.

## Invariants (src §36, non-negotiable)

1. Airflow live state beats static metadata.
2. Current DQ runtime evidence beats historical examples.
3. Monitoring never performs remediation.
4. Diagnostic/RCA agents remain read-only.
5. Writes require the exact human-approved revision/hash.
6. A tool call success is not business verification.
7. RESOLVED requires verified remediation + relevant DQ pass + verified Airflow recovery.
8. Static metadata is version-controlled knowledge, not a runtime-state cache.
9. Jira/Confluence are future connectors, not current dependencies.
10. Existing DQ algorithms are reused through adapters, never reimplemented. (Here: DQ algorithms live only in `dq_agents/`; the backend uses adapters.)
11. The frontend talks only to FastAPI. It never reaches repositories, Airflow or MCP directly.
12. Every material state change is auditable and correlated to incident/review/job IDs.

## Layout (see `docs/architecture.md` §3)

`backend/{api,domain,repositories,services,db,metadata,airflow,mcp,llm,agents,prompts,sop,dq,monitoring,jobs,security,observability}`,
`frontend/`, `dq_agents/` (DQ algorithms), `dq_service/` (DQ API, jobs, File Manager), `mcp_servers/`, `metadata/` (YAML), `migrations/NNNN_*.sql`, `config/`, `integrations/airflow/`,
`tests/{unit,contract,integration,e2e,evals,fakes}`, `tools/`, `deploy/`, `docs/`.

## Conventions

- Routes are thin; orchestration lives in services; SQL only in repositories; `backend/domain` imports nothing from `backend`.
- Schema changes go in a new numbered migration. Never edit an applied one, and the app never runs DDL.
- Every external call has a timeout. Write tools are never auto-retried.
- Never log or persist secrets. Wrap untrusted content for prompts with `untrusted()`.
- Prompts carry `id@version`. A behavioural prompt change bumps the version and runs the affected evals.
- Commit messages start with the slice ID (`S09: …`), on branch `slice/Sxx-slug`.

## Commands

_To be filled in by S01._ Expected shape:

```text
uv sync
uv run ruff check . ; uv run ruff format --check . ; uv run mypy backend frontend tools
uv run pytest -m "not integration and not e2e and not live"
uv run python tools/trace.py --strict
docker compose -f deploy/docker-compose.dev.yml up -d
uv run python -m backend.db.migrate up
uv run uvicorn backend.main:app --reload
uv run streamlit run frontend/app.py
```
