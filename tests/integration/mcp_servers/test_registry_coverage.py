"""Both database servers through the real S06 manager and the committed registry (S07-AC6, AC11).

Discovered = registered: every tool the servers advertise is pinned in
``config/tool_permissions.yaml`` with the right access, and nothing is excluded.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from backend.domain.audit import AuditEvent
from backend.domain.common import SystemName
from backend.mcp.connector import SdkMCPConnector
from backend.mcp.errors import NoWriteTools, ToolCallError
from backend.mcp.manager import MCPManager
from backend.mcp.models import InvocationContext, ToolClass
from backend.mcp.permissions import ToolPermissionRegistry
from backend.mcp.toolsets import diagnostic_toolset, execution_toolset
from mcp_servers.common.settings import MySQLServerSettings, PostgresServerSettings
from tests.fakes.mcp import mcp_settings
from tests.integration.mcp_servers.conftest import REPO_ROOT, secrets_of

pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]

PG, MY = SystemName.POSTGRESQL, SystemName.MYSQL
REGISTRY_FILE = REPO_ROOT / "config" / "tool_permissions.yaml"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def registered() -> dict[str, dict[str, str]]:
    data = yaml.safe_load(REGISTRY_FILE.read_text(encoding="utf-8"))
    out: dict[str, dict[str, str]] = {}
    for item in data["tools"]:
        out.setdefault(item["server"], {})[item["tool"]] = item["access"]
    return out


class Servers:
    def __init__(self) -> None:
        self.ports: dict[str, int] = {}
        self.processes: list[subprocess.Popen[bytes]] = []
        self.logs: dict[str, Path] = {}

    def start(self, key: str, module: str, env: dict[str, str], log_dir: Path) -> None:
        port = free_port()
        port_var = "MCP_POSTGRES_PORT" if module.endswith("postgres") else "MCP_MYSQL_PORT"
        log = log_dir / f"{key}.log"
        self.logs[key] = log
        with log.open("wb") as handle:
            process = subprocess.Popen(  # noqa: S603 - this interpreter + a fixed module
                [sys.executable, "-m", module],
                cwd=REPO_ROOT,
                env={**os.environ, **env, port_var: str(port)},
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
        self.ports[key] = port
        self.processes.append(process)

    def wait(self) -> None:
        deadline = time.monotonic() + 60
        for key, port in self.ports.items():
            while True:
                try:
                    body = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).json()
                    if body.get("db") == "ok":
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise AssertionError(f"{key} server did not become healthy")
                time.sleep(0.25)

    def stop(self) -> None:
        for process in self.processes:
            process.terminate()
        for process in self.processes:
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()


def server_env(settings: PostgresServerSettings | MySQLServerSettings) -> dict[str, str]:
    env = {"MCP_PII_COLUMNS": json.dumps(settings.mcp_pii_columns)}
    for name, value in settings.model_dump().items():
        if name.endswith("_dsn"):
            env[name.upper()] = value.get_secret_value() if value is not None else ""
    return env


@pytest.fixture(scope="module")
def servers(
    pg_settings: PostgresServerSettings,
    my_settings: MySQLServerSettings,
    seeded: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Servers]:
    log_dir = tmp_path_factory.mktemp("mcp-logs")
    running = Servers()
    try:
        running.start("postgres", "mcp_servers.postgres", server_env(pg_settings), log_dir)
        running.start("mysql", "mcp_servers.mysql", server_env(my_settings), log_dir)
        reader_only = {**server_env(pg_settings), "MCP_POSTGRES_WRITER_DSN": ""}
        running.start("postgres_reader_only", "mcp_servers.postgres", reader_only, log_dir)
        running.wait()
        yield running
    finally:
        running.stop()


def manager_for(tmp_path: Path, pg_port: int, my_port: int) -> MCPManager:
    settings = mcp_settings(
        tmp_path,
        tool_permissions_file=REGISTRY_FILE,
        mcp_postgresql_url=f"http://127.0.0.1:{pg_port}/mcp",
        mcp_mysql_url=f"http://127.0.0.1:{my_port}/mcp",
        mcp_s3_enabled=False,
        mcp_mongodb_enabled=False,
        mcp_autostart=False,
        mcp_probe_timeout_s=5,
    )
    return MCPManager(
        settings,
        ToolPermissionRegistry.load(REGISTRY_FILE),
        SdkMCPConnector(probe_timeout_s=5),
        env="dev",
    )


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> int:
        self.events.append(event)
        return len(self.events)


@pytest.mark.spec("S07-AC6")
async def test_discovered_equals_registered(servers: Servers, tmp_path: Path) -> None:
    manager = manager_for(tmp_path, servers.ports["postgres"], servers.ports["mysql"])
    try:
        snapshot = await manager.discover()
    finally:
        await manager.shutdown()
    expected = registered()
    for system in (PG, MY):
        state = snapshot.servers[system]
        assert state.healthy, state.error
        assert state.ignored_tools == 0
        classes = {name: tool.tool_class for name, tool in state.tools.items()}
        want = {
            name: ToolClass.WRITE if access == "write" else ToolClass.READ
            for name, access in expected[system.value].items()
        }
        assert classes == want, (system, {n: c for n, c in classes.items() if want.get(n) != c})


@pytest.mark.spec("S07-AC4", "S07-AC6", "S07-AC8", "S07-AC10")
async def test_canonical_answers_through_s06_tool_handles(servers: Servers, tmp_path: Path) -> None:
    manager = manager_for(tmp_path, servers.ports["postgres"], servers.ports["mysql"])
    try:
        await manager.discover()
        audit = RecordingAudit()
        handles = {h.name: h for h in diagnostic_toolset(manager, audit)}
        assert not any(h.access is ToolClass.WRITE for h in handles.values())
        ctx = InvocationContext(phase="test")

        async def call(name: str, args: dict[str, Any]) -> Any:
            return json.loads((await handles[name].invoke(args, ctx)).content)

        dupes = await call(
            "postgresql.find_duplicates", {"table": "dw.customer", "key_columns": ["customer_id"]}
        )
        assert dupes["duplicate_keys"] == 417
        target = await call("postgresql.get_row_count", {"table": "dw.customer"})
        source = await call("mysql.get_row_count", {"table": "sales.customer"})
        assert (target["row_count"], source["row_count"]) == (100_417, 100_000)
        with pytest.raises(ToolCallError) as info:
            await handles["postgresql.run_select"].invoke(
                {"sql": "SELECT email FROM dw.customer"}, ctx
            )
        assert "PII" in str(info.value.detail)
    finally:
        await manager.shutdown()
    # Every call is audited (invariant 12): a STARTED row, then its outcome; no PII in any row.
    rows = [(e.tool_name, e.status, e.operation) for e in audit.events]
    assert rows == [
        ("postgresql.find_duplicates", "STARTED", "read"),
        ("postgresql.find_duplicates", "OK", "read"),
        ("postgresql.get_row_count", "STARTED", "read"),
        ("postgresql.get_row_count", "OK", "read"),
        ("mysql.get_row_count", "STARTED", "read"),
        ("mysql.get_row_count", "OK", "read"),
        ("postgresql.run_select", "STARTED", "read"),
        ("postgresql.run_select", "TOOL_ERROR", "read"),
    ]
    dumped = " ".join(e.model_dump_json() for e in audit.events)
    assert "@example.test" not in dumped and "+1-555-" not in dumped


@pytest.mark.spec("S07-AC6", "S07-AC10")
def test_health_answers_without_secrets(
    servers: Servers, pg_settings: PostgresServerSettings, my_settings: MySQLServerSettings
) -> None:
    for port in servers.ports.values():
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
        assert response.status_code == 200
        assert response.json()["db"] == "ok"
    logs = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in servers.logs.values()
    )
    bodies = "\n".join(
        httpx.get(f"http://127.0.0.1:{p}/health", timeout=5).text for p in servers.ports.values()
    )
    for secret in secrets_of(pg_settings, my_settings):
        assert secret not in logs and secret not in bodies


@pytest.mark.spec("S07-AC11")
async def test_reader_only_server_has_no_write_tools(servers: Servers, tmp_path: Path) -> None:
    manager = manager_for(tmp_path, servers.ports["postgres_reader_only"], servers.ports["mysql"])
    try:
        snapshot = await manager.discover()
        state = snapshot.servers[PG]
        assert state.healthy and not any(n.startswith("execute_") for n in state.tools)
        with pytest.raises(NoWriteTools):
            execution_toolset(manager, RecordingAudit(), "postgresql")
        # The MySQL server has a writer: its execution toolset carries execute_dml.
        names = {h.name for h in execution_toolset(manager, RecordingAudit(), "mysql")}
        assert "mysql.execute_dml" in names
    finally:
        await manager.shutdown()
