-- Read-only role for the conversational analytics agent (/ask).
-- Idempotent: safe to run on a fresh volume (auto-run by docker-entrypoint-initdb.d)
-- AND to apply once by hand to an already-populated database.
--
-- NOTE: this file ships with a placeholder password. After applying, set the real
-- password (kept in .env as AGENT_DB_PASSWORD) with:
--   ALTER ROLE agent_ro PASSWORD '<AGENT_DB_PASSWORD>';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_ro') THEN
        CREATE ROLE agent_ro LOGIN PASSWORD 'change_me_via_alter_role';
    END IF;
END
$$;

-- Connect + read-only access to the analytics objects only. No DDL, no write.
GRANT CONNECT ON DATABASE house_prices TO agent_ro;
GRANT USAGE ON SCHEMA public TO agent_ro;
GRANT SELECT ON transactions, postcodes, market_transactions, monthly_price_stats TO agent_ro;

-- get_data_freshness() is read-only; the matview refresh function must stay ungranted.
GRANT EXECUTE ON FUNCTION get_data_freshness() TO agent_ro;
REVOKE EXECUTE ON FUNCTION refresh_monthly_stats() FROM agent_ro;

-- Stop any future tables in public from auto-granting to the agent.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM agent_ro;

-- Per-role guardrails. default_transaction_read_only is the real guarantee: even a
-- validation bypass in run_sql cannot write. statement_timeout kills runaway scans.
ALTER ROLE agent_ro SET default_transaction_read_only = on;
ALTER ROLE agent_ro SET statement_timeout = '5s';
ALTER ROLE agent_ro SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE agent_ro SET lock_timeout = '2s';
ALTER ROLE agent_ro SET search_path = public;
