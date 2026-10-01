"""Airflow connection settings (S05).

The platform's Airflow credentials come only from the environment. ``AIRFLOW_BASE_URL`` unset means
*not configured*, which is allowed outside prod (routes answer 503, readiness is degraded).
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.config import ConfigurationError, load_or_fail


class AirflowSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    airflow_base_url: str | None = None
    airflow_username: str | None = None
    airflow_password: SecretStr | None = None
    # Seam for Airflow 3 (/api/v2, OQ-03); only the 2.x stable REST API exists today.
    airflow_api_version: Literal["v1"] = "v1"
    airflow_connect_timeout_s: float = Field(default=5.0, gt=0, le=60)
    airflow_read_timeout_s: float = Field(default=30.0, gt=0, le=300)
    airflow_get_retries: int = Field(default=3, ge=0, le=10)
    airflow_log_max_chars: int = Field(default=20_000, ge=1_000, le=1_000_000)
    airflow_log_max_download_bytes: int = Field(default=2_097_152, ge=4_096, le=64 * 1_048_576)
    airflow_context_run_limit: int = Field(default=5, ge=1, le=100)
    recovery_poll_seconds: float = Field(default=30.0, gt=0, le=3_600)

    @field_validator("airflow_base_url", "airflow_username", "airflow_password", mode="before")
    @classmethod
    def _empty_means_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("airflow_base_url")
    @classmethod
    def _plain_http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("AIRFLOW_BASE_URL must be an http(s) URL with a host")
        if parts.username or parts.password:
            raise ValueError("AIRFLOW_BASE_URL must not embed credentials")
        if parts.query or parts.fragment:
            raise ValueError("AIRFLOW_BASE_URL must not have a query or fragment")
        return value.strip().rstrip("/")

    @model_validator(mode="after")
    def _credentials_with_url(self) -> AirflowSettings:
        if self.airflow_base_url is not None and (
            self.airflow_username is None or self.airflow_password is None
        ):
            raise ValueError(
                "AIRFLOW_USERNAME and AIRFLOW_PASSWORD are required with AIRFLOW_BASE_URL"
            )
        return self

    @property
    def configured(self) -> bool:
        return self.airflow_base_url is not None

    @property
    def api_root(self) -> str:
        if self.airflow_base_url is None:
            raise ConfigurationError("Airflow is not configured")
        return f"{self.airflow_base_url}/api/{self.airflow_api_version}"

    def check_for_env(self, env: str) -> None:
        """Production needs a configured Airflow over https (credentials are basic auth)."""
        if env != "prod":
            return
        if not self.configured:
            raise ConfigurationError(
                "ENV=prod requires AIRFLOW_BASE_URL, AIRFLOW_USERNAME and AIRFLOW_PASSWORD"
            )
        if not str(self.airflow_base_url).startswith("https://"):
            raise ConfigurationError("ENV=prod requires an https:// AIRFLOW_BASE_URL")

    def describe(self) -> str:
        """Non-secret description for logs."""
        return (
            f"{self.airflow_base_url} (api {self.airflow_api_version})"
            if self.configured
            else "not configured"
        )


def load_airflow_settings() -> AirflowSettings:
    return load_or_fail(AirflowSettings)
