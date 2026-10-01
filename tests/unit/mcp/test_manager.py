from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.config import ConfigurationError
from backend.domain.common import SystemName
from backend.mcp.manager import MCPManager, child_env
from backend.mcp.models import ToolClass, ToolSpec
from backend.mcp.settings import MCPSettings
from tests.fakes.mcp import FakeMCPConnector, FakeServer, entry, mcp_settings, registry, tool

PG, MY = SystemName.POSTGRESQL, SystemName.MYSQL
SELECT = tool("run_select")
ENTRIES = [entry("postgresql", SELECT, "read"), entry("mysql", SELECT, "read")]


def manager(
    tmp_path: Path, servers: dict[SystemName, FakeServer], **overrides: object
) -> MCPManager:
    return MCPManager(
        mcp_settings(tmp_path, **overrides),
        registry(tmp_path, ENTRIES),
        FakeMCPConnector(servers),
        env="test",
    )


@pytest.mark.spec("S06-AC6")
async def test_one_server_down_does_not_block_the_others(tmp_path: Path) -> None:
    servers = {PG: FakeServer(healthy=False), MY: FakeServer(tools=[SELECT, tool("extra")])}
    snapshot = await manager(tmp_path, servers).discover()
    assert snapshot.servers[PG].healthy is False and snapshot.servers[PG].error == "unreachable"
    mysql = snapshot.servers[MY]
    assert mysql.healthy and {n: t.tool_class for n, t in mysql.tools.items()} == {
        "run_select": ToolClass.READ,
        "extra": ToolClass.UNCLASSIFIED,
    }


@pytest.mark.spec("S06-AC6")
async def test_discovery_failure_on_one_server_is_isolated(tmp_path: Path) -> None:
    servers = {PG: FakeServer(list_error=RuntimeError("boom")), MY: FakeServer(tools=[SELECT])}
    snapshot = await manager(tmp_path, servers).discover()
    assert snapshot.servers[PG].healthy is False
    assert snapshot.servers[PG].error == "discovery failed: RuntimeError"
    assert snapshot.servers[MY].healthy


async def test_disabled_servers_are_not_probed(tmp_path: Path) -> None:
    connector = FakeMCPConnector({PG: FakeServer()})
    settings = mcp_settings(
        tmp_path, mcp_mysql_enabled=False, mcp_s3_enabled=False, mcp_mongodb_enabled=False
    )
    mgr = MCPManager(settings, registry(tmp_path, ENTRIES), connector, env="test")
    snapshot = await mgr.discover()
    assert connector.health_checks == 1
    assert snapshot.servers[MY].error == "disabled"


async def test_snapshot_is_replaced_atomically(tmp_path: Path) -> None:
    servers = {PG: FakeServer(tools=[SELECT])}
    mgr = manager(tmp_path, servers)
    first = await mgr.discover()
    servers[PG].tools.append(tool("new_tool"))
    second = await mgr.discover()
    assert "new_tool" not in first.servers[PG].tools
    assert "new_tool" in second.servers[PG].tools
    assert mgr.snapshot() is second


async def test_discoveries_never_overlap(tmp_path: Path) -> None:
    connector = FakeMCPConnector({PG: FakeServer(tools=[SELECT])})
    active, peak = 0, 0
    real_list = connector.list_tools

    async def slow_list(server: object) -> list[ToolSpec]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return await real_list(server)  # type: ignore[arg-type]

    connector.list_tools = slow_list  # type: ignore[method-assign]
    mgr = MCPManager(mcp_settings(tmp_path), registry(tmp_path, ENTRIES), connector, env="test")
    await asyncio.gather(*(mgr.discover() for _ in range(4)))
    assert peak == 1


async def test_untrusted_tool_names_are_ignored_not_echoed(tmp_path: Path) -> None:
    hostile = [tool("ok_tool"), tool("bad name;rm"), tool("dup"), tool("dup"), tool("x" * 200)]
    snapshot = await manager(tmp_path, {PG: FakeServer(tools=hostile)}).discover()
    state = snapshot.servers[PG]
    assert set(state.tools) == {"ok_tool"}
    assert state.ignored_tools == 4


