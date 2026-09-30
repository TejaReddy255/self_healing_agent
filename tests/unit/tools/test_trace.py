from __future__ import annotations

from pathlib import Path

import pytest

from tools import trace

pytestmark = pytest.mark.spec("S01-AC9")

# Fixture specs use S9x IDs so the real `tools/trace.py` run over this repo never confuses them
# with genuine acceptance criteria.
SPEC_TEMPLATE = """---
id: S98
status: {status}
---
# S98 fixture
- **S98-AC1** first
- **S98-AC2** second
- **S98-AC3** third

## Verification evidence
| Check | Command | Result | Evidence / notes |
|---|---|---|---|
| runtime | uvicorn | PASS | {runtime} |
"""

TEST_FILE = """
import pytest

@pytest.mark.spec("S98-AC1", "S98-AC2")
def test_something():
    pass
"""


def make_repo(tmp_path: Path, status: str, runtime: str) -> tuple[Path, Path]:
    specs = tmp_path / "specs"
    tests = tmp_path / "tests"
    specs.mkdir()
    tests.mkdir()
    (specs / "S98-fixture.md").write_text(
        SPEC_TEMPLATE.format(status=status, runtime=runtime), encoding="utf-8"
    )
    (specs / "_TEMPLATE.md").write_text("**S97-AC1** ignored template", encoding="utf-8")
    (tests / "test_fixture.py").write_text(TEST_FILE, encoding="utf-8")
    return specs, tests


def run(specs: Path, tests: Path, *extra: str) -> int:
    return trace.main(["--specs-dir", str(specs), "--tests-dir", str(tests), *extra])


def test_matrix_lists_every_ac(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    specs, tests = make_repo(tmp_path, "DONE", "n/a")
    run(specs, tests)
    out = capsys.readouterr().out
    assert "S98-AC1" in out and "S98-AC2" in out
    assert "S98-AC3" in out and "UNCOVERED" in out
    assert "S97-AC1" not in out  # template ignored


def test_strict_fails_for_done_spec_with_uncovered_ac(tmp_path: Path) -> None:
    specs, tests = make_repo(tmp_path, "DONE", "n/a")
    assert run(specs, tests, "--strict") == 1


def test_strict_passes_when_runtime_verified(tmp_path: Path) -> None:
    specs, tests = make_repo(tmp_path, "DONE", "runtime-verified: S98-AC3")
    assert run(specs, tests, "--strict") == 0


def test_strict_ignores_specs_not_done(tmp_path: Path) -> None:
    specs, tests = make_repo(tmp_path, "IN_PROGRESS", "n/a")
    assert run(specs, tests, "--strict") == 0


def test_non_strict_never_fails(tmp_path: Path) -> None:
    specs, tests = make_repo(tmp_path, "DONE", "n/a")
    assert run(specs, tests) == 0


def test_real_repository_specs_parse() -> None:
    repo = Path(__file__).resolve().parents[3]
    specs = trace.load_specs(repo / "docs" / "specs")
    s01 = next(s for s in specs if s.spec_id == "S01")
    assert s01.acs[0] == "S01-AC1"
    assert len(s01.acs) == 11
