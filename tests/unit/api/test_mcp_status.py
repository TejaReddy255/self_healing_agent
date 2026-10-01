from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import ConfigurationError, Settings
from backend.domain.common import SystemName
from tests.conftest import auth
from tests.fakes.mcp import (
    FakeMCPConnector,
    FakeServer,
    entry,
    mcp_settings,
    tool,
    write_registry,
)

PG, MY = SystemName.POSTGRESQL, SystemName.MYSQL
SELECT, DML, DROP = tool("run_select"), tool("execute_dml"), tool("drop_table")
ENTRIES = [
    entry("postgresql", SELECT, "read"),
    entry("postgresql", DML, "write"),
    entry("postgresql", DROP, "read"),  # MISMATCH
]


def app_with(
    make_settings: Callable[..., Settings],
    tmp_path: Path,
    servers: dict[SystemName, FakeServer],
    **overrides: Any,
) -> TestClient:
    write_registry(tmp_path / "perms.yaml", ENTRIES)
    app = create_app(
        make_settings(),
        mcp_settings=mcp_settings(tmp_path, **overrides),
        mcp_connector=FakeMCPConnector(servers),
    )
    return TestClient(app)


@pytest.mark.spec("S06-AC2")
def test_status_lists_classes_and_unusable_tools(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    pg = FakeServer(tools=[SELECT, DML, DROP, tool("mystery")])
    with app_with(make_settings, tmp_path, {PG: pg}) as client:
        body = client.get("/mcp-status", headers=auth("viewer")).json()
        postgres = next(s for s in body["servers"] if s["name"] == "postgresql")
        assert postgres["healthy"] is True and postgres["url_alias"] == "127.0.0.1:8001"
        assert postgres["counts"] == {
            "read": 1, "write": 1, "unclassified": 1, "mismatch": 1, "schema_changed": 0,
        }  # fmt: skip
        assert postgres["unclassified"] == ["mystery"] and postgres["mismatch"] == ["drop_table"]
        assert body["diagnostic_write_tools_blocked"] == 3
        assert client.get("/mcp-status").status_code == 401


@pytest.mark.spec("S06-AC6")
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "degraded"),
        ({"mcp_mysql_required": True}, "down"),
        (
            {"mcp_mysql_enabled": False, "mcp_s3_enabled": False, "mcp_mongodb_enabled": False},
            "ok",
        ),
    ],
    ids=["optional-down", "required-down", "all-enabled-healthy"],
)
def test_readiness_reflects_server_health(
    make_settings: Callable[..., Settings], tmp_path: Path, overrides: dict[str, Any], expected: str
) -> None:
    servers = {PG: FakeServer(tools=[SELECT])}  # mysql/s3/mongodb have no fake: down
    with app_with(make_settings, tmp_path, servers, **overrides) as client:
        ready = client.get("/health/ready", headers=auth("viewer"))
        assert ready.json()["components"]["mcp"]["status"] == expected
        assert ready.status_code == (503 if expected == "down" else 200)


@pytest.mark.spec("S06-AC6")
def test_readiness_never_calls_mcp_inline(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    connector = FakeMCPConnector({PG: FakeServer()})
    write_registry(tmp_path / "perms.yaml", ENTRIES)
    app = create_app(make_settings(), mcp_settings=mcp_settings(tmp_path), mcp_connector=connector)
    with TestClient(app) as client:
        after_startup = connector.health_checks
        for _ in range(3):
            client.get("/health/ready", headers=auth("viewer"))
        assert connector.health_checks == after_startup


@pytest.mark.spec("S06-AC10")
def test_app_refuses_to_start_with_an_invalid_registry(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    (tmp_path / "perms.yaml").write_text("version: 1\ntools: [broken\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="TOOL_PERMISSIONS_FILE"):
        create_app(make_settings(), mcp_settings=mcp_settings(tmp_path))


@pytest.mark.spec("S06-AC7")
def test_app_refuses_autostart_in_prod(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    from pydantic import SecretStr

    from backend.airflow.settings import AirflowSettings
    from backend.db.settings import DatabaseSettings

    write_registry(tmp_path / "perms.yaml", [])
    with pytest.raises(ConfigurationError, match="MCP_AUTOSTART"):
        create_app(
            make_settings(env="prod", cors_allow_origins=["https://ops.example.com"]),
            db_settings=DatabaseSettings(_env_file=None, pg_password=SecretStr("x"), pg_port=1),
            airflow_settings=AirflowSettings(
                _env_file=None,
                airflow_base_url="https://airflow.example.com",
                airflow_username="svc",
                airflow_password=SecretStr("x"),
            ),
            mcp_settings=mcp_settings(tmp_path, mcp_autostart=True),
        )
