from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backend.domain.common import SystemName
from backend.mcp.errors import InvalidTarget, NoWriteTools, TargetUnavailable
from backend.mcp.manager import MCPManager
from backend.mcp.models import ToolClass
from backend.mcp.permissions import looks_write_capable
from backend.mcp.toolsets import diagnostic_toolset, execution_toolset
from tests.fakes.mcp import FakeMCPConnector, FakeServer, entry, mcp_settings, registry, tool

PG, MY = SystemName.POSTGRESQL, SystemName.MYSQL


class NullAudit:
    async def record(self, event: object, **_: Any) -> int:
        return 1


def build(
    tmp_path: Path,
    entries: list[dict[str, Any]],
    servers: dict[SystemName, FakeServer],
    **overrides: Any,
) -> MCPManager:
    manager = MCPManager(
        mcp_settings(tmp_path, **overrides),
        registry(tmp_path, entries),
        FakeMCPConnector(servers),
        env="test",
    )
    asyncio.run(manager.discover())
    return manager


def standard(tmp_path: Path, **overrides: Any) -> MCPManager:
    select, dml, drop = tool("run_select"), tool("execute_dml"), tool("drop_table")
    entries = [
        entry("postgresql", select, "read"),
        entry("postgresql", dml, "write"),
        entry("mysql", select, "read"),
        entry("mysql", dml, "write"),
        entry("mysql", drop, "read"),  # MISMATCH: write-looking name
    ]
    servers = {
        PG: FakeServer(tools=[select, dml, tool("mystery")]),
        MY: FakeServer(tools=[select, dml, drop]),
    }
    return build(tmp_path, entries, servers, **overrides)


# --- AC4: the diagnostic toolset is read-only ----------------------------------------------------


@pytest.mark.spec("S06-AC4")
def test_diagnostic_toolset_contains_only_read_tools(tmp_path: Path) -> None:
    names = [h.name for h in diagnostic_toolset(standard(tmp_path), None)]
    assert names == ["mysql.run_select", "postgresql.run_select"]


NAMES = st.sampled_from(
    ["run_select", "list_tables", "execute_dml", "delete_rows", "describe", "get_count",
     "put_object", "find", "update_many", "x_tool", "describe_dataset", "bulkwrite"]
)  # fmt: skip
HINTS = st.sampled_from(
    [{"read_only_hint": True}, {}, {"read_only_hint": False}, {"destructive_hint": True},
     {"read_only_hint": True, "destructive_hint": True}]
)  # fmt: skip
World = tuple[list[dict[str, Any]], dict[SystemName, FakeServer], dict[str, bool], set[str]]


@st.composite
def worlds(draw: st.DrawFn) -> World:
    """Random servers (enabled/healthy), tools (hints), registry entries (access, pin drift)."""
    servers: dict[SystemName, FakeServer] = {}
    entries: list[dict[str, Any]] = []
    enabled: dict[str, bool] = {}
    expected_read: set[str] = set()
    for system in draw(st.sets(st.sampled_from(list(SystemName)), min_size=1)):
        enabled[system.value] = draw(st.booleans())
        healthy = draw(st.booleans())
        specs = []
        for name in draw(st.sets(NAMES, max_size=6)):
            spec = tool(name, hints=draw(HINTS))
            choice = draw(st.sampled_from(["read", "write", "absent"]))
            drifted = draw(st.booleans())
            if choice != "absent":
                entries.append(entry(system.value, spec, choice))
            live = replace(spec, description="changed") if drifted else spec
            specs.append(live)
            # Independent expectation, written from the rules rather than from the code:
            if (
                choice == "read"
                and not drifted
                and healthy
                and enabled[system.value]
                and not looks_write_capable(name)
                and live.read_only_hint is True
                and live.destructive_hint is not True
            ):
                expected_read.add(f"{system.value}.{name}")
        servers[system] = FakeServer(healthy=healthy, tools=specs)
    return entries, servers, enabled, expected_read


@pytest.mark.spec("S06-AC4")
@settings(
    max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(world=worlds())
def test_diagnostic_toolset_property(
    tmp_path_factory: pytest.TempPathFactory, world: World
) -> None:
    entries, servers, enabled, expected = world
    overrides = {f"mcp_{s.value}_enabled": enabled.get(s.value, True) for s in SystemName}
    manager = build(tmp_path_factory.mktemp("w"), entries, servers, **overrides)
    handles = diagnostic_toolset(manager, None)
    assert {h.name for h in handles} == expected
    assert all(h.access is ToolClass.READ for h in handles)


# --- AC5: the execution toolset is one target server ---------------------------------------------


@pytest.mark.spec("S06-AC5")
def test_execution_toolset_is_scoped_to_the_target(tmp_path: Path) -> None:
    handles = execution_toolset(standard(tmp_path), NullAudit(), "mysql")
    assert [(h.name, h.access) for h in handles] == [
        ("mysql.execute_dml", ToolClass.WRITE),
        ("mysql.run_select", ToolClass.READ),
    ]


@pytest.mark.spec("S06-AC5")
def test_execution_toolset_errors(tmp_path: Path) -> None:
    manager = standard(tmp_path, mcp_s3_enabled=False)
    audit = NullAudit()
    with pytest.raises(InvalidTarget):
        execution_toolset(manager, audit, "oracle")
    with pytest.raises(InvalidTarget):
        execution_toolset(manager, audit, "s3")  # disabled
    with pytest.raises(TargetUnavailable):
        execution_toolset(manager, audit, "mongodb")  # no fake server: unhealthy
    with pytest.raises(InvalidTarget, match="audit"):
        execution_toolset(manager, None, "mysql")  # type: ignore[arg-type]

    (tmp_path / "ro").mkdir()
    select = tool("run_select")
    read_only = build(
        tmp_path / "ro", [entry("postgresql", select, "read")], {PG: FakeServer(tools=[select])}
    )
    with pytest.raises(NoWriteTools):
        execution_toolset(read_only, audit, "postgresql")


def test_handles_cannot_be_built_outside_the_toolsets(tmp_path: Path) -> None:
    from backend.mcp.invoke import ToolHandle

    manager = standard(tmp_path)
    write = manager.snapshot().servers[PG].tools["execute_dml"]
    with pytest.raises(TypeError, match="created only by"):
        ToolHandle(
            write, manager.server_config(PG), manager.connector, None, 0, lambda: write, object()
        )
