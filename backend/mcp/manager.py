"""MCP manager: probe, dev autostart, discovery and classification (S06; src §5.1 steps 34-39).

Discovery is isolated per server: one server being down never blocks the others. The result is an
immutable ``MCPSnapshot`` swapped atomically, so readers (toolset builders, status, readiness)
never call MCP inline and never see a half-updated view.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

from backend.domain.common import SystemName
from backend.mcp.connector import MCPConnector
from backend.mcp.models import ClassifiedTool, MCPSnapshot, ServerState
from backend.mcp.permissions import TOOL_NAME, ToolPermissionRegistry
from backend.mcp.settings import MCPSettings, ServerConfig

logger = logging.getLogger(__name__)

AUTOSTART_WAIT_S = 15.0
TERMINATE_GRACE_S = 5.0
# A local server that crashed is not respawned on every refresh.
AUTOSTART_BACKOFF_S = 300.0
MAX_TOOLS_PER_SERVER = 500
REPO_ROOT = Path(__file__).resolve().parents[2]
# Child processes get only these variables plus MCP_*: never API tokens, PG_PASSWORD, Airflow or
# LLM credentials. MCP servers read their own settings from MCP_* variables (S07, S24).
_CHILD_ENV_KEYS = frozenset(
    {
        "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
        "TMPDIR", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "LANG", "LC_ALL",
        "PYTHONIOENCODING", "PYTHONUTF8", "VIRTUAL_ENV",
    }
)  # fmt: skip


def child_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment handed to autostarted MCP servers."""
    return {
        key: value
        for key, value in environ.items()
        if key.upper() in _CHILD_ENV_KEYS or key.upper().startswith("MCP_")
    }


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _spawn(module: str, log_path: Path) -> subprocess.Popen[bytes]:
    """``python -m <module>`` in its own process group, from the repository root, with a minimal
    environment; output goes to a log file so crashes can be diagnosed."""
    log = log_path.open("ab")
    try:
        common: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log,
            "stderr": subprocess.STDOUT,
            "cwd": REPO_ROOT,
            "env": child_env(os.environ),
        }
        if sys.platform == "win32":
            common["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            common["start_new_session"] = True
        process: subprocess.Popen[bytes] = subprocess.Popen(  # noqa: S603 - fixed interpreter, allowlisted module, no shell
            [sys.executable, "-m", module],
            **common,  # type: ignore[call-overload]
        )
        return process
    finally:
        log.close()


def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
    """Stop the process and its children (its own group/tree), never anything else."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
        subprocess.run(  # noqa: S603 - fixed system tool, numeric PID
            [str(taskkill), "/T", "/F", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=TERMINATE_GRACE_S,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        if sys.platform != "win32":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        process.kill()
        process.wait(TERMINATE_GRACE_S)


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
        self._autostart_failed_at: dict[SystemName, float] = {}
        self._snapshot = self._empty_snapshot()
        self._task: asyncio.Task[None] | None = None
        # One discovery at a time: overlapping passes could spawn a server twice (orphaning one)
        # or let an older pass overwrite a newer snapshot.
        self._discover_lock = asyncio.Lock()

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
        if self._started_by_us(server):
            return await self.probe(server)
        failed_at = self._autostart_failed_at.get(server)
        if failed_at is not None and time.monotonic() - failed_at < AUTOSTART_BACKOFF_S:
            return False
        log_path = Path(tempfile.gettempdir()) / f"self-healing-mcp-{server.value}.log"
        logger.info(
            "starting local mcp server", extra={"server": server.value, "log": str(log_path)}
        )
        process = await asyncio.to_thread(_spawn, config.module, log_path)
        self._processes[server] = process
        deadline = time.monotonic() + AUTOSTART_WAIT_S
        while time.monotonic() < deadline:
            if process.poll() is not None:
                logger.warning(
                    "local mcp server exited during startup",
                    extra={"server": server.value, "log": str(log_path)},
                )
                break
            if await self.probe(server):
                self._autostart_failed_at.pop(server, None)
                return True
            await asyncio.sleep(0.25)
        else:
            logger.warning(
                "local mcp server did not become healthy", extra={"server": server.value}
            )
        self._autostart_failed_at[server] = time.monotonic()
        await asyncio.to_thread(_terminate_tree, process)
        self._processes.pop(server, None)
        return False

    def _started_by_us(self, server: SystemName) -> bool:
        process = self._processes.get(server)
        return process is not None and process.poll() is None

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
                started_by_us=self._started_by_us(config.name),
                checked_at=now,
                error=error,
                tools=MappingProxyType(tools),
                ignored_tools=ignored,
            )

        ignored = 0
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
        # Server-supplied names are untrusted: invalid or duplicated names are never usable and
        # never echoed; the count is capped.
        counts: dict[str, int] = {}
        for spec in specs:
            counts[spec.name] = counts.get(spec.name, 0) + 1
        valid = [s for s in specs if TOOL_NAME.fullmatch(s.name) and counts[s.name] == 1]
        ignored = len(specs) - len(valid) + max(0, len(valid) - MAX_TOOLS_PER_SERVER)
        if ignored:
            logger.warning(
                "mcp server advertised unusable tool names",
                extra={"server": config.name.value, "ignored": ignored},
            )
        tools = {
            spec.name: self._registry.classify(
                config.name,
                spec,
                default_timeout_s=self._settings.mcp_default_timeout_s,
                default_max_output_bytes=self._settings.mcp_max_output_bytes,
            )
            for spec in valid[:MAX_TOOLS_PER_SERVER]
        }
        return state(True, None, tools)

    async def discover(self) -> MCPSnapshot:
        async with self._discover_lock:
            return await self._discover_locked()

    async def _discover_locked(self) -> MCPSnapshot:
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
                    started_by_us=self._started_by_us(config.name),
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
            try:
                await asyncio.to_thread(_terminate_tree, process)
            except Exception:
                logger.exception("could not stop local mcp server", extra={"server": server.value})
            else:
                logger.info("stopped local mcp server", extra={"server": server.value})
        self._processes.clear()
