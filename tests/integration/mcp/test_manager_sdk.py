"""Real MCP SDK path (S06): the dev echo server, launched through autostart."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from backend.domain.common import SystemName
from backend.mcp.connector import SdkMCPConnector
from backend.mcp.errors import ToolCallError
from backend.mcp.manager import MCPManager
from backend.mcp.models import InvocationContext, ToolClass
from backend.mcp.toolsets import diagnostic_toolset
from tests.fakes.mcp import mcp_settings, registry

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]

PG = SystemName.POSTGRESQL
ENTRIES = [
    {"server": "postgresql", "tool": "echo", "access": "read"},
    {"server": "postgresql", "tool": "get_row_count", "access": "read"},
    {"server": "postgresql", "tool": "set_note", "access": "write"},
    {"server": "postgresql", "tool": "fail", "access": "read"},
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture
def devecho_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[MCPManager, int]:
    port = free_port()
    monkeypatch.setenv("MCP_DEVECHO_PORT", str(port))  # inherited by the autostarted process
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
    manager = MCPManager(
        settings,
        registry(tmp_path, ENTRIES),
        SdkMCPConnector(probe_timeout_s=settings.mcp_probe_timeout_s),
        env="dev",
    )
    return manager, port


@pytest.mark.spec("S06-AC1", "S06-AC7")
async def test_autostart_discover_call_and_shutdown(
    devecho_manager: tuple[MCPManager, int],
) -> None:
    manager, _ = devecho_manager
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
        assert state.tools["echo"].spec.input_schema["required"] == ["text"]

        tools = {h.name: h for h in diagnostic_toolset(manager, None)}
        assert set(tools) == {"postgresql.echo", "postgresql.get_row_count", "postgresql.fail"}
        ctx = InvocationContext(phase="test")
        output = await tools["postgresql.echo"].invoke({"text": "hello"}, ctx)
        assert output.content == "hello"
        with pytest.raises(ToolCallError):
            await tools["postgresql.fail"].invoke({"reason": "boom"}, ctx)
        process = manager._processes[PG]
    finally:
        await manager.shutdown()
    assert process.poll() is not None  # the server we started is gone


@pytest.mark.spec("S06-AC7")
async def test_shutdown_never_touches_servers_it_did_not_start(
    devecho_manager: tuple[MCPManager, int],
) -> None:
    manager, port = devecho_manager
    external = await asyncio.to_thread(
        subprocess.Popen,
        [sys.executable, "-m", "mcp_servers.devecho"],
        env={**os.environ, "MCP_DEVECHO_PORT": str(port)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            if await manager.probe(PG):
                break
            await asyncio.sleep(0.25)
        snapshot = await manager.discover()
        assert snapshot.servers[PG].healthy and not snapshot.servers[PG].started_by_us
        await manager.shutdown()
        assert external.poll() is None  # still running
    finally:
        external.terminate()
        external.wait(10)
