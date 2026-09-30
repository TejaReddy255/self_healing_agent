---
id: S01
title: Project skeleton, config, logging, auth scaffold, health, CI
status: DONE
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
- **S01-AC4** Every request produces JSON log lines containing `request_id`. An inbound `X-Request-ID` is propagated and echoed. A missing one is generated. *(Clarified 2026-09-30 after code review: "every request" means every request that reaches the application. CORS preflight `OPTIONS` requests are answered by the outermost CORS middleware and are not correlated, which keeps CORS headers on 500 responses.)*
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
Token file stores SHA-256 of tokens only. No secrets in `.env.example`. The redaction filter is attached to the
root logger's **handler**, not to the logger itself: logger-level filters do not see records propagated from child
loggers. **Rule for later slices:** every log handler (including OTel/Sentry in S23) must be created with
`backend.observability.logging.build_handler`, which attaches the redaction filter. `ENV` is required, so a
deployment cannot silently fall back to dev rules. Config validation errors never echo input values. API docs and
OpenAPI are disabled when `ENV=prod`.

## Rollback
Not applicable (initial slice).

## Open issues
- OQ-05 decided (2026-09-30): hashed tokens now, OIDC for humans in S23. The `Actor` abstraction is the seam.
- Starlette emits a deprecation warning that `starlette.testclient` will require `httpx2`. It is harmless now;
  revisit when upgrading FastAPI/Starlette.
- Streamlit binds on all interfaces by default. Local dev only; production binding is decided in S25.
- Deferred from the S01 security review to **S23**: F6 (refuse plain `http://` for a remote `AGENT_BASE_URL` unless
  explicitly allowed, which is needed for Compose-internal `http://api:8000`) and F7 remainder (pin CI actions to
  commit SHAs; add dependency audit and gitleaks secret scan). Recorded in `S23` scope.

## Verification evidence
Run on 2026-09-30, Windows 11, Python 3.12.10, uv 0.12.21, branch `slice/S01-project-skeleton`, commit d4cb0da.

| Check | Command | Result | Evidence / notes |
|---|---|---|---|
| Install | `uv sync` (lockfile `uv.lock` committed) | PASS | runtime-verified: S01-AC1 — clean install into `.venv`; the CI uses `uv sync --locked` |
| Lint | `uv run ruff check .` | PASS | "All checks passed!" (`.claude/` hook scripts excluded as external tooling) |
| Format | `uv run ruff format --check .` | PASS | 52 files already formatted. Markdown excluded after ruff rewrote code blocks in the source doc (reverted, see changelog) |
| Types | `uv run mypy backend frontend tools` | PASS | strict mode, "no issues found in 35 source files" |
| Unit tests | `uv run pytest -m "not integration and not e2e and not live"` | PASS | 69 passed in ~4 s. Covers AC2–AC11 (see `tools/trace.py` matrix) |
| Traceability | `uv run python tools/trace.py --strict` | PASS | every S01 AC is covered by a test except AC1 (runtime-verified here) |
| API runtime | `uv run uvicorn backend.main:app --port 8765` + curl | PASS | runtime-verified: S01-AC2 — `GET /` → service/version/ok; `/health` → 200 unauthenticated; `/health/ready` → 401 without token, 200 `{"status":"ok","components":{}}` with a viewer token; unknown path → 404 envelope with `request_id` |
| Log format | server log inspection | PASS | 0 non-JSON lines; `X-Request-ID: rt-check-1` echoed in the response and present on the matching `request completed` log line |
| Streamlit runtime | `uv run streamlit run frontend/app.py --server.headless true --server.port 8599` | PASS | `/_stcore/health` → ok, page → 200. Rendering and error states are covered by the AppTest suite |
| CI workflow | `.github/workflows/ci.yml` | NOT VERIFIED (remote) | Commands asserted equal to AC1 by `tests/unit/test_ci_workflow.py`. No GitHub remote exists yet, so no hosted CI run |
| Cold-start flake | full test run on a fresh venv | FIXED | The first `AppTest` rendering `st.table` timed out (15 s) because Streamlit lazily imports pandas/pyarrow. Fixed with a session warm-up fixture plus `UV_COMPILE_BYTECODE=1` in CI; the timeout was not raised |
| Code review | `code-reviewer` agent on d4cb0da | PASS (no High) | 3 Medium + 7 Low. Fixed: test isolation from a local `.env` (reproduced and fixed: 91 pass with a README-style `.env` present); a malformed log call no longer raises from the redaction filter; URL passwords containing `@`/`/` and the `authorization/credential/dsn` keys are now redacted; non-JSON list settings give a clear error; a cancelled request is logged as `no_response` instead of 500; `trace.py` parses tests with `ast` so markers in comments/strings no longer count; the frontend handles non-object payloads and invalid settings; version read from package metadata; warning when prod has zero tokens. Spec clarified: AC4 (preflight), security note (handler rule) |
| Security review | `security-reviewer` agent on d4cb0da | PASS (no Critical/High) | 1 Medium + 6 Low + 1 Info. Fixed: F1 prefixed and quoted keys (`PG_PASSWORD=`, `access_token=`, `"token": "x"`); F2 non-primitive extras rendered then redacted; F3 `hide_input_in_errors` plus no chained pydantic error; F4 `/docs`, `/redoc`, `/openapi.json` off in prod; F5 `ENV` required; F7 uv version pinned in CI; F8 `cookie`/`session` keys plus `.envrc`, `*.p12`, `*.pfx` ignored. Deferred to S23: F6, F7 remainder |
| Re-run after fixes | lint, format, mypy, pytest, trace | PASS | 91 passed in ~6 s; mypy strict clean; `trace.py --strict` OK |
| Runtime after fixes | missing `ENV`; `ENV=prod` uvicorn on :8766 | PASS | missing `ENV` → `ConfigurationError: Invalid configuration: env: Field required`; prod: `/` 200, `/health` 200, `/docs` 404, `/openapi.json` 404, `/health/ready` 200 with a viewer token; 0 non-JSON log lines, no `color_message` noise |

## Changelog
- 2026-09-29 — created (DRAFT)
- 2026-09-30 — APPROVED by product owner (without a separate `/slice-spec` pass; spec judged implementation-ready)
- 2026-09-30 — implemented (commit d4cb0da). Additions beyond the listed scope, both small and supportive:
  `tools/new_token.py` (generates a token plus its hashed file entry) and `config/api_tokens.example.json`.
  Ruff configured to skip `.claude/` and Markdown. IN_REVIEW.
- 2026-09-30 — code and security review findings fixed (see evidence). `ENV` made required (strengthens AC3). AC4
  wording clarified for CORS preflights. F6 and the F7 remainder moved to S23. Awaiting human acceptance for DONE.
- 2026-09-30 — accepted by product owner: `ENV` required kept; AC4 preflight clarification accepted; F6 and the F7
  remainder deferred to S23. Status DONE; merged to `main`.
