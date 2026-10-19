-- 001_schema_migrations.sql
-- Bookkeeping for the forward-only migration runner.
--
-- This file is idempotent on purpose: the runner bootstraps the same table
-- before it can read it, and a hand-run `psql -1 -f 001_...` must not fail on
-- a database where the runner already created it.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer     PRIMARY KEY,
    name        text        NOT NULL,
    filename    text        NOT NULL,
    checksum    text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    duration_ms integer     NOT NULL DEFAULT 0
);

COMMENT ON TABLE schema_migrations IS
    'Applied migration ledger. A migration is never re-run; a changed checksum for an applied version is a hard error (forward-only).';
