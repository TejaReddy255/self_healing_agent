from __future__ import annotations

from pathlib import Path

import pytest

from backend.config import ConfigurationError
from backend.domain.common import SystemName
from backend.mcp.manager import MCPManager
from backend.mcp.models import ToolClass
from backend.mcp.settings import MCPSettings
from tests.fakes.mcp import FakeMCPConnector, FakeServer, mcp_settings, registry, tool

PG, MY = SystemName.POSTGRESQL, SystemName.MYSQL
ENTRIES = [
    {"server": "postgresql", "tool": "run_select", "access": "read"},
    {"server": "mysql", "tool": "run_select", "access": "read"},
]


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
    mgr = manager(
        tmp_path,
        {PG: FakeServer(healthy=False), MY: FakeServer(tools=[tool("run_select"), tool("extra")])},
    )
    snapshot = await mgr.discover()
    assert snapshot.servers[PG].healthy is False and snapshot.servers[PG].error == "unreachable"
    mysql = snapshot.servers[MY]
    assert mysql.healthy and {n: t.tool_class for n, t in mysql.tools.items()} == {
        "run_select": ToolClass.READ,
        "extra": ToolClass.UNCLASSIFIED,
    }


@pytest.mark.spec("S06-AC6")
async def test_discovery_failure_on_one_server_is_isolated(tmp_path: Path) -> None:
    mgr = manager(
        tmp_path,
        {
            PG: FakeServer(list_error=RuntimeError("boom")),
            MY: FakeServer(tools=[tool("run_select")]),
        },
    )
    snapshot = await mgr.discover()
    assert snapshot.servers[PG].healthy is False
    assert snapshot.servers[PG].error == "discovery failed: RuntimeError"
    assert snapshot.servers[MY].healthy


async def test_disabled_servers_are_not_probed(tmp_path: Path) -> None:
    connector = FakeMCPConnector({PG: FakeServer()})
    mgr = MCPManager(
        mcp_settings(
            tmp_path, mcp_mysql_enabled=False, mcp_s3_enabled=False, mcp_mongodb_enabled=False
        ),
        registry(tmp_path, ENTRIES),
        connector,
        env="test",
    )
    snapshot = await mgr.discover()
    assert connector.health_checks == 1
    assert snapshot.servers[MY].error == "disabled"


async def test_snapshot_is_replaced_atomically(tmp_path: Path) -> None:
    servers = {PG: FakeServer(tools=[tool("run_select")])}
    mgr = manager(tmp_path, servers)
    first = await mgr.discover()
    servers[PG].tools.append(tool("new_tool"))
    second = await mgr.discover()
    assert "new_tool" not in first.servers[PG].tools  # old snapshot never mutates
    assert "new_tool" in second.servers[PG].tools
    assert mgr.snapshot() is second


# --- AC7: autostart policy -----------------------------------------------------------------------


@pytest.mark.spec("S06-AC7")
def test_autostart_is_refused_in_prod() -> None:
    settings = MCPSettings(_env_file=None, mcp_autostart=True)
    with pytest.raises(ConfigurationError, match="MCP_AUTOSTART"):
        settings.check_for_env("prod")
    settings.check_for_env("dev")


@pytest.mark.spec("S06-AC7")
@pytest.mark.parametrize("module", ["os", "mcp_servers.x; rm -rf /", "../evil", "mcp_servers"])
def test_autostart_modules_are_allowlisted(module: str) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MCPSettings(_env_file=None, mcp_postgresql_module=module)


@pytest.mark.spec("S06-AC7")
async def test_autostart_only_for_loopback_and_never_in_prod(tmp_path: Path) -> None:
    remote = manager(
        tmp_path, {}, mcp_autostart=True, mcp_postgresql_url="http://mcp.internal:8001/mcp"
    )
    assert remote.autostart_allowed(remote.server_config(PG)) is False
    assert await remote.start_local(PG) is False

    prod = MCPManager(
        mcp_settings(tmp_path, mcp_autostart=True), registry(tmp_path, ENTRIES),
        FakeMCPConnector({}), env="prod",
    )  # fmt: skip
    assert prod.autostart_allowed(prod.server_config(PG)) is False


@pytest.mark.parametrize(
    "url", ["ftp://x/mcp", "http://user:pw@127.0.0.1:8001/mcp", "http://127.0.0.1:8001/mcp?t=1"]
)
def test_unsafe_server_urls_are_rejected(url: str) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MCPSettings(_env_file=None, mcp_postgresql_url=url)
