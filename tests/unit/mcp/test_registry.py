from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.config import ConfigurationError
from backend.domain.common import SystemName
from backend.mcp.models import ToolClass, ToolSpec
from backend.mcp.permissions import ToolPermissionRegistry, looks_write_capable, schema_sha256
from tests.fakes.mcp import registry, tool

PG = SystemName.POSTGRESQL


def classify(reg: ToolPermissionRegistry, spec: ToolSpec, server: SystemName = PG) -> ToolClass:
    return reg.classify(
        server, spec, default_timeout_s=60, default_max_output_bytes=8192
    ).tool_class


@pytest.mark.spec("S06-AC1")
def test_registry_classifies_read_and_write(tmp_path: Path) -> None:
    reg = registry(
        tmp_path,
        [
            {"server": "postgresql", "tool": "run_select", "access": "read", "timeout_s": 30},
            {"server": "postgresql", "tool": "execute_dml", "access": "write"},
        ],
    )
    read = reg.classify(
        PG, tool("run_select", sql="string"), default_timeout_s=60, default_max_output_bytes=8192
    )
    assert (read.tool_class, read.timeout_s, read.qualified_name) == (
        ToolClass.READ,
        30,
        "postgresql.run_select",
    )
    assert classify(reg, tool("execute_dml", sql="string")) is ToolClass.WRITE


@pytest.mark.spec("S06-AC1")
def test_classification_is_per_server(tmp_path: Path) -> None:
    reg = registry(tmp_path, [{"server": "postgresql", "tool": "run_select", "access": "read"}])
    assert classify(reg, tool("run_select"), PG) is ToolClass.READ
    assert classify(reg, tool("run_select"), SystemName.MYSQL) is ToolClass.UNCLASSIFIED


@pytest.mark.spec("S06-AC2")
def test_unknown_tools_are_unclassified(tmp_path: Path) -> None:
    reg = registry(tmp_path, [])
    assert classify(reg, tool("list_tables")) is ToolClass.UNCLASSIFIED


@pytest.mark.spec("S06-AC3")
@pytest.mark.parametrize(
    "name", ["execute_dml", "delete_many", "putObject", "insertRows", "set_flag"]
)
def test_read_entries_that_look_write_capable_are_mismatch(
    tmp_path: Path, name: str, caplog: pytest.LogCaptureFixture
) -> None:
    reg = registry(tmp_path, [{"server": "postgresql", "tool": name, "access": "read"}])
    assert classify(reg, tool(name)) is ToolClass.MISMATCH
    assert "looks write-capable" in caplog.text


@pytest.mark.spec("S06-AC3")
def test_destructive_hint_also_trips_the_mismatch(tmp_path: Path) -> None:
    reg = registry(tmp_path, [{"server": "postgresql", "tool": "describe", "access": "read"}])
    hinted = ToolSpec(name="describe", destructive_hint=True)
    assert classify(reg, hinted) is ToolClass.MISMATCH


@pytest.mark.spec("S06-AC3")
def test_heuristic_never_flags_ordinary_read_names() -> None:
    for name in (
        "describe_dataset",
        "get_settings",
        "list_tables",
        "run_select",
        "find_duplicates",
    ):
        assert not looks_write_capable(name), name


@pytest.mark.spec("S06-AC3")
def test_pinned_schema_mismatch_is_schema_changed(tmp_path: Path) -> None:
    vetted = tool("run_select", sql="string")
    reg = registry(
        tmp_path,
        [
            {
                "server": "postgresql",
                "tool": "run_select",
                "access": "read",
                "input_schema_sha256": schema_sha256(vetted.input_schema),
            }
        ],
    )
    assert classify(reg, vetted) is ToolClass.READ
    widened = tool("run_select", sql="string", allow_writes="boolean")
    assert classify(reg, widened) is ToolClass.SCHEMA_CHANGED


# --- AC10: a broken registry stops startup -------------------------------------------------------


@pytest.mark.spec("S06-AC10")
@pytest.mark.parametrize(
    ("content", "match"),
    [
        (None, "cannot read"),
        ("version: 1\ntools: [unclosed\n", "invalid YAML"),
        ("version: 2\ntools: []\n", "version"),
        ("version: 1\ntools: []\nextra: 1\n", "extra"),
        (
            "version: 1\ntools:\n- {server: postgresql, tool: a, access: read}\n"
            "- {server: postgresql, tool: a, access: write}\n",
            "duplicate registry entry",
        ),
        ("version: 1\ntools:\n- {server: oracle, tool: a, access: read}\n", "server"),
        ("version: 1\ntools:\n- {server: postgresql, tool: a, access: admin}\n", "access"),
        ("!!python/object/apply:os.system ['echo pwned']\n", "unsupported YAML"),
        ("version: 1\nversion: 1\ntools: []\n", "duplicate key"),
    ],
    ids=[
        "missing", "yaml-error", "wrong-version", "unknown-key", "duplicate-entry",
        "unknown-server", "unknown-access", "unsafe-tag", "duplicate-key",
    ],
)  # fmt: skip
def test_invalid_registry_is_a_configuration_error(
    tmp_path: Path, content: str | None, match: str
) -> None:
    path = tmp_path / "perms.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=match):
        ToolPermissionRegistry.load(path)


def test_committed_registry_is_valid() -> None:
    from backend.mcp.settings import DEFAULT_PERMISSIONS_FILE

    ToolPermissionRegistry.load(DEFAULT_PERMISSIONS_FILE)


def test_registry_overrides_are_capped_by_global_limits(tmp_path: Path) -> None:
    entries: list[dict[str, Any]] = [
        {"server": "postgresql", "tool": "big", "access": "read", "max_output_bytes": 999_999}
    ]
    reg = registry(tmp_path, entries)
    classified = reg.classify(PG, tool("big"), default_timeout_s=60, default_max_output_bytes=8192)
    assert classified.max_output_bytes == 8192