# --- AC7: autostart policy -----------------------------------------------------------------------


@pytest.mark.spec("S06-AC7")
def test_autostart_and_plain_http_remotes_are_refused_in_prod() -> None:
    with pytest.raises(ConfigurationError, match="MCP_AUTOSTART"):
        MCPSettings(_env_file=None, mcp_autostart=True).check_for_env("prod")
    # (The test fixture disables servers by default; this one must be enabled to be checked.)
    remote = MCPSettings(
        _env_file=None, mcp_mysql_enabled=True, mcp_mysql_url="http://mcp.internal:8002/mcp"
    )
    with pytest.raises(ConfigurationError, match="https or a loopback"):
        remote.check_for_env("prod")
    MCPSettings(
        _env_file=None, mcp_mysql_enabled=True, mcp_mysql_url="https://mcp.internal/mcp"
    ).check_for_env("prod")
    MCPSettings(_env_file=None).check_for_env("prod")  # loopback defaults are fine


@pytest.mark.spec("S06-AC7")
@pytest.mark.parametrize("module", ["os", "mcp_servers.x; rm -rf /", "../evil", "mcp_servers"])
def test_autostart_modules_are_allowlisted(module: str) -> None:
    with pytest.raises(ValidationError):
        MCPSettings(_env_file=None, mcp_postgresql_module=module)


@pytest.mark.spec("S06-AC7")
@pytest.mark.parametrize(
    "url", ["http://mcp.internal:8001/mcp", "http://localhost:8001/mcp", "http://0.0.0.0:8001/mcp"]
)
async def test_autostart_only_for_literal_loopback(tmp_path: Path, url: str) -> None:
    remote = manager(tmp_path, {}, mcp_autostart=True, mcp_postgresql_url=url)
    assert remote.autostart_allowed(remote.server_config(PG)) is False
    assert await remote.start_local(PG) is False


@pytest.mark.spec("S06-AC7")
def test_autostart_is_never_allowed_in_prod(tmp_path: Path) -> None:
    prod = MCPManager(
        mcp_settings(tmp_path, mcp_autostart=True),
        registry(tmp_path, ENTRIES),
        FakeMCPConnector({}),
        env="prod",
    )
    assert prod.autostart_allowed(prod.server_config(PG)) is False


@pytest.mark.spec("S06-AC7")
def test_autostarted_servers_get_a_minimal_environment() -> None:
    env = child_env(
        {
            "PATH": "/usr/bin",
            "SYSTEMROOT": r"C:\Windows",
            "MCP_POSTGRES_READER_DSN": "postgresql://reader@db/dw",
            "PG_PASSWORD": "secret",
            "API_TOKENS": "{...}",
            "AIRFLOW_PASSWORD": "secret",
            "GOOGLE_APPLICATION_CREDENTIALS": "/keys/sa.json",
        }
    )
    assert set(env) == {"PATH", "SYSTEMROOT", "MCP_POSTGRES_READER_DSN"}


@pytest.mark.spec("S07-AC6")
def test_child_env_keeps_the_login_name_for_getpass() -> None:
    # aiomysql calls getpass.getuser() at import; on Windows it needs USERNAME (no pwd module).
    env = child_env({"USERNAME": "dev", "USER": "dev", "LOGNAME": "dev", "PG_USER": "x"})
    assert set(env) == {"USERNAME", "USER", "LOGNAME"}


@pytest.mark.parametrize(
    "url", ["ftp://x/mcp", "http://user:pw@127.0.0.1:8001/mcp", "http://127.0.0.1:8001/mcp?t=1"]
)
def test_unsafe_server_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        MCPSettings(_env_file=None, mcp_postgresql_url=url)


def test_output_cap_cannot_exceed_nfr_006() -> None:
    with pytest.raises(ValidationError):
        MCPSettings(_env_file=None, mcp_max_output_bytes=9000)
