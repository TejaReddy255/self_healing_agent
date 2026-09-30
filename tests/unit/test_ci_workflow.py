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


@pytest.mark.spec("S01-AC10")
def test_ci_runs_the_s01_quality_commands() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["checks"]["steps"]
    commands = [step["run"] for step in steps if "run" in step]
    assert commands == REQUIRED_COMMANDS


@pytest.mark.spec("S01-AC10")
def test_ci_runs_integration_tests_against_postgres_and_never_skips() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["integration"]
    assert job["services"]["postgres"]["image"].startswith("postgres:16")
    assert job["env"]["REQUIRE_INTEGRATION_DB"] == "1"
    assert [s["run"] for s in job["steps"] if "run" in s] == [
        "uv sync --locked",
        "uv run pytest -m integration",
    ]


@pytest.mark.spec("S01-AC10")
def test_ci_uses_python_312_and_read_only_permissions() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"contents": "read"}
    setup = next(s for s in workflow["jobs"]["checks"]["steps"] if "setup-uv" in s.get("uses", ""))
    assert setup["with"]["python-version"] == "3.12"
