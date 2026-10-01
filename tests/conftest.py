from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from backend.airflow.settings import AirflowSettings
from backend.app import create_app
from backend.config import Settings
from backend.db.settings import DatabaseSettings
from backend.mcp.settings import MCPSettings
from backend.metadata.settings import MetadataSettings
from backend.security.auth import hash_token
from frontend.api_client import FrontendSettings

# Plaintext test tokens per role; only their SHA-256 digests reach the app configuration.
ROLE_TOKENS: dict[str, str] = {
    "viewer": "test-viewer-token",
    "operator": "test-operator-token",
    "reviewer": "test-reviewer-token",
    "admin": "test-admin-token",
    "service": "test-service-token",
}


def tokens_json(tokens: dict[str, str] = ROLE_TOKENS) -> str:
    return json.dumps(
        {hash_token(tok): {"subject": f"{role}@test", "role": role} for role, tok in tokens.items()}
    )


def auth(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {ROLE_TOKENS[role]}"}


_SETTINGS_ENV_VARS = (
    "ENV",
    "SERVICE_NAME",
    "VERSION",
    "LOG_LEVEL",
    "CORS_ALLOW_ORIGINS",
    "API_TOKENS",
    "API_TOKENS_FILE",
    "AGENT_BASE_URL",
    "AGENT_API_TOKEN",
    "AGENT_HEALTH_TIMEOUT_SECONDS",
    "AUDIT_MAX_PAYLOAD_BYTES",
    "PG_HOST",
    "PG_PORT",
    "PG_DATABASE",
    "PG_USER",
    "PG_PASSWORD",
    "PG_POOL_MAX",
    "PG_STATEMENT_TIMEOUT_MS",
    "PG_CONNECT_TIMEOUT_S",
    "PG_ACQUIRE_TIMEOUT_S",
    "PG_MIGRATION_LOCK_TIMEOUT_MS",
    "MIGRATIONS_DIR",
    "METADATA_DIR",
    "METADATA_MAX_FILE_BYTES",
    "METADATA_REFRESH_INTERVAL_S",
    "AIRFLOW_BASE_URL",
    "AIRFLOW_USERNAME",
    "AIRFLOW_PASSWORD",
    "AIRFLOW_API_VERSION",
    "AIRFLOW_CONNECT_TIMEOUT_S",
    "AIRFLOW_READ_TIMEOUT_S",
    "AIRFLOW_GET_RETRIES",
    "AIRFLOW_LOG_MAX_CHARS",
    "AIRFLOW_LOG_MAX_DOWNLOAD_BYTES",
    "AIRFLOW_CONTEXT_RUN_LIMIT",
    "RECOVERY_POLL_SECONDS",
    "MCP_POSTGRESQL_URL",
    "MCP_POSTGRESQL_ENABLED",
    "MCP_POSTGRESQL_REQUIRED",
    "MCP_POSTGRESQL_MODULE",
    "MCP_MYSQL_URL",
    "MCP_MYSQL_ENABLED",
    "MCP_MYSQL_REQUIRED",
    "MCP_MYSQL_MODULE",
    "MCP_S3_URL",
    "MCP_S3_ENABLED",
    "MCP_S3_REQUIRED",
    "MCP_S3_MODULE",
    "MCP_MONGODB_URL",
    "MCP_MONGODB_ENABLED",
    "MCP_MONGODB_REQUIRED",
    "MCP_MONGODB_MODULE",
    "MCP_AUTOSTART",
    "MCP_DEFAULT_TIMEOUT_S",
    "MCP_MAX_OUTPUT_BYTES",
    "MCP_PROBE_TIMEOUT_S",
    "MCP_REFRESH_INTERVAL_S",
    "MCP_READ_RETRIES",
    "TOOL_PERMISSIONS_FILE",
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep developer shells and a local .env file from leaking into tests."""
    for key in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    # Tests never reach real MCP endpoints unless they opt in with explicit MCPSettings (S06).
    for system in ("POSTGRESQL", "MYSQL", "S3", "MONGODB"):
        monkeypatch.setenv(f"MCP_{system}_ENABLED", "false")
    for settings_class in (
        Settings,
        DatabaseSettings,
        MetadataSettings,
        AirflowSettings,
        MCPSettings,
        FrontendSettings,
    ):
        monkeypatch.setitem(settings_class.model_config, "env_file", None)


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {"env": "test", "api_tokens": SecretStr(tokens_json())}
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return factory


@pytest.fixture
def app(make_settings: Callable[..., Settings]) -> FastAPI:
    return create_app(make_settings())


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
