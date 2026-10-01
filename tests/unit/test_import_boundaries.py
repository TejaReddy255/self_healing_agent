from __future__ import annotations

import ast
import re
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


# --- S04-AC6: only backend/metadata reads the metadata directory ------------------------------

REPO = DOMAIN.parent.parent
METADATA_PACKAGE = REPO / "backend" / "metadata"
# The hardened YAML parser shared with config files (S06); it never opens METADATA_DIR itself.
SHARED_YAML_PARSER = REPO / "backend" / "yaml_safe.py"
SCANNED = [
    p
    for p in (REPO / name for name in ("backend", "frontend", "tools", "dq_agents", "dq_service",
                                       "mcp_servers", "integrations"))
    if p.is_dir()
]  # fmt: skip
FILE_READERS = {"open", "read_text", "read_bytes"}
# A path into the tree, e.g. "metadata/sops/x.yaml"; the bare word "metadata" is a route or name.
METADATA_PATH = re.compile(r"(^|[/\\])metadata[/\\]")
METADATA_NAMES = {"metadata_dir", "METADATA_DIR", "DEFAULT_METADATA_DIR", "resolved_dir"}
YAML_LOADERS = {"load", "safe_load", "full_load", "unsafe_load", "load_all", "safe_load_all"}


def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                ids.add(id(value))
    return ids


