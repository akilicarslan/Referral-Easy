-- 009_command_dedup.sql
-- Command idempotency: one command_id, one outcome.
--
-- The contract:
--   same command_id + same request hash    -> replay the stored response
--   same command_id + DIFFERENT request hash -> REJECT
--
-- The second branch is the one that matters. Retrying a request is safe;
-- silently executing a *different* request under a reused id is how a client
-- bug turns into a double booking. A plain "upsert on conflict do nothing"
-- would quietly keep the first response and hand it back for the second,
-- different, request -- so the hash is compared explicitly and a mismatch is a
-- hard error (RE005).
--
-- The stored hash is also immutable: rewriting it would retroactively bless
-- the second request.

CREATE TABLE command_dedup (
    command_id      text        PRIMARY KEY,
    request_hash    text        NOT NULL,
    status          text        NOT NULL DEFAULT 'in_progress',
    response_status integer,
    response        jsonb,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz,
    expires_at      timestamptz,

    CONSTRAINT command_dedup_id_present
        CHECK (length(btrim(command_id)) > 0),
    CONSTRAINT command_dedup_request_hash_hex
        CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT command_dedup_status_known
        CHECK (status IN ('in_progress', 'completed', 'failed')),
    CONSTRAINT command_dedup_completion_is_whole
        CHECK ((status = 'in_progress') = (completed_at IS NULL)),
    CONSTRAINT command_dedup_completed_has_response
        CHECK (status <> 'completed' OR response_status IS NOT NULL)
);

COMMENT ON TABLE command_dedup IS
    'Deduplication ledger for inbound commands. PRIMARY KEY(command_id) plus the stored request_hash make "same id, different request" detectable and rejectable.';
COMMENT ON COLUMN command_dedup.request_hash IS
    'sha256 hex of the canonical request body. Immutable: the trigger below refuses any rewrite.';
COMMENT ON COLUMN command_dedup.response IS
    'The stored response replayed for an identical retry. Never replayed for a different request hash.';
COMMENT ON COLUMN command_dedup.expires_at IS
    'Optional retention boundary. Expired rows are prunable; they are never resurrected as fresh commands.';

CREATE INDEX command_dedup_in_progress_idx
    ON command_dedup (created_at)
    WHERE status = 'in_progress';

CREATE INDEX command_dedup_expiry_idx
    ON command_dedup (expires_at)
    WHERE expires_at IS NOT NULL;

-- The stored request hash and the command identity never change.
CREATE OR REPLACE FUNCTION command_dedup_no_rewrite() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.command_id <> OLD.command_id THEN
        RAISE EXCEPTION 'command_dedup row for % cannot be renumbered to %', OLD.command_id, NEW.command_id
            USING ERRCODE = 'RE005';
    END IF;

    IF NEW.request_hash <> OLD.request_hash THEN
        RAISE EXCEPTION 'command % request_hash cannot be rewritten (stored %, supplied %)',
            OLD.command_id, OLD.request_hash, NEW.request_hash
            USING ERRCODE = 'RE005',
                  HINT = 'A changed hash is a different command and needs a different command_id.';
    END IF;

    IF OLD.status <> 'in_progress' AND NEW.status = 'in_progress' THEN
        RAISE EXCEPTION 'command % cannot return to in_progress (was %)', OLD.command_id, OLD.status
            USING ERRCODE = 'RE005';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER command_dedup_no_rewrite_trigger
    BEFORE UPDATE ON command_dedup
    FOR EACH ROW EXECUTE FUNCTION command_dedup_no_rewrite();

-- ---------------------------------------------------------------------------
-- Operations
-- ---------------------------------------------------------------------------

-- Begin a command. Returns the ledger row after this call, which is either the
-- row this call created (status = in_progress) or the pre-existing one
-- (replayed, in_progress, or failed). Never a second row for one command_id.
CREATE OR REPLACE FUNCTION command_begin(
    p_command_id   text,
    p_request_hash text
) RETURNS command_dedup
LANGUAGE plpgsql AS $$
DECLARE
    v_row command_dedup;
BEGIN
    INSERT INTO command_dedup (command_id, request_hash)
    VALUES (p_command_id, p_request_hash)
    ON CONFLICT (command_id) DO NOTHING;

    SELECT * INTO v_row FROM command_dedup c WHERE c.command_id = p_command_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'command % could neither be inserted nor read back', p_command_id
            USING ERRCODE = 'RE005';
    END IF;

    IF v_row.request_hash <> p_request_hash THEN
        RAISE EXCEPTION
            'command % was first seen with a different request hash (stored %, supplied %)',
            p_command_id, v_row.request_hash, p_request_hash
            USING ERRCODE = 'RE005',
                  HINT = 'Reusing a command_id for a different request body is rejected, not coerced.';
    END IF;

    RETURN v_row;
END;
$$;

COMMENT ON FUNCTION command_begin IS
    'Idempotent command entry. Creates the ledger row or returns the existing one; raises RE005 when the same command_id arrives with a different request hash.';

-- Finish a command and store the response that later retries will replay.
CREATE OR REPLACE FUNCTION command_complete(
    p_command_id      text,
    p_request_hash    text,
    p_response_status integer,
    p_response        jsonb,
    p_status          text DEFAULT 'completed'
) RETURNS command_dedup
LANGUAGE plpgsql AS $$
DECLARE
    v_row command_dedup;
BEGIN
    IF p_status NOT IN ('completed', 'failed') THEN
        RAISE EXCEPTION 'command_complete status must be completed or failed, got %', p_status
            USING ERRCODE = 'RE005';
    END IF;

    UPDATE command_dedup c
       SET status          = p_status,
           response_status = p_response_status,
           response        = p_response,
           completed_at    = now()
     WHERE c.command_id   = p_command_id
       AND c.request_hash = p_request_hash
       AND c.status       = 'in_progress'
    RETURNING * INTO v_row;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'command % cannot be completed: unknown, request hash mismatch, or already terminal',
            p_command_id
            USING ERRCODE = 'RE005';
    END IF;

    RETURN v_row;
END;
$$;

COMMENT ON FUNCTION command_complete IS
    'Marks a command terminal and stores its response. The WHERE clause re-asserts the request hash, so a completion can never land on a different command''s row.';

-- Read path. Same rejection rule as command_begin, so a caller that skips
-- command_begin cannot read another request's response by guessing an id.
CREATE OR REPLACE FUNCTION command_replay(
    p_command_id   text,
    p_request_hash text
) RETURNS command_dedup
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_row command_dedup;
BEGIN
    SELECT * INTO v_row FROM command_dedup c WHERE c.command_id = p_command_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'command % not found', p_command_id
            USING ERRCODE = 'RE005';
    END IF;

    IF v_row.request_hash <> p_request_hash THEN
        RAISE EXCEPTION
            'command % request hash mismatch: stored %, supplied %',
            p_command_id, v_row.request_hash, p_request_hash
            USING ERRCODE = 'RE005';
    END IF;

    RETURN v_row;
END;
$$;

COMMENT ON FUNCTION command_replay IS
    'Returns the ledger row for a retry, refusing when the request hash differs. Only complete rows carry a replayable response.';
