"""MCP server settings (S07). Only ``MCP_*`` variables: the S06 autostart passes nothing else."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_PII_COLUMN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*$"
)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")


def _json_list(value: object) -> object:
    if isinstance(value, str):
        import json

        try:
            return json.loads(value) if value.strip() else []
        except json.JSONDecodeError as exc:
            raise ValueError("must be a JSON list of strings") from exc
    return value


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    mcp_bind_host: str = "127.0.0.1"
    # NFR-006: at most 20 rows of data per tool result reach the agent by default.
    mcp_row_limit: int = Field(default=20, ge=1, le=200)
    mcp_statement_timeout_ms: int = Field(default=30_000, ge=100, le=600_000)
    mcp_max_affected_rows: int = Field(default=10_000, ge=1, le=1_000_000)
    mcp_pii_columns: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("mcp_bind_host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        # The servers do not authenticate clients yet (S23): never expose them beyond loopback.
        if value not in LOOPBACK_HOSTS:
            raise ValueError("MCP_BIND_HOST must be a loopback address until client auth (S23)")
        return value

    @field_validator("mcp_pii_columns", mode="before")
    @classmethod
    def _decode_pii(cls, value: object) -> object:
        return _json_list(value)

    @field_validator("mcp_pii_columns")
    @classmethod
    def _check_pii(cls, value: list[str]) -> list[str]:
        for item in value:
            if not _PII_COLUMN.fullmatch(item):
                raise ValueError("MCP_PII_COLUMNS entries must look like schema.table.column")
        return [item.lower() for item in value]


def _blank_to_none(value: object) -> object:
    """An empty DSN means "not configured" (the S06 child env may carry an empty value)."""
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, SecretStr) and not value.get_secret_value().strip():
        return None
    return value


def _check_schemas(value: list[str]) -> list[str]:
    if not value:
        raise ValueError("at least one schema must be allowed")
    for item in value:
        if not _SCHEMA.fullmatch(item):
            raise ValueError("schema names must be plain identifiers")
    return [item.lower() for item in value]


class PostgresServerSettings(ServerSettings):
    mcp_postgres_reader_dsn: SecretStr | None = None
    mcp_postgres_writer_dsn: SecretStr | None = None
    mcp_postgres_schemas: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["dw"])
    mcp_postgres_port: int = Field(default=8001, ge=1, le=65535)

    @field_validator("mcp_postgres_reader_dsn", "mcp_postgres_writer_dsn", mode="before")
    @classmethod
    def _blank_dsn(cls, value: object) -> object:
        return _blank_to_none(value)

    @field_validator("mcp_postgres_schemas", mode="before")
    @classmethod
    def _decode_schemas(cls, value: object) -> object:
        return _json_list(value)

    @field_validator("mcp_postgres_schemas")
    @classmethod
    def _schemas(cls, value: list[str]) -> list[str]:
        return _check_schemas(value)


class MySQLServerSettings(ServerSettings):
    mcp_mysql_reader_dsn: SecretStr | None = None
    mcp_mysql_writer_dsn: SecretStr | None = None
    mcp_mysql_schemas: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["sales"])
    mcp_mysql_port: int = Field(default=8002, ge=1, le=65535)

    @field_validator("mcp_mysql_reader_dsn", "mcp_mysql_writer_dsn", mode="before")
    @classmethod
    def _blank_dsn(cls, value: object) -> object:
        return _blank_to_none(value)

    @field_validator("mcp_mysql_schemas", mode="before")
    @classmethod
    def _decode_schemas(cls, value: object) -> object:
        return _json_list(value)

    @field_validator("mcp_mysql_schemas")
    @classmethod
    def _schemas(cls, value: list[str]) -> list[str]:
        return _check_schemas(value)
