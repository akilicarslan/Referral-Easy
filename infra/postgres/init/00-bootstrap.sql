-- 00-bootstrap.sql
-- Runs once, as the superuser, the first time the PostgreSQL data directory is
-- initialised. This is NOT a migration: it configures the server, and it must
-- stay valid on a database where no ReferralEasy table exists yet.
--
-- Schema objects live in ../../migrations/, applied by scripts/migrate.py. Keep
-- the split: if it would be different on a restored production dump, it belongs
-- in a numbered migration, not here.

-- ---------------------------------------------------------------------------
-- Time
-- ---------------------------------------------------------------------------
-- Everything is stored as timestamptz, so the server timezone is a display
-- detail -- but a display detail that shows up in every log line during an
-- incident. UTC makes those logs comparable without mental arithmetic.
ALTER DATABASE :"DBNAME" SET timezone = 'UTC';

-- Lease and expiry arithmetic is interval-based; a consistent intervalstyle
-- keeps dumps and logs readable in the same way everywhere.
ALTER DATABASE :"DBNAME" SET intervalstyle = 'iso_8601';

-- ---------------------------------------------------------------------------
-- Session defaults
-- ---------------------------------------------------------------------------
-- A statement that never returns is an outage that never ends. The guards in
-- migrations/ are all short single-update statements, so nothing legitimate
-- approaches these ceilings.
ALTER DATABASE :"DBNAME" SET statement_timeout = '30s';
ALTER DATABASE :"DBNAME" SET lock_timeout = '10s';

-- The migration runner holds a transaction per file; a couple of the DDL files
-- take a few hundred milliseconds. Well inside the default.
ALTER DATABASE :"DBNAME" SET idle_in_transaction_session_timeout = '60s';

-- ---------------------------------------------------------------------------
-- Digest functions
-- ---------------------------------------------------------------------------
-- The schema deliberately uses only the core `sha256(bytea)` function (see
-- 013_audit.sql), so no extension is required for it to work. pgcrypto is
-- enabled anyway because it is the natural home for any future digest work and
-- its absence is a confusing failure mode to debug later.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- Note on roles and passwords
-- ---------------------------------------------------------------------------
-- No role or password is created here. Credentials belong in the environment
-- (see .env.example), not in a file that gets committed. The compose service
-- creates the application database and user from POSTGRES_USER /
-- POSTGRES_PASSWORD / POSTGRES_DB before this script runs.
