"""HTTP client for the FastAPI control plane. The frontend talks to nothing else (invariant 11)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class FrontendSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    agent_base_url: str = "http://127.0.0.1:8000"
    agent_api_token: SecretStr | None = None
    agent_health_timeout_seconds: float = Field(default=5.0, gt=0, le=60)


@dataclass(frozen=True)
class ReadinessView:
    """Readiness as shown to the operator; ``error`` is set when it could not be obtained."""

    status: str | None = None
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None


class AgentApiClient:
    def __init__(
        self,
        base_url: str,
        token: SecretStr | None,
        timeout_s: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        secret = token.get_secret_value().strip() if token is not None else ""
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._client = httpx.Client(
            base_url=self._base_url, headers=headers, timeout=timeout_s, transport=transport
        )

    @classmethod
    def from_settings(cls, settings: FrontendSettings) -> AgentApiClient:
        return cls(
            settings.agent_base_url,
            settings.agent_api_token,
            settings.agent_health_timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

    def get_readiness(self) -> ReadinessView:
        try:
            response = self._client.get("/health/ready")
        except httpx.TimeoutException:
            return ReadinessView(
                error=f"The backend did not respond within {self._timeout_s:g} s. "
                "It may be overloaded or starting up; try again shortly."
            )
        except httpx.TransportError:
            return ReadinessView(
                error=f"Backend unreachable at {self._base_url}. "
                "Check that the API is running and AGENT_BASE_URL is correct."
            )

        if response.status_code == 401:
            return ReadinessView(
                error="Authentication failed: set AGENT_API_TOKEN to a valid API token."
            )
        if response.status_code == 403:
            return ReadinessView(error="Your API token does not have the Viewer role.")
        if response.status_code not in (200, 503):
            return ReadinessView(
                error=f"Unexpected response from the backend (HTTP {response.status_code})."
            )
        unreadable = ReadinessView(error="The backend returned an unreadable response.")
        try:
            body = response.json()
        except ValueError:
            return unreadable
        if not isinstance(body, dict) or not isinstance(body.get("components", {}), dict):
            return unreadable
        return ReadinessView(status=body.get("status"), components=body.get("components", {}))
