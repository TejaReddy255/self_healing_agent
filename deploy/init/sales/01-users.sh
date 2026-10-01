#!/bin/sh
# Sales (source) MySQL users for the MCP server (S07). Runs once, on first start.
set -eu
: "${MCP_MYSQL_READER_PASSWORD:?}" "${MCP_MYSQL_WRITER_PASSWORD:?}"
mysql --protocol=socket -uroot -p"$MYSQL_ROOT_PASSWORD" <<SQL
CREATE USER 'mcp_reader'@'%' IDENTIFIED BY '${MCP_MYSQL_READER_PASSWORD}';
GRANT SELECT ON sales.* TO 'mcp_reader'@'%';
CREATE USER 'mcp_writer'@'%' IDENTIFIED BY '${MCP_MYSQL_WRITER_PASSWORD}';
GRANT SELECT, INSERT, UPDATE, DELETE ON sales.* TO 'mcp_writer'@'%';
FLUSH PRIVILEGES;
SQL
