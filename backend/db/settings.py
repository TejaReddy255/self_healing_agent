"""Operational PostgreSQL settings (S02).

Kept separate from the API ``Settings`` so the migration CLI can run without API configuration.
``PG_PASSWORD`` has no default; when it is unset the database is treated as *not configured*
(allowed only outside prod, see ``backend.app.create_app``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.config import load_or_fail

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    pg_host: str = "127.0.0.1"
    pg_port: int = Field(default=5432, ge=1, le=65535)
    pg_database: str = "selfhealing"
    pg_user: str = "selfhealing"
    pg_password: SecretStr | None = None
    pg_pool_max: int = Field(default=10, ge=1, le=100)
    pg_statement_timeout_ms: int = Field(default=30_000, ge=100, le=3_600_000)
    pg_connect_timeout_s: float = Field(default=5.0, gt=0, le=60)
    pg_acquire_timeout_s: float = Field(default=5.0, gt=0, le=60)
    migrations_dir: Path = DEFAULT_MIGRATIONS_DIR

    @property
    def configured(self) -> bool:
        return self.pg_password is not None and bool(self.pg_password.get_secret_value())

    def describe(self) -> str:
        """Non-secret description for logs and error messages."""
        return f"{self.pg_user}@{self.pg_host}:{self.pg_port}/{self.pg_database}"

    def connect_kwargs(self, *, application_name: str = "self-healing") -> dict[str, Any]:
        """Keyword arguments for ``asyncpg.connect`` / ``asyncpg.create_pool``."""
        return {
            "host": self.pg_host,
            "port": self.pg_port,
            "user": self.pg_user,
            "password": self.pg_password.get_secret_value() if self.pg_password else None,
            "database": self.pg_database,
            "timeout": self.pg_connect_timeout_s,
            "command_timeout": self.pg_statement_timeout_ms / 1000 + 5,
            "server_settings": {
                "statement_timeout": str(self.pg_statement_timeout_ms),
                "application_name": application_name,
            },
        }


def load_database_settings() -> DatabaseSettings:
    return load_or_fail(DatabaseSettings)
