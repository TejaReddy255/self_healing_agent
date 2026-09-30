---
id: S01
title: Project skeleton, config, logging, auth scaffold, health, CI
status: IN_PROGRESS
phase: 0
owner_agent: devops-sre
reviewers: [code-reviewer, security-reviewer]
depends_on: []
blocked_by: []
traces: [NFR-001, NFR-002, NFR-003, NFR-060, NFR-061]
source_sections: ["src §0.1", "src §0.7", "src §5.1", "src §13", "src §16 (CORS, credential defaults)"]
tags: [runtime, security-sensitive]
approved_by: product owner
approved_on: 2026-09-30
---

# S01 — Project skeleton, config, logging, auth scaffold, health, CI

## Objective
Create a runnable, tested, lint-clean repository skeleton for the backend, the Streamlit frontend
and the tooling. It includes fail-fast configuration, redacted structured logging with correlation IDs,
token-based role auth, health endpoints, the traceability tool and CI, so that every later slice
only adds behaviour.

## Read first
- `docs/architecture.md` §3, §4, §10, §12, §14, ADR-001, ADR-002
- `.claude/rules/python.md`, `.claude/rules/tests.md`

## In scope
- `git init`, `.gitignore` (Python, `.env*` except `.env.example`, `reports/`, `.venv/`).
- `pyproject.toml` managed by `uv`: Python 3.12; runtime deps per ADR-002 needed now (fastapi,
  uvicorn, pydantic, pydantic-settings, httpx, streamlit); dev deps (pytest, pytest-asyncio, respx,
  ruff, mypy). Pinned `uv.lock`. Register pytest markers `spec(id)`, `integration`, `e2e`, `eval`, `live`.
- Package skeleton with `__init__.py` for every directory in `docs/architecture.md` §3 that the
  backend will need (empty modules allowed; no placeholder logic).
- `backend/config.py`: `Settings` (pydantic-settings) with `ENV`, `SERVICE_NAME`, `VERSION`,
  `LOG_LEVEL`, `CORS_ALLOW_ORIGINS: list[str]`, `API_TOKENS_FILE: Path | None`, `API_TOKENS: SecretStr | None`.
  Validation: in `prod`, CORS must not contain `*` and a token source is required.
- `backend/observability/logging.py`: JSON formatter; `request_id` contextvar; `RedactionFilter`
  masking values whose key matches `(?i)password|passwd|secret|token|api[_-]?key|authorization|credential|dsn|connection[_-]?string`
  and inline patterns (`Bearer …`, `postgres://user:pass@`, `mysql://user:pass@`).
- `backend/observability/middleware.py`: reads/creates `X-Request-ID`, binds it to the log context, and echoes it in the response.
- `backend/security/auth.py`: `Role` enum (`viewer < operator < reviewer < admin`; `service` separate);
  `Actor(subject, role)`; bearer token → Actor from the tokens file/env (format `{token_sha256: {subject, role}}`,
  so plaintext tokens are never stored in config); `require_role(min_role)` / `require_service()` dependencies;
  401 vs 403 semantics.
- `backend/app.py` `create_app(settings)` with lifespan hook (no external connections yet),
  CORS middleware, request-ID middleware, exception handlers returning `{error_code, message, request_id}`.
- Routes: `GET /` → `{service, version, status: "ok"}`; `GET /health` (no auth) → liveness;
  `GET /health/ready` (viewer) → `{status, components: {}}` backed by a `ReadinessRegistry` that later slices register checks into.
- `frontend/app.py` + `frontend/api_client.py`: Streamlit shell showing backend readiness. The client has
  configurable `AGENT_BASE_URL`, timeout and token, and renders friendly errors.
- `tools/trace.py`: parse `docs/specs/S*.md` for `**Sxx-ACn**`, scan `tests/` for
  `@pytest.mark.spec("…")`, parse each spec's "Verification evidence" rows marked `runtime-verified: Sxx-ACn`,
  and print the matrix. `--strict` exits 1 if any `status: DONE` spec has an uncovered AC.
