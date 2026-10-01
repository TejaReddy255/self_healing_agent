"""Deterministic toolset composition (S06, FR-022, NFR-004).

Toolsets depend only on configuration, the permission registry and the discovery snapshot, never
on model output. ``execution_toolset`` may only be imported by remediation code (boundary test).
"""

from __future__ import annotations

from backend.domain.common import SystemName
from backend.mcp.errors import InvalidTarget, NoWriteTools, TargetUnavailable
from backend.mcp.invoke import AuditSink, ToolHandle
from backend.mcp.manager import MCPManager
from backend.mcp.models import ToolClass


def _handles(
    manager: MCPManager, audit: AuditSink | None, server: SystemName, *classes: ToolClass
) -> list[ToolHandle]:
    state = manager.snapshot().servers[server]
    return [
        ToolHandle(
            tool=tool,
            server=manager.server_config(server),
            connector=manager.connector,
            audit=audit,
            read_retries=manager.settings.mcp_read_retries,
        )
        for tool in state.tools_of(*classes)
    ]


def diagnostic_toolset(manager: MCPManager, audit: AuditSink | None) -> tuple[ToolHandle, ...]:
    """READ tools on healthy, enabled servers only.

    Never a WRITE, UNCLASSIFIED, MISMATCH or SCHEMA_CHANGED tool (NFR-004).
    """
    handles: list[ToolHandle] = []
    for name, state in sorted(manager.snapshot().servers.items()):
        if state.enabled and state.healthy:
            handles.extend(_handles(manager, audit, name, ToolClass.READ))
    return tuple(handles)


def execution_toolset(
    manager: MCPManager, audit: AuditSink | None, target: SystemName | str
) -> tuple[ToolHandle, ...]:
    """READ + WRITE tools of the approved target server only (src §8.1)."""
    try:
        system = SystemName(target)
    except ValueError:
        raise InvalidTarget("the target is not a known system") from None
    state = manager.snapshot().servers.get(system)
    if state is None or not state.enabled:
        raise InvalidTarget(f"{system.value} is not an enabled MCP server")
    if not state.healthy:
        raise TargetUnavailable(f"{system.value} is not healthy")
    handles = _handles(manager, audit, system, ToolClass.READ, ToolClass.WRITE)
    if not any(h.access is ToolClass.WRITE for h in handles):
        raise NoWriteTools(f"{system.value} exposes no registered write tools")
    return tuple(handles)
