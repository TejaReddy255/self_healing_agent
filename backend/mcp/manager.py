"""MCP manager: probe, dev autostart, discovery and classification (S06; src §5.1 steps 34-39).

Discovery is isolated per server: one server being down never blocks the others. The result is an
immutable ``MCPSnapshot`` swapped atomically, so readers (toolset builders, status, readiness)
never call MCP inline and never see a half-updated view.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from types import MappingProxyType

from backend.domain.common import SystemName
from backend.mcp.connector import MCPConnector
from backend.mcp.models import ClassifiedTool, MCPSnapshot, ServerState
from backend.mcp.permissions import ToolPermissionRegistry
from backend.mcp.settings import MCPSettings, ServerConfig

logger = logging.getLogger(__name__)

AUTOSTART_WAIT_S = 15.0
TERMINATE_GRACE_S = 5.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _spawn(module: str) -> subprocess.Popen[bytes]:
    """``python -m <module>`` in its own process group; no shell, no inherited stdin."""
    if sys.platform == "win32":
        return subprocess.Popen(  # noqa: S603 - fixed interpreter, allowlisted module, no shell
            [sys.executable, "-m", module],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(  # noqa: S603 - fixed interpreter, allowlisted module, no shell
        [sys.executable, "-m", module],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class MCPManager:
    def __init__(
        self,
        settings: MCPSettings,
        registry: ToolPermissionRegistry,
        connector: MCPConnector,
        *,
        env: str,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._connector = connector
        self._env = env
        self._clock = clock
        self._servers = settings.servers()
        self._processes: dict[SystemName, subprocess.Popen[bytes]] = {}
        self._snapshot = self._empty_snapshot()
        self._task: asyncio.Task[None] | None = None

    # --- state ---------------------------------------------------------------------------

    def snapshot(self) -> MCPSnapshot:
        return self._snapshot

    def server_config(self, server: SystemName) -> ServerConfig:
        return self._servers[server]

    @property
    def settings(self) -> MCPSettings:
        return self._settings

    @property
    def connector(self) -> MCPConnector:
        return self._connector

    def _empty_snapshot(self) -> MCPSnapshot:
        now = self._clock()
        return MCPSnapshot(
            taken_at=now,
            servers=MappingProxyType(
                {
                    name: ServerState(
                        name=name,
                        url_alias=config.url_alias,
                        enabled=config.enabled,
                        required=config.required,
                        healthy=False,
                        started_by_us=False,
                        checked_at=now,
                        error="not checked yet" if config.enabled else "disabled",
                    )
                    for name, config in self._servers.items()
                }
            ),
        )

    # --- probing and autostart ------------------------------------------------------------

    async def probe(self, server: SystemName) -> bool:
        return (await self._connector.health(self._servers[server])).healthy

    def autostart_allowed(self, config: ServerConfig) -> bool:
        return (
            self._settings.mcp_autostart
            and self._env != "prod"
            and config.enabled
            and config.loopback
        )

    async def start_local(self, server: SystemName) -> bool:
        """Start ``python -m <module>`` for an unreachable local server (dev only)."""
        config = self._servers[server]
        if not self.autostart_allowed(config):
            return False
        if server in self._processes and self._processes[server].poll() is None:
            return await self.probe(server)
        logger.info("starting local mcp server", extra={"server": server.value})
        process = await asyncio.to_thread(_spawn, config.module)
        self._processes[server] = process
        deadline = time.monotonic() + AUTOSTART_WAIT_S
        while time.monotonic() < deadline:
            if process.poll() is not None:
                logger.warning(
                    "local mcp server exited during startup", extra={"server": server.value}
                )
                return False
            if await self.probe(server):
                return True
            await asyncio.sleep(0.25)
        logger.warning("local mcp server did not become healthy", extra={"server": server.value})
        return False

    # --- discovery ------------------------------------------------------------------------

    async def _discover_one(self, config: ServerConfig) -> ServerState:
        now = self._clock()

        def state(
            healthy: bool, error: str | None, tools: dict[str, ClassifiedTool]
        ) -> ServerState:
            return ServerState(
                name=config.name,
                url_alias=config.url_alias,
                enabled=config.enabled,
                required=config.required,
                healthy=healthy,
                started_by_us=config.name in self._processes,
                checked_at=now,
                error=error,
                tools=MappingProxyType(tools),
            )

        if not config.enabled:
            return state(False, "disabled", {})
        health = await self._connector.health(config)
        if not health.healthy and await self.start_local(config.name):
            health = await self._connector.health(config)
        if not health.healthy:
            return state(False, health.error or "unhealthy", {})
        try:
            specs = await self._connector.list_tools(config)
        except Exception as exc:  # one server's failure must not block the others
            logger.warning(
                "mcp tool discovery failed",
                extra={"server": config.name.value, "error": type(exc).__name__},
            )
            return state(False, f"discovery failed: {type(exc).__name__}", {})
        tools = {
            spec.name: self._registry.classify(
                config.name,
                spec,
                default_timeout_s=self._settings.mcp_default_timeout_s,
                default_max_output_bytes=self._settings.mcp_max_output_bytes,
            )
            for spec in specs
        }
        return state(True, None, tools)

    async def discover(self) -> MCPSnapshot:
        configs = list(self._servers.values())
        results = await asyncio.gather(
            *(self._discover_one(c) for c in configs), return_exceptions=True
        )
        servers: dict[SystemName, ServerState] = {}
        for config, result in zip(configs, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "mcp discovery crashed",
                    extra={"server": config.name.value, "error": type(result).__name__},
                )
                servers[config.name] = ServerState(
                    name=config.name,
                    url_alias=config.url_alias,
                    enabled=config.enabled,
                    required=config.required,
                    healthy=False,
                    started_by_us=config.name in self._processes,
                    checked_at=self._clock(),
                    error=f"discovery failed: {type(result).__name__}",
                )
            else:
                servers[config.name] = result
        self._snapshot = MCPSnapshot(taken_at=self._clock(), servers=MappingProxyType(servers))
        return self._snapshot

    # --- background refresh and shutdown ---------------------------------------------------

    async def _refresh_forever(self) -> None:
        while True:
            await asyncio.sleep(self._settings.mcp_refresh_interval_s)
            try:
                await self.discover()
            except Exception:
                logger.exception("mcp refresh failed; will retry next interval")

    def start_refresh(self) -> None:
        if self._settings.mcp_refresh_interval_s > 0 and self._task is None:
            self._task = asyncio.create_task(self._refresh_forever(), name="mcp-refresh")

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for server, process in list(self._processes.items()):
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, TERMINATE_GRACE_S)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)
            logger.info("stopped local mcp server", extra={"server": server.value})
        self._processes.clear()
