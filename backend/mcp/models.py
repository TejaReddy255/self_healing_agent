"""MCP discovery and invocation types (S06)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from backend.domain.common import SystemName


class ToolClass(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
    UNCLASSIFIED = "UNCLASSIFIED"  # not in the registry: unusable (default deny)
    MISMATCH = "MISMATCH"  # registered as read but failed a write tripwire: excluded
    SCHEMA_CHANGED = "SCHEMA_CHANGED"  # live definition differs from the pinned hash: excluded


@dataclass(frozen=True)
class ToolSpec:
    """A tool as advertised by an MCP server.

    ``annotations`` holds the MCP hints (``read_only_hint``, ``destructive_hint``,
    ``idempotent_hint``, ``open_world_hint``) when the server sends them.
    """

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def read_only_hint(self) -> bool | None:
        value = self.annotations.get("read_only_hint")
        return value if isinstance(value, bool) else None

    @property
    def destructive_hint(self) -> bool | None:
        value = self.annotations.get("destructive_hint")
        return value if isinstance(value, bool) else None


@dataclass(frozen=True)
class ClassifiedTool:
    server: SystemName
    spec: ToolSpec
    tool_class: ToolClass
    timeout_s: float
    max_output_bytes: int
    reason: str | None = None
    definition_sha256: str = ""

    @property
    def qualified_name(self) -> str:
        return f"{self.server.value}.{self.spec.name}"

    @property
    def usable(self) -> bool:
        return self.tool_class in (ToolClass.READ, ToolClass.WRITE)


@dataclass(frozen=True)
class ServerHealth:
    healthy: bool
    error: str | None = None


@dataclass(frozen=True)
class ServerState:
    name: SystemName
    url_alias: str
    enabled: bool
    required: bool
    healthy: bool
    started_by_us: bool
    checked_at: datetime
    error: str | None = None
    tools: MappingProxyType[str, ClassifiedTool] = field(
        default_factory=lambda: MappingProxyType({})
    )
    # Tools the server advertised that could not even be classified (invalid or duplicate names,
    # or beyond the per-server cap). They are never usable.
    ignored_tools: int = 0

    def tools_of(self, *classes: ToolClass) -> tuple[ClassifiedTool, ...]:
        return tuple(t for _, t in sorted(self.tools.items()) if t.tool_class in classes)


@dataclass(frozen=True)
class MCPSnapshot:
    """Immutable result of one discovery pass; replaced atomically on refresh."""

    taken_at: datetime
    servers: MappingProxyType[SystemName, ServerState]


@dataclass(frozen=True)
class InvocationContext:
    """Correlation for audit rows (invariant 12)."""

    phase: str
    actor_subject: str | None = None
    actor_role: str | None = None
    request_id: str | None = None
    analysis_id: UUID | None = None
    review_id: UUID | None = None
    job_id: UUID | None = None
    incident_id: UUID | None = None
    pipeline_id: str | None = None
    dag_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class ToolCallResult:
    content_text: str
    is_error: bool


@dataclass(frozen=True)
class ToolOutput:
    tool: str
    content: str
    truncated: bool
    original_bytes: int
    duration_ms: int
