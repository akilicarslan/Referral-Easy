-- 013_audit.sql
-- The audit log, and why it is here rather than in OpenSearch.
--
-- Telemetry and audit are different questions. Telemetry asks "how is it
-- performing"; it is sampled, best-effort, and may be dropped under load. Audit
-- asks "what did this system do, and on whose behalf"; it must be complete, and
-- it must survive the telemetry pipeline being down, misconfigured or rebuilt.
-- A trace index that can be reindexed cannot be the audit of record. So audit
-- lives in the system of record, in PostgreSQL, as rows that are append-only
-- and chained.
--
-- Entry hash chain: each row stores the hash of the previous row, so removing
-- or reordering an entry breaks every subsequent link and is detectable by
-- audit_verify_chain(). Appending a transaction that never happened is still
-- possible for someone with write access -- a hash chain is tamper-EVIDENT,
-- not tamper-proof, and it would be dishonest to claim otherwise.

CREATE TABLE audit (
    seq         bigserial   PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT now(),

    actor_kind  text        NOT NULL,
    actor_id    text,

    action      text        NOT NULL,
    entity_type text        NOT NULL,
    entity_id   text,
    workflow_id uuid        REFERENCES workflow (id) ON DELETE RESTRICT,

    outcome     text        NOT NULL,
    detail      jsonb       NOT NULL DEFAULT '{}'::jsonb,

    prev_hash   text        NOT NULL,
    entry_hash  text        NOT NULL,

    CONSTRAINT audit_actor_kind_known
        CHECK (actor_kind IN ('automation', 'human', 'system')),
    CONSTRAINT audit_action_present
        CHECK (length(btrim(action)) > 0),
    CONSTRAINT audit_entity_type_present
        CHECK (length(btrim(entity_type)) > 0),
    CONSTRAINT audit_outcome_known
        CHECK (outcome IN ('allow', 'deny', 'error')),
    CONSTRAINT audit_hashes_hex
        CHECK (prev_hash ~ '^[0-9a-f]{64}$' AND entry_hash ~ '^[0-9a-f]{64}$')
);

COMMENT ON TABLE audit IS
    'Append-only audit of record, in PostgreSQL. Telemetry goes to OpenSearch; this table is never derived from it and never replaced by it.';
COMMENT ON COLUMN audit.actor_kind IS
    'Taken from the transaction actor context at insert time (fail-closed default: automation).';
COMMENT ON COLUMN audit.entry_hash IS
    'sha256 over this row''s content and the previous entry_hash. Rewriting history breaks every later link.';

-- The chain cannot fork: two rows cannot claim the same predecessor.
CREATE UNIQUE INDEX audit_prev_hash_key ON audit (prev_hash);

CREATE INDEX audit_workflow_idx  ON audit (workflow_id, occurred_at DESC);
CREATE INDEX audit_entity_idx    ON audit (entity_type, entity_id, occurred_at DESC);
CREATE INDEX audit_action_idx    ON audit (action, occurred_at DESC);

CREATE TRIGGER audit_append_only_trigger
    BEFORE UPDATE OR DELETE ON audit
    FOR EACH ROW EXECUTE FUNCTION refuse_row_mutation();

CREATE TRIGGER audit_no_truncate_trigger
    BEFORE TRUNCATE ON audit
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_row_mutation();

REVOKE UPDATE, DELETE, TRUNCATE ON audit FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- Chain maintenance
-- ---------------------------------------------------------------------------

