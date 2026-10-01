"""Run with ``python -m mcp_servers.mysql``."""

from __future__ import annotations

from mcp_servers.common.app import build_server
from mcp_servers.common.settings import MySQLServerSettings
from mcp_servers.mysql.tools import MySQLTools


def main() -> None:
    settings = MySQLServerSettings()
    server = build_server(MySQLTools(settings))
    server.run("streamable-http", host=settings.mcp_bind_host, port=settings.mcp_mysql_port)


if __name__ == "__main__":
    main()
