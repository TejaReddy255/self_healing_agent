#!/bin/sh
# Defence in depth for the MCP reader (S07 security review): functions that run SQL passed as a
# string read tables the server's validator never sees. The validator allowlists functions; this
# also takes EXECUTE away from PUBLIC, so the MCP roles cannot call them at all. Idempotent.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
DO $$
DECLARE f regprocedure;
BEGIN
  FOR f IN
    SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'pg_catalog'
       AND (p.proname ~ '_to_xml' OR p.proname IN ('ts_stat', 'ts_rewrite', 'xpath', 'xpath_exists'))
  LOOP
    EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC', f);
  END LOOP;
END $$;
SQL
