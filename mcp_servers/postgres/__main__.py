"""Run with ``python -m mcp_servers.postgres``."""

from __future__ import annotations

from mcp_servers.common.app import build_server
from mcp_servers.common.settings import PostgresServerSettings
from mcp_servers.postgres.tools import PostgresTools


def main() -> None:
    settings = PostgresServerSettings()
    server = build_server(PostgresTools(settings))
    server.run("streamable-http", host=settings.mcp_bind_host, port=settings.mcp_postgres_port)


if __name__ == "__main__":
    main()
