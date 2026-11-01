-- =============================================================================
-- One-time PostgreSQL setup for the RAG service (IMPLEMENTATION_PLAN.md §3.5).
--
-- Run as a superuser with psql, passing the two passwords as variables:
--   psql -U postgres -h 127.0.0.1 -v owner_pw=... -v app_pw=... -f scripts/db/init_local.sql
-- (`uv run poe db-init` does this for you using the passwords in .env.)
--
-- Idempotent: safe to re-run. Existing roles get their passwords re-synced with .env.
--
-- Roles (least privilege):
--   rag_owner  owns the schema and runs migrations (DDL)
--   rag_app    the running service: read/write rows only, can't change the schema
-- =============================================================================
\set ON_ERROR_STOP on

-- Roles. CREATE ROLE has no IF NOT EXISTS; \gexec runs the generated statement
-- only when the SELECT returns a row.
SELECT format('CREATE ROLE rag_owner LOGIN PASSWORD %L', :'owner_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_owner') \gexec
SELECT format('CREATE ROLE rag_app LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'app_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app') \gexec
SELECT format('ALTER ROLE rag_owner PASSWORD %L', :'owner_pw') \gexec
SELECT format('ALTER ROLE rag_app PASSWORD %L', :'app_pw') \gexec

-- Databases (CREATE DATABASE can't run inside a transaction or a DO block).
SELECT 'CREATE DATABASE rag OWNER rag_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'rag') \gexec
SELECT 'CREATE DATABASE rag_test OWNER rag_owner'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'rag_test') \gexec

-- Privileges, repeated per database. In PG 15+ the public schema belongs to the
-- database owner (rag_owner), and ordinary users can't create objects in it.
\connect rag
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO rag_app;
-- Tables and sequences that rag_owner creates later (via migrations) are automatically
-- readable/writable by rag_app.
ALTER DEFAULT PRIVILEGES FOR ROLE rag_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rag_app;
ALTER DEFAULT PRIVILEGES FOR ROLE rag_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO rag_app;
ALTER DEFAULT PRIVILEGES FOR ROLE rag_owner IN SCHEMA public
    GRANT EXECUTE ON FUNCTIONS TO rag_app;
-- Cover objects that already exist (re-runs after migrations).
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rag_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rag_app;

\connect rag_test
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO rag_app;
ALTER DEFAULT PRIVILEGES FOR ROLE rag_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO rag_app;
ALTER DEFAULT PRIVILEGES FOR ROLE rag_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO rag_app;
ALTER DEFAULT PRIVILEGES FOR ROLE rag_owner IN SCHEMA public
    GRANT EXECUTE ON FUNCTIONS TO rag_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rag_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rag_app;