-- Canonical string a row hashes to. Kept in one function so verification and
-- insertion can never disagree about what was hashed.
CREATE OR REPLACE FUNCTION audit_entry_canonical(
    p_prev_hash   text,
    p_occurred_at timestamptz,
    p_actor_kind  text,
    p_actor_id    text,
    p_action      text,
    p_entity_type text,
    p_entity_id   text,
    p_workflow_id uuid,
    p_outcome     text,
    p_detail      jsonb
) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT concat_ws('|',
        p_prev_hash,
        to_char(p_occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        p_actor_kind,
        coalesce(p_actor_id, ''),
        p_action,
        p_entity_type,
        coalesce(p_entity_id, ''),
        coalesce(p_workflow_id::text, ''),
        p_outcome,
        p_detail::text
    )
$$;

CREATE OR REPLACE FUNCTION audit_link() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_prev text;
BEGIN
    -- Serialise appenders so the chain has exactly one tail. Taken last, inside
    -- the same statement, to keep the lock ordering one-way.
    PERFORM pg_advisory_xact_lock(hashtext('referraleasy.audit.chain'));

    SELECT a.entry_hash INTO v_prev
      FROM audit a
     ORDER BY a.seq DESC
     LIMIT 1;

    -- Genesis link.
    v_prev := coalesce(v_prev, repeat('0', 64));

    NEW.actor_kind  := coalesce(nullif(NEW.actor_kind, ''), current_actor_kind());
    NEW.actor_id    := coalesce(NEW.actor_id, current_actor_id());
    NEW.prev_hash   := v_prev;
    NEW.entry_hash  := encode(sha256(convert_to(audit_entry_canonical(
        v_prev,
        NEW.occurred_at,
        NEW.actor_kind,
        NEW.actor_id,
        NEW.action,
        NEW.entity_type,
        NEW.entity_id,
        NEW.workflow_id,
        NEW.outcome,
        NEW.detail
    ), 'UTF8')), 'hex');

    RETURN NEW;
END;
$$;

CREATE TRIGGER audit_link_trigger
    BEFORE INSERT ON audit
    FOR EACH ROW EXECUTE FUNCTION audit_link();

-- Walk the chain and report the first broken link. Returns one row per problem;
-- an empty result means the recorded history is internally consistent.
CREATE OR REPLACE FUNCTION audit_verify_chain()
RETURNS TABLE (seq bigint, problem text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_expected_prev text := repeat('0', 64);
    v_row           record;
    v_recomputed    text;
BEGIN
    FOR v_row IN SELECT * FROM audit a ORDER BY a.seq LOOP
        IF v_row.prev_hash <> v_expected_prev THEN
            seq := v_row.seq;
            problem := format('prev_hash %s does not match the preceding entry_hash %s',
                              v_row.prev_hash, v_expected_prev);
            RETURN NEXT;
        END IF;

        v_recomputed := encode(sha256(convert_to(audit_entry_canonical(
            v_row.prev_hash, v_row.occurred_at, v_row.actor_kind, v_row.actor_id,
            v_row.action, v_row.entity_type, v_row.entity_id, v_row.workflow_id,
            v_row.outcome, v_row.detail
        ), 'UTF8')), 'hex');

        IF v_recomputed <> v_row.entry_hash THEN
            seq := v_row.seq;
            problem := 'entry_hash does not match the row content';
            RETURN NEXT;
        END IF;

        v_expected_prev := v_row.entry_hash;
    END LOOP;
END;
$$;

COMMENT ON FUNCTION audit_verify_chain IS
    'Returns one row per broken link (empty = chain intact). Detects edits, deletions and reordering; it cannot detect a well-formed append by an attacker with write access.';

-- ---------------------------------------------------------------------------
-- Recording helper
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit_record(
    p_action      text,
    p_entity_type text,
    p_entity_id   text DEFAULT NULL,
    p_workflow_id uuid DEFAULT NULL,
    p_outcome     text DEFAULT 'allow',
    p_detail      jsonb DEFAULT '{}'::jsonb
) RETURNS audit
LANGUAGE plpgsql AS $$
DECLARE
    v_row audit;
BEGIN
    INSERT INTO audit (actor_kind, action, entity_type, entity_id, workflow_id, outcome, detail)
    VALUES (current_actor_kind(), p_action, p_entity_type, p_entity_id, p_workflow_id, p_outcome, p_detail)
    RETURNING * INTO v_row;

    RETURN v_row;
END;
$$;

COMMENT ON FUNCTION audit_record IS
    'Appends one audit entry. Actor identity comes from the transaction context, never from an argument, so a caller cannot attribute its own act to somebody else.';

-- NOTE ON DENIALS: a refused write aborts its transaction (that is the point of
-- RAISE EXCEPTION), so the denial cannot be logged in the same transaction. A
-- gateway that wants denials in the audit trail writes outcome='deny' in a
-- separate transaction after catching the error. The schema supports it; it
-- does not pretend to do it automatically.

-- ---------------------------------------------------------------------------
-- Automatic entries for the transitions that matter most
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION audit_workflow_state_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM audit_record(
        'workflow.transition',
        'workflow',
        NEW.id::text,
        NEW.id,
        'allow',
        jsonb_build_object(
            'ticket_ref',    NEW.ticket_ref,
            'from_state',    OLD.state,
            'to_state',      NEW.state,
            'from_version',  OLD.state_version,
            'to_version',    NEW.state_version
        )
    );
    RETURN NULL;
END;
$$;

CREATE TRIGGER audit_workflow_state_change_trigger
    AFTER UPDATE ON workflow
    FOR EACH ROW
    WHEN (OLD.state IS DISTINCT FROM NEW.state)
    EXECUTE FUNCTION audit_workflow_state_change();

CREATE OR REPLACE FUNCTION audit_approval_consumed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM audit_record(
        'approval.consumed',
        'approval',
        NEW.id::text,
        NEW.workflow_id,
        'allow',
        jsonb_build_object(
            'patient_ref', NEW.patient_ref,
            'state_version', NEW.state_version,
            'booking_payload_hash', NEW.booking_payload_hash,
            'consumed_by', NEW.consumed_by
        )
    );
    RETURN NULL;
END;
$$;

CREATE TRIGGER audit_approval_consumed_trigger
    AFTER UPDATE ON approval
    FOR EACH ROW
    WHEN (OLD.consumed_at IS NULL AND NEW.consumed_at IS NOT NULL)
    EXECUTE FUNCTION audit_approval_consumed();

CREATE OR REPLACE FUNCTION audit_booking_requested() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM audit_record(
        'booking.requested',
        'booking_request',
        NEW.id::text,
        NEW.workflow_id,
        'allow',
        jsonb_build_object(
            'approval_id', NEW.approval_id,
            'patient_ref', NEW.patient_ref,
            'payload_hash', NEW.payload_hash,
            'requested_by', NEW.requested_by
        )
    );
    RETURN NULL;
END;
$$;

CREATE TRIGGER audit_booking_requested_trigger
    AFTER INSERT ON booking_request
    FOR EACH ROW EXECUTE FUNCTION audit_booking_requested();

CREATE OR REPLACE FUNCTION audit_handoff_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM audit_record(
        'handoff.' || NEW.state,
        'handoff',
        NEW.id::text,
        NEW.workflow_id,
        'allow',
        jsonb_build_object(
            'ticket_ref', NEW.ticket_ref,
            'from_state', OLD.state,
            'to_state', NEW.state,
            'claim_generation', NEW.claim_generation,
            'claimed_by', NEW.claimed_by,
            'resolution', NEW.resolution
        )
    );
    RETURN NULL;
END;
$$;

CREATE TRIGGER audit_handoff_change_trigger
    AFTER UPDATE ON handoff
    FOR EACH ROW
    WHEN (OLD.state IS DISTINCT FROM NEW.state)
    EXECUTE FUNCTION audit_handoff_change();