- `.env.example`, `README.md` quickstart (Windows PowerShell and bash), `.github/workflows/ci.yml`
  (uv sync, ruff check, ruff format --check, mypy, pytest -m "not integration and not e2e and not live").
- `deploy/docker-compose.dev.yml` with the operational PostgreSQL only (used from S02).
- Update the **Commands** section of `CLAUDE.md`.

## Out of scope
DB connectivity (S02), OIDC (S23), metrics endpoint (S23), any domain logic.

## Public contracts
- `create_app(settings: Settings | None = None) -> FastAPI`
- `require_role(min_role: Role) -> Callable[..., Actor]`, `require_service() -> Callable[..., Actor]`
- `ReadinessRegistry.register(name: str, check: Callable[[], Awaitable[ComponentStatus]])`
- `GET /` 200; `GET /health` 200 `{status:"ok"}`; `GET /health/ready` 200/503 `{status, components}`
- Error envelope: `{error_code: str, message: str, request_id: str}`

## Persistence and migrations
None.

## Configuration
As listed above. No secret has a default value.

## Acceptance criteria
- **S01-AC1** On a clean clone, `uv sync` succeeds, and `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy backend frontend tools` and `uv run pytest` all pass.
- **S01-AC2** `uv run uvicorn backend.main:app` starts. `GET /` returns service, version and `status: ok`. `GET /health` returns 200 without auth.
- **S01-AC3** With `ENV=prod`, startup fails with a clear configuration error when no token source is configured or when `CORS_ALLOW_ORIGINS` contains `*`. No setting representing a credential has a default value (asserted by a test that introspects `Settings`).
- **S01-AC4** Every request produces JSON log lines containing `request_id`. An inbound `X-Request-ID` is propagated and echoed. A missing one is generated.
- **S01-AC5** The redaction filter masks sensitive keys and inline credential patterns in messages and extras (table-driven test with ≥ 10 cases).
- **S01-AC6** `require_role` returns 401 for missing or invalid tokens and 403 for insufficient role. Role ordering viewer < operator < reviewer < admin holds, and `service` is accepted only by `require_service`.
- **S01-AC7** CORS preflight from a non-allowlisted origin is rejected. An allowlisted origin is accepted.
- **S01-AC8** The Streamlit shell starts (`streamlit run frontend/app.py`) and shows readiness, or an actionable message when the backend is unreachable or times out (AppTest).
- **S01-AC9** `tools/trace.py` prints the AC → test matrix. `--strict` fails for a fixture DONE spec with an uncovered AC and passes when covered.
- **S01-AC10** The CI workflow runs the same commands as S01-AC1.
- **S01-AC11** Unhandled exceptions return the error envelope with `request_id` and no stack trace, and log the stack trace server-side.

## Test plan
| AC | Layer | Test(s) |
|---|---|---|
| AC1, AC10 | CI | workflow run |
| AC2, AC4, AC7, AC11 | unit (TestClient) | `tests/unit/test_app.py` |
| AC3 | unit | `tests/unit/test_config.py` |
| AC5 | unit | `tests/unit/test_redaction.py` |
| AC6 | unit | `tests/unit/test_auth.py` |
| AC8 | unit (AppTest) | `tests/unit/frontend/test_shell.py` |
| AC9 | unit | `tests/unit/tools/test_trace.py` |

## Runtime verification
Start the API and Streamlit locally. Stop the API and confirm the Streamlit error message. Check one log line for `request_id`.

## Security and eval considerations
Token file stores SHA-256 of tokens only. No secrets in `.env.example`. The redaction filter is installed on the root logger.

## Rollback
Not applicable (initial slice).

## Open issues
- OQ-05 decides whether S23 replaces token auth with OIDC; the `Actor` abstraction must survive that change.

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|

## Changelog
- 2026-09-29 — created (DRAFT)
- 2026-09-30 — APPROVED by product owner (without a separate `/slice-spec` pass; spec judged implementation-ready)
