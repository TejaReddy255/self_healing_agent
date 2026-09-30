from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

DOMAIN = Path(__file__).resolve().parents[2] / "backend" / "domain"
# Pure contracts: the standard library, pydantic and the domain package itself.
ALLOWED_THIRD_PARTY = {"pydantic", "pydantic_core"}


def imported_modules(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: only within backend.domain itself
                name = "backend.domain" if node.level == 1 else "<outside backend.domain>"
                found.append((node.lineno, name))
            elif node.module:
                found.append((node.lineno, node.module))
    return found


def is_allowed(module: str) -> bool:
    top = module.split(".")[0]
    return (
        module == "backend.domain"
        or module.startswith("backend.domain.")
        or top in sys.stdlib_module_names
        or top == "__future__"
        or top in ALLOWED_THIRD_PARTY
    )


def violations() -> list[str]:
    problems: list[str] = []
    for path in sorted(DOMAIN.rglob("*.py")):
        for line, module in imported_modules(path):
            if not is_allowed(module):
                problems.append(f"{path.relative_to(DOMAIN.parent.parent)}:{line} imports {module}")
    return problems


@pytest.mark.spec("S03-AC6")
def test_domain_imports_nothing_from_other_backend_packages() -> None:
    assert violations() == []


@pytest.mark.spec("S03-AC6")
def test_checker_flags_forbidden_imports(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from backend.services import audit_service\nimport fastapi\nfrom .. import config\n"
        "from . import common\nimport json\nfrom pydantic import BaseModel\n",
        encoding="utf-8",
    )
    verdicts = [(module, is_allowed(module)) for _, module in imported_modules(sample)]
    assert verdicts == [
        ("backend.services", False),
        ("fastapi", False),
        ("<outside backend.domain>", False),
        ("backend.domain", True),
        ("json", True),
        ("pydantic", True),
    ]
