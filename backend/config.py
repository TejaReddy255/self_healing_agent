"""Control-plane settings (S01).

All configuration comes from the environment or a local ``.env`` file. ``ENV`` is required so a
deployment can never silently run with development rules. Settings that represent credentials
are ``SecretStr | None`` with **no default value** (NFR-003); production startup fails fast when a
required value is missing or unsafe.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

Environment = Literal["dev", "test", "prod"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class ConfigurationError(RuntimeError):
    """Raised when settings are missing or unsafe for the selected environment."""


def _installed_version() -> str:
    try:
        return package_version("self-healing-agents")
    except PackageNotFoundError:
        return "0.0.0+unknown"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        # Validation errors must never echo raw input (it may contain tokens or DSNs).
        hide_input_in_errors=True,
    )

    env: Environment
    service_name: str = "self-healing-control-plane"
    version: str = Field(default_factory=_installed_version)
    log_level: LogLevel = "INFO"
    cors_allow_origins: list[str] = Field(default_factory=list)

    # Token → Actor mapping, JSON {"<sha256 of token>": {"subject": "...", "role": "..."}}.
    # Either a file path or the JSON itself (secret); plaintext tokens are never configured.
    api_tokens_file: Path | None = None
    api_tokens: SecretStr | None = None

    @model_validator(mode="after")
    def _validate_environment(self) -> Self:
        if self.env == "prod":
            if self.api_tokens_file is None and self.api_tokens is None:
                raise ValueError(
                    "ENV=prod requires a token source: set API_TOKENS_FILE or API_TOKENS"
                )
            if "*" in self.cors_allow_origins:
                raise ValueError(
                    "ENV=prod forbids a wildcard in CORS_ALLOW_ORIGINS; list explicit origins"
                )
        return self


def load_settings() -> Settings:
    """Load settings from the environment, converting validation errors to a clear failure."""
    try:
        return Settings()  # ENV comes from the environment / .env
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'settings'}: {err['msg']}"
            for err in exc.errors(include_url=False, include_input=False)
        )
        raise ConfigurationError(f"Invalid configuration: {details}") from None
    except SettingsError as exc:
        # e.g. a list setting such as CORS_ALLOW_ORIGINS that is not valid JSON
        raise ConfigurationError(
            f"Invalid configuration: {exc}. List settings must be JSON, e.g. "
            '["https://ops.example.com"]'
        ) from None
