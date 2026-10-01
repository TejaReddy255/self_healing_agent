"""MCP connection seam (S06, ADR-012): the official ``mcp`` SDK behind a small Protocol.

Each call opens a short-lived streamable-HTTP session. That costs one extra round trip but keeps
the manager free of long-lived connection state that could go stale between refreshes, and a
timeout on one call can never poison the next.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from backend.mcp.errors import ToolCallError, ToolTimeout, ToolUnavailable
from backend.mcp.models import ServerHealth, ToolCallResult, ToolSpec
from backend.mcp.settings import ServerConfig
from backend.observability.redaction import redact_text

ERROR_DETAIL_CHARS = 1000


class MCPConnector(Protocol):
    async def health(self, server: ServerConfig) -> ServerHealth: ...

    async def list_tools(self, server: ServerConfig) -> list[ToolSpec]: ...

    async def call_tool(
        self, server: ServerConfig, name: str, arguments: Mapping[str, Any], timeout_s: float
    ) -> ToolCallResult: ...


def _text_of(content: list[Any]) -> str:
    parts: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        parts.append(text if isinstance(text, str) else f"[{getattr(item, 'type', 'content')}]")
    return "\n".join(parts)


class SdkMCPConnector:
    def __init__(self, *, probe_timeout_s: float) -> None:
        self._probe_timeout_s = probe_timeout_s

    def _http(self, timeout_s: float) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(timeout=timeout_s, follow_redirects=False)

    async def health(self, server: ServerConfig) -> ServerHealth:
        try:
            async with self._http(self._probe_timeout_s) as http:
                response = await http.get(server.health_url)
        except httpx2.TimeoutException:
            return ServerHealth(False, "timeout")
        except httpx2.HTTPError:
            return ServerHealth(False, "unreachable")
        if response.status_code != 200:
            return ServerHealth(False, f"http_{response.status_code}")
        return ServerHealth(True)

    async def list_tools(self, server: ServerConfig) -> list[ToolSpec]:
        async with (
            self._http(self._probe_timeout_s * 3) as http,
            streamable_http_client(server.url, http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await asyncio.wait_for(session.initialize(), self._probe_timeout_s * 3)
            listed = await asyncio.wait_for(session.list_tools(), self._probe_timeout_s * 3)
        specs: list[ToolSpec] = []
        for tool in listed.tools:
            annotations = getattr(tool, "annotations", None)
            specs.append(
                ToolSpec(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=dict(tool.input_schema or {}),
                    destructive_hint=getattr(annotations, "destructive_hint", None),
                )
            )
        return specs

    async def call_tool(
        self, server: ServerConfig, name: str, arguments: Mapping[str, Any], timeout_s: float
    ) -> ToolCallResult:
        try:
            async with asyncio.timeout(timeout_s):
                async with (
                    self._http(timeout_s) as http,
                    streamable_http_client(server.url, http_client=http) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    result = await session.call_tool(
                        name, dict(arguments), read_timeout_seconds=timeout_s
                    )
        except TimeoutError as exc:
            raise ToolTimeout(f"{server.name.value}.{name} timed out") from exc
        except httpx2.HTTPError as exc:
            raise ToolUnavailable(f"{server.name.value} is unreachable") from exc
        except OSError as exc:
            raise ToolUnavailable(f"{server.name.value} is unreachable") from exc
        content = list(getattr(result, "content", []) or [])
        is_error = bool(getattr(result, "is_error", False))
        if is_error:
            detail = redact_text(_text_of(content))[:ERROR_DETAIL_CHARS]
            raise ToolCallError(f"{server.name.value}.{name} reported an error", detail=detail)
        return ToolCallResult(content_text=_text_of(content), is_error=False)
