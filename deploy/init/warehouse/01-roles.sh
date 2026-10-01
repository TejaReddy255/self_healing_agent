#!/bin/sh
# Warehouse (target) PostgreSQL roles for the MCP server (S07). Runs once, on first start.
#   dw_owner   - owns dw objects; never logs in.
#   mcp_reader - SELECT only; every session read-only.
#   mcp_writer - DML on dw; may SET ROLE dw_owner only for allowlisted DDL (partitions, indexes).
set -eu
: "${MCP_POSTGRES_READER_PASSWORD:?}" "${MCP_POSTGRES_WRITER_PASSWORD:?}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v reader_pw="$MCP_POSTGRES_READER_PASSWORD" -v writer_pw="$MCP_POSTGRES_WRITER_PASSWORD" <<'SQL'
CREATE ROLE dw_owner NOLOGIN;
CREATE SCHEMA dw AUTHORIZATION dw_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA dw FROM PUBLIC;

CREATE ROLE mcp_reader LOGIN PASSWORD :'reader_pw';
ALTER ROLE mcp_reader SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE warehouse TO mcp_reader;
GRANT USAGE ON SCHEMA dw TO mcp_reader;

CREATE ROLE mcp_writer LOGIN NOINHERIT PASSWORD :'writer_pw';
GRANT CONNECT ON DATABASE warehouse TO mcp_writer;
GRANT USAGE ON SCHEMA dw TO mcp_writer;
GRANT dw_owner TO mcp_writer;  -- NOINHERIT: only via SET ROLE, used by execute_ddl alone

ALTER DEFAULT PRIVILEGES FOR ROLE dw_owner IN SCHEMA dw GRANT SELECT ON TABLES TO mcp_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE dw_owner IN SCHEMA dw
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mcp_writer;
ALTER DEFAULT PRIVILEGES FOR ROLE dw_owner IN SCHEMA dw GRANT USAGE ON SEQUENCES TO mcp_writer;
SQL
