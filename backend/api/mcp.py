"""MCP status API (S06, FR-024)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from backend.mcp.manager import MCPManager
from backend.mcp.models import ToolClass
from backend.security.auth import Actor, Role, require_role

router = APIRouter(tags=["mcp"])


class ToolCounts(BaseModel):
    read: int
    write: int
    unclassified: int
    mismatch: int
    schema_changed: int


class ServerStatus(BaseModel):
    name: str
    url_alias: str
    enabled: bool
    required: bool
    healthy: bool
    started_by_us: bool
    checked_at: datetime
    error: str | None
    counts: ToolCounts
    unclassified: list[str]
    mismatch: list[str]
    schema_changed: list[str]


class MCPStatus(BaseModel):
    taken_at: datetime
    servers: list[ServerStatus]
    diagnostic_write_tools_blocked: int


@router.get("/mcp-status", response_model=MCPStatus)
async def mcp_status(
    request: Request, _: Annotated[Actor, Depends(require_role(Role.VIEWER))]
) -> MCPStatus:
    manager: MCPManager = request.app.state.mcp_manager
    snapshot = manager.snapshot()
    servers: list[ServerStatus] = []
    blocked = 0
    for name, state in sorted(snapshot.servers.items()):

        def names(cls: ToolClass, state: object = state) -> list[str]:
            return [t.spec.name for t in state.tools_of(cls)]  # type: ignore[attr-defined]

        excluded = len(state.tools) - len(state.tools_of(ToolClass.READ))
        blocked += excluded
        servers.append(
            ServerStatus(
                name=name.value,
                url_alias=state.url_alias,
                enabled=state.enabled,
                required=state.required,
                healthy=state.healthy,
                started_by_us=state.started_by_us,
                checked_at=state.checked_at,
                error=state.error,
                counts=ToolCounts(
                    read=len(state.tools_of(ToolClass.READ)),
                    write=len(state.tools_of(ToolClass.WRITE)),
                    unclassified=len(state.tools_of(ToolClass.UNCLASSIFIED)),
                    mismatch=len(state.tools_of(ToolClass.MISMATCH)),
                    schema_changed=len(state.tools_of(ToolClass.SCHEMA_CHANGED)),
                ),
                unclassified=names(ToolClass.UNCLASSIFIED),
                mismatch=names(ToolClass.MISMATCH),
                schema_changed=names(ToolClass.SCHEMA_CHANGED),
            )
        )
    return MCPStatus(
        taken_at=snapshot.taken_at, servers=servers, diagnostic_write_tools_blocked=blocked
    )
