# Self-Healing Pipeline Monitor

Agentic self-healing control plane for Airflow ETL pipelines, built with spec-driven development.

- Process and conventions: [`CLAUDE.md`](CLAUDE.md), [`docs/sdd-workflow.md`](docs/sdd-workflow.md)
- What is being built, in what order: [`docs/implementation-plan.md`](docs/implementation-plan.md)
- Source architecture: [`self_healing_platform_architecture_coding_agent.md`](self_healing_platform_architecture_coding_agent.md)

## Prerequisites

Python 3.12, [uv](https://docs.astral.sh/uv/), Git. Docker Desktop is needed from slice S02 onward.

## Quickstart

PowerShell (Windows):

```powershell
uv sync
Copy-Item .env.example .env
# Set PG_PASSWORD in .env to a password of your choice (local only; .env is git-ignored)
# Create an API token for yourself; put the printed JSON entry into config/api_tokens.json
uv run python tools/new_token.py you@example.com admin
docker compose --env-file .env -f deploy/docker-compose.dev.yml up -d   # Postgres on 127.0.0.1:55432
uv run python -m backend.db.migrate up
uv run uvicorn backend.main:app --reload            # http://127.0.0.1:8000/health
# In a second terminal: set AGENT_API_TOKEN in .env to the printed token, then
uv run streamlit run frontend/app.py                 # http://localhost:8501
```

bash:

```bash
uv sync
cp .env.example .env                                      # then set PG_PASSWORD in .env
uv run python tools/new_token.py you@example.com admin   # add the JSON entry to config/api_tokens.json
docker compose --env-file .env -f deploy/docker-compose.dev.yml up -d
uv run python -m backend.db.migrate up
uv run uvicorn backend.main:app --reload
uv run streamlit run frontend/app.py
```

`config/api_tokens.json` holds only SHA-256 digests of tokens and is git-ignored; see
`config/api_tokens.example.json` for the shape.

## Quality checks (same as CI)

```text
uv run ruff check .
uv run ruff format --check .
uv run mypy backend frontend tools
uv run pytest -m "not integration and not e2e and not live"
uv run pytest -m integration          # needs the dev Postgres container running
uv run python tools/trace.py --strict
```

Integration tests create and drop their own throwaway databases on the dev Postgres. They are skipped
when it is not running, except in CI (`REQUIRE_INTEGRATION_DB=1`), where they fail instead.
