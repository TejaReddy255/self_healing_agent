from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backend.domain.common import SystemName
from backend.mcp.errors import InvalidTarget, NoWriteTools, TargetUnavailable
from backend.mcp.manager import MCPManager
from backend.mcp.models import ToolClass
from backend.mcp.permissions import ToolPermissionRegistry, looks_write_capable
from backend.mcp.toolsets import diagnostic_toolset, execution_toolset
from tests.fakes.mcp import FakeMCPConnector, FakeServer, mcp_settings, registry, tool

PG, MY = SystemName.POSTGRESQL, SystemName.MYSQL


def build(
    tmp_path: Path,
    entries: list[dict[str, Any]],
    servers: dict[SystemName, FakeServer],
    **overrides: Any,
) -> MCPManager:
    reg = registry(tmp_path, entries)
    manager = MCPManager(
        mcp_settings(tmp_path, **overrides), reg, FakeMCPConnector(servers), env="test"
    )
    asyncio.run(manager.discover())
    return manager


def standard(tmp_path: Path, **overrides: Any) -> MCPManager:
    entries = [
        {"server": "postgresql", "tool": "run_select", "access": "read"},
        {"server": "postgresql", "tool": "execute_dml", "access": "write"},
        {"server": "mysql", "tool": "run_select", "access": "read"},
        {"server": "mysql", "tool": "execute_dml", "access": "write"},
        {"server": "mysql", "tool": "drop_table", "access": "read"},  # MISMATCH
    ]
    servers = {
        PG: FakeServer(tools=[tool("run_select"), tool("execute_dml"), tool("mystery")]),
        MY: FakeServer(tools=[tool("run_select"), tool("execute_dml"), tool("drop_table")]),
    }
    return build(tmp_path, entries, servers, **overrides)


# --- AC4: the diagnostic toolset is read-only ----------------------------------------------------


@pytest.mark.spec("S06-AC4")
def test_diagnostic_toolset_contains_only_read_tools(tmp_path: Path) -> None:
    names = [h.name for h in diagnostic_toolset(standard(tmp_path), None)]
    assert names == ["mysql.run_select", "postgresql.run_select"]


NAMES = st.sampled_from(
    ["run_select", "list_tables", "execute_dml", "delete_rows", "describe", "get_count",
     "put_object", "find", "update_many", "x_tool"]
)  # fmt: skip
SERVERS = st.sampled_from(list(SystemName))


@st.composite
def worlds(draw: st.DrawFn) -> tuple[list[dict[str, Any]], dict[SystemName, FakeServer]]:
    servers: dict[SystemName, FakeServer] = {}
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for system in draw(st.sets(SERVERS, min_size=1)):
        names = draw(st.sets(NAMES, max_size=6))
        servers[system] = FakeServer(healthy=draw(st.booleans()), tools=[tool(n) for n in names])
        for name in names:
            choice = draw(st.sampled_from(["read", "write", "absent"]))
            if choice != "absent":
                entries[(system.value, name)] = {
                    "server": system.value, "tool": name, "access": choice,
                }  # fmt: skip
    return list(entries.values()), servers


@pytest.mark.spec("S06-AC4")
@settings(
    max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(world=worlds())
def test_diagnostic_toolset_property(
    tmp_path_factory: pytest.TempPathFactory,
    world: tuple[list[dict[str, Any]], dict[SystemName, FakeServer]],
) -> None:
    entries, servers = world
    manager = build(tmp_path_factory.mktemp("w"), entries, servers)
    handles = diagnostic_toolset(manager, None)
    registered = {(e["server"], e["tool"]): e["access"] for e in entries}
    for handle in handles:
        server, name = handle.name.split(".", 1)
        assert handle.access is ToolClass.READ
        assert registered[(server, name)] == "read"  # never UNCLASSIFIED
        assert not looks_write_capable(name)  # never a write-looking name (MISMATCH excluded)
        state = manager.snapshot().servers[SystemName(server)]
        assert state.healthy and state.enabled


# --- AC5: the execution toolset is one target server ---------------------------------------------


@pytest.mark.spec("S06-AC5")
def test_execution_toolset_is_scoped_to_the_target(tmp_path: Path) -> None:
    handles = execution_toolset(standard(tmp_path), None, "mysql")
    assert [(h.name, h.access) for h in handles] == [
        ("mysql.execute_dml", ToolClass.WRITE),
        ("mysql.run_select", ToolClass.READ),
    ]


@pytest.mark.spec("S06-AC5")
def test_execution_toolset_errors(tmp_path: Path) -> None:
    manager = standard(tmp_path, mcp_s3_enabled=False)
    with pytest.raises(InvalidTarget):
        execution_toolset(manager, None, "oracle")
    with pytest.raises(InvalidTarget):
        execution_toolset(manager, None, "s3")  # disabled
    with pytest.raises(TargetUnavailable):
        execution_toolset(manager, None, "mongodb")  # no fake server: unhealthy

    (tmp_path / "ro").mkdir()
    read_only = build(
        tmp_path / "ro",
        [{"server": "postgresql", "tool": "run_select", "access": "read"}],
        {PG: FakeServer(tools=[tool("run_select")])},
    )
    with pytest.raises(NoWriteTools):
        execution_toolset(read_only, None, "postgresql")


def test_unused_registry_entries_do_not_create_tools(tmp_path: Path) -> None:
    manager = build(
        tmp_path,
        [{"server": "postgresql", "tool": "ghost", "access": "read"}],
        {PG: FakeServer(tools=[])},
    )
    assert diagnostic_toolset(manager, None) == ()
    assert (
        isinstance(manager, MCPManager)
        and len(ToolPermissionRegistry.load(tmp_path / "perms.yaml")) == 1
    )
