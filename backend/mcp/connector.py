"""MCP connection seam (S06, ADR-012): the official ``mcp`` SDK (2.x) behind a small Protocol.

Each call opens a short-lived streamable-HTTP session. That costs extra round trips but keeps the
manager free of long-lived connection state that could go stale between refreshes, and a timeout
on one call can never poison the next.

Failures from the SDK arrive in several shapes (anyio exception groups wrapping httpx2 errors,
``MCPError``, validation errors). ``map_failure`` turns every one of them into a ``ToolError``
that says whether the request may have run, so callers never retry a write that might have
happened (NFR-011).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from typing import Any, Protocol

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import PaginatedRequestParams

from backend.mcp.errors import (
    ToolCallError,
    ToolError,
    ToolOutcomeUnknown,
    ToolTimeout,
    ToolUnavailable,
)
from backend.mcp.models import ServerHealth, ToolCallResult, ToolSpec
from backend.mcp.settings import ServerConfig
from backend.observability.redaction import redact_text

ERROR_DETAIL_CHARS = 1000
MAX_TOOL_PAGES = 10


class MCPConnector(Protocol):
    async def health(self, server: ServerConfig) -> ServerHealth: ...

    async def list_tools(self, server: ServerConfig) -> list[ToolSpec]: ...

    async def call_tool(
        self, server: ServerConfig, name: str, arguments: Mapping[str, Any], timeout_s: float
    ) -> ToolCallResult: ...


def _leaves(exc: BaseException) -> Iterator[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _leaves(inner)
    else:
        yield exc


def map_failure(exc: BaseException, label: str) -> ToolError:
    """Classify any SDK/transport failure. Messages are fixed: no server text, no arguments."""
    leaves = list(_leaves(exc))
    if any(isinstance(e, (TimeoutError, httpx2.TimeoutException)) for e in leaves):
        return ToolTimeout(f"{label} timed out")
    if leaves and all(isinstance(e, (httpx2.ConnectError, ConnectionRefusedError)) for e in leaves):
        return ToolUnavailable(f"{label}: server unreachable")
    for leaf in leaves:
        if isinstance(leaf, MCPError):
            detail = redact_text(str((getattr(leaf, "error", None) and leaf.error.message) or ""))
            return ToolCallError(
                f"{label} reported a protocol error", detail=detail[:ERROR_DETAIL_CHARS]
            )
    # Anything else happened after the request may have been sent: the outcome is unknown.
    return ToolOutcomeUnknown(
        f"{label}: no result ({type(leaves[0]).__name__ if leaves else 'unknown'})"
    )


def _text_of(content: list[Any]) -> str:
    parts: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        parts.append(text if isinstance(text, str) else f"[{getattr(item, 'type', 'content')}]")
    return "\n".join(parts)


def _annotations(tool: Any) -> dict[str, Any]:
    raw = getattr(tool, "annotations", None)
    if raw is None:
        return {}
    keys = ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint")
    return {k: getattr(raw, k) for k in keys if getattr(raw, k, None) is not None}


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
        budget = self._probe_timeout_s * 3
        specs: list[ToolSpec] = []
        try:
            async with asyncio.timeout(budget * MAX_TOOL_PAGES):
                async with (
                    self._http(budget) as http,
                    streamable_http_client(server.url, http_client=http) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    cursor: str | None = None
                    for _ in range(MAX_TOOL_PAGES):
                        params = PaginatedRequestParams(cursor=cursor) if cursor else None
                        listed = await session.list_tools(params=params)
                        for tool in listed.tools:
                            specs.append(
                                ToolSpec(
                                    name=tool.name,
                                    description=tool.description or "",
                                    input_schema=dict(tool.input_schema or {}),
                                    annotations=_annotations(tool),
                                )
                            )
                        cursor = listed.next_cursor
                        if not cursor:
                            break
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise map_failure(exc, f"{server.name.value} tool listing") from None
        return specs

    async def call_tool(
        self, server: ServerConfig, name: str, arguments: Mapping[str, Any], timeout_s: float
    ) -> ToolCallResult:
        label = f"{server.name.value}.{name}"
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
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise map_failure(exc, label) from None
        content = list(getattr(result, "content", []) or [])
        if bool(getattr(result, "is_error", False)):
            detail = redact_text(_text_of(content))[:ERROR_DETAIL_CHARS]
            raise ToolCallError(f"{label} reported an error", detail=detail)
        return ToolCallResult(content_text=_text_of(content), is_error=False)
