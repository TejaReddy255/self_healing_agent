"""Control-plane settings (S01).

All configuration comes from the environment or a local ``.env`` file. Settings that represent
credentials are ``SecretStr | None`` with **no default value** (NFR-003); production startup fails
fast when a required value is missing or unsafe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "test", "prod"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class ConfigurationError(RuntimeError):
    """Raised when settings are missing or unsafe for the selected environment."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    env: Environment = "dev"
    service_name: str = "self-healing-control-plane"
    version: str = "0.1.0"
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
        return Settings()
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'settings'}: {err['msg']}"
            for err in exc.errors(include_url=False)
        )
        raise ConfigurationError(f"Invalid configuration: {details}") from exc
