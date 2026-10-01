"""Real MCP SDK path (S06): the dev echo server via autostart, and real transport failures."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.domain.common import SystemName
from backend.mcp.connector import SdkMCPConnector
from backend.mcp.errors import ToolCallError, ToolTimeout, ToolUnavailable
from backend.mcp.manager import MCPManager
from backend.mcp.models import InvocationContext, ToolClass, ToolSpec
from backend.mcp.settings import ServerConfig
from backend.mcp.toolsets import diagnostic_toolset
from tests.fakes.mcp import entry, mcp_settings, registry

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

PG = SystemName.POSTGRESQL
ACCESS = {"echo": "read", "get_row_count": "read", "set_note": "write", "fail": "read"}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def config(port: int) -> ServerConfig:
    return ServerConfig(
        name=PG,
        url=f"http://127.0.0.1:{port}/mcp",
        enabled=True,
        required=False,
        module="mcp_servers.devecho",
    )


def start_devecho(port: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "mcp_servers.devecho"],
        env={**os.environ, "MCP_DEVECHO_PORT": str(port)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def wait_healthy(connector: SdkMCPConnector, server: ServerConfig) -> None:
    for _ in range(80):
        if (await connector.health(server)).healthy:
            return
        await asyncio.sleep(0.25)
    raise AssertionError("devecho did not start")


async def live_specs(port: int) -> list[ToolSpec]:
    """Run devecho briefly to read the definitions a reviewer would pin."""
    process = await asyncio.to_thread(start_devecho, port)
    connector = SdkMCPConnector(probe_timeout_s=2)
    try:
        await wait_healthy(connector, config(port))
        return await connector.list_tools(config(port))
    finally:
        process.terminate()
        await asyncio.to_thread(process.wait, 10)


async def devecho_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MCPManager:
    port = free_port()
    specs = await live_specs(port)
    entries = [entry("postgresql", s, ACCESS[s.name]) for s in specs if s.name in ACCESS]
    monkeypatch.setenv("MCP_DEVECHO_PORT", str(port))  # MCP_* is passed to the child
    settings = mcp_settings(
        tmp_path,
        mcp_autostart=True,
        mcp_postgresql_url=f"http://127.0.0.1:{port}/mcp",
        mcp_postgresql_module="mcp_servers.devecho",
        mcp_mysql_enabled=False,
        mcp_s3_enabled=False,
        mcp_mongodb_enabled=False,
        mcp_probe_timeout_s=2,
    )
    return MCPManager(
        settings, registry(tmp_path, entries), SdkMCPConnector(probe_timeout_s=2), env="dev"
    )


@pytest.mark.spec("S06-AC1", "S06-AC7")
async def test_autostart_discover_call_and_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = await devecho_manager(tmp_path, monkeypatch)
    try:
        snapshot = await manager.discover()
        state = snapshot.servers[PG]
        assert state.healthy and state.started_by_us, state.error
        assert {n: t.tool_class for n, t in state.tools.items()} == {
            "echo": ToolClass.READ,
            "get_row_count": ToolClass.READ,
            "set_note": ToolClass.WRITE,
            "fail": ToolClass.READ,
        }
        assert state.tools["echo"].spec.read_only_hint is True
        tools = {h.name: h for h in diagnostic_toolset(manager, None)}
        assert set(tools) == {"postgresql.echo", "postgresql.get_row_count", "postgresql.fail"}
        ctx = InvocationContext(phase="test")
        assert (await tools["postgresql.echo"].invoke({"text": "hello"}, ctx)).content == "hello"
        with pytest.raises(ToolCallError):
            await tools["postgresql.fail"].invoke({"reason": "boom"}, ctx)
        process = manager._processes[PG]
    finally:
        await manager.shutdown()
    assert process.poll() is not None


# --- real transport failures map to the right ToolError --------------------------------------


async def test_closed_port_is_unavailable() -> None:
    connector = SdkMCPConnector(probe_timeout_s=2)
    with pytest.raises(ToolUnavailable):
        await connector.call_tool(config(free_port()), "echo", {"text": "x"}, timeout_s=5)


@pytest.fixture
def silent_server() -> Iterator[int]:
    """Accepts connections and never answers."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port: int = listener.getsockname()[1]
    stop = threading.Event()
    held: list[socket.socket] = []

    def accept() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
                held.append(conn)
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield port
    stop.set()
    thread.join(2)
    for conn in held:
        conn.close()
    listener.close()


async def test_hanging_server_is_a_timeout(silent_server: int) -> None:
    connector = SdkMCPConnector(probe_timeout_s=1)
    with pytest.raises(ToolTimeout):
        await connector.call_tool(config(silent_server), "echo", {"text": "x"}, timeout_s=1)
