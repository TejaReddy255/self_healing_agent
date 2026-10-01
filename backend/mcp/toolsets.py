"""Deterministic toolset composition (S06, FR-022, NFR-004).

Toolsets depend only on configuration, the permission registry and the discovery snapshot, never
on model output. ``execution_toolset`` may only be imported by remediation code (boundary test).
"""

from __future__ import annotations

from functools import partial

from backend.domain.common import SystemName
from backend.mcp.errors import InvalidTarget, NoWriteTools, TargetUnavailable
from backend.mcp.invoke import _CONSTRUCTION_TOKEN, AuditSink, ToolHandle
from backend.mcp.manager import MCPManager
from backend.mcp.models import ClassifiedTool, ServerState, ToolClass


def _handles(
    manager: MCPManager,
    audit: AuditSink | None,
    state: ServerState,
    *classes: ToolClass,
) -> list[ToolHandle]:
    def live(server: SystemName, name: str) -> ClassifiedTool | None:
        current = manager.snapshot().servers.get(server)
        if current is None or not current.healthy or not current.enabled:
            return None
        return current.tools.get(name)

    return [
        ToolHandle(
            tool=tool,
            server=manager.server_config(state.name),
            connector=manager.connector,
            audit=audit,
            read_retries=manager.settings.mcp_read_retries,
            live=partial(live, state.name, tool.spec.name),
            _token=_CONSTRUCTION_TOKEN,
        )
        for tool in state.tools_of(*classes)
    ]


def diagnostic_toolset(manager: MCPManager, audit: AuditSink | None) -> tuple[ToolHandle, ...]:
    """READ tools on healthy, enabled servers only.

    Never a WRITE, UNCLASSIFIED, MISMATCH or SCHEMA_CHANGED tool (NFR-004).
    """
    handles: list[ToolHandle] = []
    for _, state in sorted(manager.snapshot().servers.items()):
        if state.enabled and state.healthy:
            handles.extend(_handles(manager, audit, state, ToolClass.READ))
    return tuple(handles)


def execution_toolset(
    manager: MCPManager, audit: AuditSink, target: SystemName | str
) -> tuple[ToolHandle, ...]:
    """READ + WRITE tools of the approved target server only (src §8.1).

    ``audit`` is required: writes are refused without a recorded start row (invariant 12).
    """
    if audit is None:
        raise InvalidTarget("an audit sink is required for the execution toolset")
    try:
        system = SystemName(target)
    except ValueError:
        raise InvalidTarget("the target is not a known system") from None
    state = manager.snapshot().servers.get(system)
    if state is None or not state.enabled:
        raise InvalidTarget(f"{system.value} is not an enabled MCP server")
    if not state.healthy:
        raise TargetUnavailable(f"{system.value} is not healthy")
    handles = _handles(manager, audit, state, ToolClass.READ, ToolClass.WRITE)
    if not any(h.access is ToolClass.WRITE for h in handles):
        raise NoWriteTools(f"{system.value} exposes no registered write tools")
    return tuple(handles)
