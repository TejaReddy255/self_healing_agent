"""In-memory MCP connector for tests (S06, ADR-012)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from backend.domain.common import SystemName
from backend.mcp.errors import ToolCallError, ToolTimeout, ToolUnavailable
from backend.mcp.models import ServerHealth, ToolCallResult, ToolSpec
from backend.mcp.permissions import ToolPermissionRegistry, schema_sha256
from backend.mcp.settings import MCPSettings, ServerConfig

Result = str | Exception | Callable[[Mapping[str, Any]], str]


@dataclass
class FakeServer:
    healthy: bool = True
    tools: list[ToolSpec] = field(default_factory=list)
    results: dict[str, Result] = field(default_factory=dict)
    delay_s: float = 0.0
    list_error: Exception | None = None
    unavailable_times: int = 0  # the next N calls fail with ToolUnavailable


@dataclass
class Call:
    server: SystemName
    tool: str
    arguments: dict[str, Any]
    timeout_s: float


class FakeMCPConnector:
    def __init__(self, servers: dict[SystemName, FakeServer] | None = None) -> None:
        self.servers = servers or {}
        self.calls: list[Call] = []
        self.health_checks = 0

    def server(self, name: SystemName) -> FakeServer:
        return self.servers.setdefault(name, FakeServer())

    async def health(self, server: ServerConfig) -> ServerHealth:
        self.health_checks += 1
        fake = self.servers.get(server.name)
        if fake is None or not fake.healthy:
            return ServerHealth(False, "unreachable")
        return ServerHealth(True)

    async def list_tools(self, server: ServerConfig) -> list[ToolSpec]:
        fake = self.servers[server.name]
        if fake.list_error is not None:
            raise fake.list_error
        return list(fake.tools)

    async def call_tool(
        self, server: ServerConfig, name: str, arguments: Mapping[str, Any], timeout_s: float
    ) -> ToolCallResult:
        self.calls.append(Call(server.name, name, dict(arguments), timeout_s))
        fake = self.servers[server.name]
        if fake.unavailable_times > 0:
            fake.unavailable_times -= 1
            raise ToolUnavailable(f"{server.name.value} is unreachable")
        if fake.delay_s:
            try:
                async with asyncio.timeout(timeout_s):
                    await asyncio.sleep(fake.delay_s)
            except TimeoutError as exc:
                raise ToolTimeout(f"{server.name.value}.{name} timed out") from exc
        outcome = fake.results.get(name, f"{name} ok")
        if isinstance(outcome, ToolCallError):
            raise outcome
        if isinstance(outcome, Exception):
            raise outcome
        text = outcome(arguments) if callable(outcome) else outcome
        return ToolCallResult(content_text=text, is_error=False)


def tool(name: str, **schema_props: str) -> ToolSpec:
    schema = {
        "type": "object",
        "properties": {k: {"type": v} for k, v in schema_props.items()},
        "required": sorted(schema_props),
    }
    return ToolSpec(name=name, description=f"{name} tool", input_schema=schema)


def write_registry(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.write_text(yaml.safe_dump({"version": 1, "tools": entries}), encoding="utf-8")
    return path


def registry(tmp_path: Path, entries: list[dict[str, Any]]) -> ToolPermissionRegistry:
    return ToolPermissionRegistry.load(write_registry(tmp_path / "perms.yaml", entries))


def mcp_settings(tmp_path: Path, **overrides: Any) -> MCPSettings:
    values: dict[str, Any] = {
        "tool_permissions_file": tmp_path / "perms.yaml",
        "mcp_refresh_interval_s": 0,
        **{f"mcp_{s.value}_enabled": True for s in SystemName},
    }
    values.update(overrides)
    return MCPSettings(_env_file=None, **values)


__all__ = ["FakeMCPConnector", "FakeServer", "mcp_settings", "registry", "schema_sha256", "tool"]