def metadata_file_access(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    docstrings = _docstrings(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and METADATA_PATH.search(node.value)
        ):
            found.append(f"{node.lineno}: path literal {node.value!r}")
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            segment = ast.get_source_segment(source, node) or ""
            if name in FILE_READERS and "metadata" in segment.lower():
                found.append(f"{node.lineno}: {name}() on a metadata path")
        if isinstance(node, ast.Attribute):
            if node.attr in METADATA_NAMES:
                found.append(f"{node.lineno}: .{node.attr}")
            if (
                node.attr in YAML_LOADERS
                and isinstance(node.value, ast.Name)
                and node.value.id == "yaml"
            ):
                found.append(f"{node.lineno}: yaml.{node.attr}")
        elif isinstance(node, ast.Name) and node.id in METADATA_NAMES:
            found.append(f"{node.lineno}: {node.id}")
        elif isinstance(node, ast.ImportFrom) and node.module == "yaml":
            found.append(f"{node.lineno}: from yaml import ...")
    return found


@pytest.mark.spec("S04-AC6")
def test_only_the_metadata_package_reads_metadata_files() -> None:
    problems = [
        f"{path.relative_to(REPO)}:{hit}"
        for root in SCANNED
        for path in sorted(root.rglob("*.py"))
        if not path.is_relative_to(METADATA_PACKAGE) and path != SHARED_YAML_PARSER
        for hit in metadata_file_access(path)
    ]
    assert problems == []


@pytest.mark.spec("S04-AC6")
def test_metadata_boundary_checker_flags_direct_reads(tmp_path: Path) -> None:
    sample = tmp_path / "sneaky.py"
    sample.write_text(
        "import yaml\nfrom backend.metadata.settings import DEFAULT_METADATA_DIR\n"
        "data = yaml.safe_load((DEFAULT_METADATA_DIR / 'pipelines/x.yaml').read_text())\n"
        "path = settings.metadata_dir\n"
        "raw = Path('metadata/pipelines/x.yaml').read_bytes()\n"
        "handle = open(metadata_root / 'sops' / 'a.yaml')\n",
        encoding="utf-8",
    )
    hits = metadata_file_access(sample)
    assert any("yaml.safe_load" in h for h in hits)
    assert any("DEFAULT_METADATA_DIR" in h for h in hits)
    assert any(".metadata_dir" in h for h in hits)
    assert any("path literal 'metadata/pipelines/x.yaml'" in h for h in hits)
    assert any("read_bytes()" in h for h in hits)
    assert any("open()" in h for h in hits)


# --- S06-AC9: only remediation code may build the execution toolset ---------------------------

EXECUTION_TOOLSET_ALLOWED = ("backend/services/remediation", "backend/jobs/remediation")


def execution_toolset_references(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "execution_toolset" for alias in node.names
        ):
            found.append(f"{node.lineno}: from {node.module} import execution_toolset")
        elif isinstance(node, ast.Attribute) and node.attr == "execution_toolset":
            found.append(f"{node.lineno}: .execution_toolset")
        elif isinstance(node, ast.Name) and node.id == "execution_toolset":
            found.append(f"{node.lineno}: execution_toolset")
    return found


@pytest.mark.spec("S06-AC9")
def test_only_remediation_code_can_reach_the_execution_toolset() -> None:
    definition = REPO / "backend" / "mcp" / "toolsets.py"
    problems = []
    for root in SCANNED:
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(REPO).as_posix()
            if path == definition or relative.startswith(EXECUTION_TOOLSET_ALLOWED):
                continue
            problems.extend(f"{relative}:{hit}" for hit in execution_toolset_references(path))
    assert problems == []


@pytest.mark.spec("S06-AC9")
def test_execution_toolset_checker_flags_imports(tmp_path: Path) -> None:
    sample = tmp_path / "sneaky.py"
    sample.write_text(
        "from backend.mcp.toolsets import execution_toolset\n"
        "import backend.mcp.toolsets as t\nt.execution_toolset(m, None, 'mysql')\n",
        encoding="utf-8",
    )
    hits = execution_toolset_references(sample)
    # The direct import and the attribute access through a module alias are both caught.
    assert hits == [
        "1: from backend.mcp.toolsets import execution_toolset",
        "3: .execution_toolset",
    ]


# Bypass routes found in review: building handles directly, reaching the raw connector, or using
# the private factory. Only backend/mcp itself (and remediation code) may do any of these.
PRIVILEGED_MCP_NAMES = {"ToolHandle", "_CONSTRUCTION_TOKEN", "_handles", "connector"}


def privileged_mcp_references(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "ToolHandle":
                found.append(f"{node.lineno}: ToolHandle(...)")
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("backend.mcp")
        ):
            for alias in node.names:
                if alias.name in {"_CONSTRUCTION_TOKEN", "_handles"}:
                    found.append(f"{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.Attribute) and node.attr in {"_CONSTRUCTION_TOKEN", "_handles"}:
            found.append(f"{node.lineno}: .{node.attr}")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "connector"
            and (isinstance(node.value, ast.Name) and "manager" in node.value.id.lower())
        ):
            found.append(f"{node.lineno}: manager.connector")
    return found


@pytest.mark.spec("S06-AC9")
def test_no_module_bypasses_the_toolset_functions() -> None:
    mcp_package = REPO / "backend" / "mcp"
    problems = []
    for root in SCANNED:
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(REPO).as_posix()
            if path.is_relative_to(mcp_package) or relative.startswith(EXECUTION_TOOLSET_ALLOWED):
                continue
            problems.extend(f"{relative}:{hit}" for hit in privileged_mcp_references(path))
    assert problems == []


@pytest.mark.spec("S06-AC9")
def test_bypass_checker_flags_direct_handle_use(tmp_path: Path) -> None:
    sample = tmp_path / "sneaky.py"
    sample.write_text(
        "from backend.mcp.invoke import _CONSTRUCTION_TOKEN, ToolHandle\n"
        "h = ToolHandle(t, s, c, None, 0, live, _CONSTRUCTION_TOKEN)\n"
        "await mcp_manager.connector.call_tool(cfg, 'execute_dml', {}, 5)\n"
        "from backend.mcp.toolsets import _handles\n",
        encoding="utf-8",
    )
    hits = privileged_mcp_references(sample)
    assert any("ToolHandle(" in h for h in hits)
    assert any("_CONSTRUCTION_TOKEN" in h for h in hits)
    assert any("manager.connector" in h for h in hits)
    assert any("_handles" in h for h in hits)
