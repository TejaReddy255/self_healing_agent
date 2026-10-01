from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"

# Must stay identical to the commands in S01-AC1 / CLAUDE.md "Commands".
REQUIRED_COMMANDS = [
    "uv sync --locked",
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uv run mypy backend frontend tools",
    'uv run pytest -m "not integration and not e2e and not live"',
    "uv run python tools/trace.py --strict",
]


def _checks_commands() -> list[str]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return [step["run"] for step in workflow["jobs"]["checks"]["steps"] if "run" in step]


@pytest.mark.spec("S01-AC10")
def test_ci_runs_the_s01_quality_commands() -> None:
    # All S01 commands, in order; later slices may add steps between them (S07: mypy mcp_servers).
    commands = iter(_checks_commands())
    assert all(any(c == required for c in commands) for required in REQUIRED_COMMANDS)


@pytest.mark.spec("S07-AC6")
def test_ci_type_checks_the_mcp_servers() -> None:
    assert "uv run mypy mcp_servers" in _checks_commands()


@pytest.mark.spec("S01-AC10")
def test_ci_runs_integration_tests_against_postgres_and_never_skips() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["integration"]
    assert job["services"]["postgres"]["image"].startswith("postgres:16")
    assert job["env"]["REQUIRE_INTEGRATION_DB"] == "1"
    assert [s["run"] for s in job["steps"] if "run" in s] == [
        "uv sync --locked",
        "docker compose -f deploy/docker-compose.dev.yml up -d --wait sales-db warehouse-db",
        "uv run pytest -m integration",
    ]


@pytest.mark.spec("S07-AC6")
def test_ci_provides_the_s07_data_containers_without_real_secrets() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    env = workflow["jobs"]["integration"]["env"]
    for key in (
        "MCP_POSTGRES_READER_DSN",
        "MCP_POSTGRES_WRITER_DSN",
        "MCP_MYSQL_READER_DSN",
        "MCP_MYSQL_WRITER_DSN",
        "MYSQL_ROOT_PASSWORD",
        "WAREHOUSE_PG_PASSWORD",
    ):
        assert "ci-ephemeral" in env[key], key
    assert "dw.customer.email" in env["MCP_PII_COLUMNS"]


@pytest.mark.spec("S01-AC10")
def test_ci_uses_python_312_and_read_only_permissions() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"contents": "read"}
    setup = next(s for s in workflow["jobs"]["checks"]["steps"] if "setup-uv" in s.get("uses", ""))
    assert setup["with"]["python-version"] == "3.12"
