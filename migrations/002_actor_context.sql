-- 002_actor_context.sql
-- Shared actor-context helpers and the append-only guard used by several tables.
--
-- WHY THIS EXISTS
-- A constraint that depends on "who is writing" must not depend on the writer
-- being polite. The actor identity travels in transaction-local GUCs, and every
-- accessor fails CLOSED: if nothing set the context, the writer is treated as
-- automation, which is the least-privileged actor in this schema.
--
--   SELECT set_config('referraleasy.actor_kind', 'human', true);   -- transaction-local
--   SELECT set_config('referraleasy.actor_id',   'op-42', true);
--
-- Setting the kind to 'human' is a capability: it is only done inside the
-- handoff functions, after the operator has won a compare-and-set on a claim.

CREATE OR REPLACE FUNCTION current_actor_kind() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT coalesce(nullif(current_setting('referraleasy.actor_kind', true), ''), 'automation')
$$;

COMMENT ON FUNCTION current_actor_kind() IS
    'Transaction actor kind: automation|human. Defaults to automation when unset (fail closed).';

CREATE OR REPLACE FUNCTION current_actor_id() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('referraleasy.actor_id', true), '')
$$;

COMMENT ON FUNCTION current_actor_id() IS
    'Transaction actor identifier (worker id, operator id, service name), or NULL when anonymous.';

-- Shared append-only guard. Attached as a BEFORE UPDATE OR DELETE trigger to
-- tables whose rows are evidence of something that already happened: rewriting
-- one of those rows would rewrite history.
CREATE OR REPLACE FUNCTION refuse_row_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% on table % is refused: the table is append-only',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'RE008',
              HINT = 'Write a new row instead; corrections are new facts, not edits of old ones.';
END;
$$;

COMMENT ON FUNCTION refuse_row_mutation() IS
    'BEFORE UPDATE OR DELETE trigger function: raises RE008. Append-only tables accept INSERT only.';
