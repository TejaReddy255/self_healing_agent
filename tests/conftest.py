from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from backend.app import create_app
from backend.config import Settings
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
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep developer shells and a local .env file from leaking into tests."""
    for key in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setitem(FrontendSettings.model_config, "env_file", None)


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
