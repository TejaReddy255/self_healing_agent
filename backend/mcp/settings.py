"""MCP server and registry settings (S06)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.config import ConfigurationError, load_or_fail
from backend.domain.common import SystemName

DEFAULT_PERMISSIONS_FILE = Path(__file__).resolve().parents[2] / "config" / "tool_permissions.yaml"
# Autostart may only launch our own server packages: never an arbitrary command.
_MODULE = re.compile(r"^mcp_servers\.[a-z][a-z0-9_]*$")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _check_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("MCP server URLs must be http(s) URLs with a host")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("MCP server URLs must not carry credentials, a query or a fragment")
    return value.strip()


def _check_module(value: str) -> str:
    if not _MODULE.fullmatch(value):
        raise ValueError("MCP autostart modules must look like 'mcp_servers.<name>'")
    return value


@dataclass(frozen=True)
class ServerConfig:
    name: SystemName
    url: str
    enabled: bool
    required: bool
    module: str

    @property
    def health_url(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}/health"

    @property
    def url_alias(self) -> str:
        """host:port only, safe to show in status pages."""
        return urlsplit(self.url).netloc

    @property
    def loopback(self) -> bool:
        return (urlsplit(self.url).hostname or "") in LOOPBACK_HOSTS


class MCPSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    mcp_postgresql_url: str = "http://127.0.0.1:8001/mcp"
    mcp_mysql_url: str = "http://127.0.0.1:8002/mcp"
    mcp_s3_url: str = "http://127.0.0.1:8003/mcp"
    mcp_mongodb_url: str = "http://127.0.0.1:8004/mcp"
    mcp_postgresql_enabled: bool = True
    mcp_mysql_enabled: bool = True
    mcp_s3_enabled: bool = True
    mcp_mongodb_enabled: bool = True
    mcp_postgresql_required: bool = False
    mcp_mysql_required: bool = False
    mcp_s3_required: bool = False
    mcp_mongodb_required: bool = False
    mcp_postgresql_module: str = "mcp_servers.postgres"
    mcp_mysql_module: str = "mcp_servers.mysql"
    mcp_s3_module: str = "mcp_servers.s3"
    mcp_mongodb_module: str = "mcp_servers.mongodb"

    mcp_autostart: bool = False
    mcp_default_timeout_s: float = Field(default=60.0, gt=0, le=600)
    mcp_max_output_bytes: int = Field(default=8192, ge=256, le=1_048_576)
    mcp_probe_timeout_s: float = Field(default=3.0, gt=0, le=30)
    mcp_refresh_interval_s: float = Field(default=60.0, ge=0, le=86_400)
    mcp_read_retries: int = Field(default=2, ge=0, le=5)
    tool_permissions_file: Path = DEFAULT_PERMISSIONS_FILE

    @field_validator("mcp_postgresql_url", "mcp_mysql_url", "mcp_s3_url", "mcp_mongodb_url")
    @classmethod
    def _urls(cls, value: str) -> str:
        return _check_url(value)

    @field_validator(
        "mcp_postgresql_module", "mcp_mysql_module", "mcp_s3_module", "mcp_mongodb_module"
    )
    @classmethod
    def _modules(cls, value: str) -> str:
        return _check_module(value)

    def servers(self) -> dict[SystemName, ServerConfig]:
        def config(name: SystemName) -> ServerConfig:
            key = name.value
            return ServerConfig(
                name=name,
                url=getattr(self, f"mcp_{key}_url"),
                enabled=getattr(self, f"mcp_{key}_enabled"),
                required=getattr(self, f"mcp_{key}_required"),
                module=getattr(self, f"mcp_{key}_module"),
            )

        return {name: config(name) for name in SystemName}

    def check_for_env(self, env: str) -> None:
        if env == "prod" and self.mcp_autostart:
            raise ConfigurationError("MCP_AUTOSTART must not be enabled when ENV=prod")


def load_mcp_settings() -> MCPSettings:
    return load_or_fail(MCPSettings)
