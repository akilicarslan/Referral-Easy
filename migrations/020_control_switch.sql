-- 020_control_switch.sql
-- Kill switches as rows: scoped, durable, shared by every replica, and audited (WS9 S3, ADR-0051).
--
-- WHY THIS EXISTS
-- The booking kill switch was one in-process boolean. A second gateway replica never heard about it,
-- the registry kept a switch of its own that nothing consulted, and nobody could say afterwards who
-- had pulled it. A kill switch is only a control if every replica obeys it, it fails closed, and it
-- records who pulled it. So:
--
--   * control_switch       -- the CURRENT state: one row per engaged (scope, target). A released
--                             switch has no row. Readers use `control_switch_active`, which also drops
--                             a switch whose `expires_at` has passed.
--   * control_switch_event -- every engage and release, append-only. This is the history an incident
--                             review reads; the current table is only its latest projection.
--
-- Both tables are written ONLY through `control_switch_set()` and `control_switch_release()`. Each
-- call takes the actor from the transaction context (never from an argument), writes the event and
-- the audit row in the same transaction as the state change, and NOTIFYs `control_switch` with the
-- new epoch, so every gateway replica re-reads within its LISTEN loop (or its TTL, if a NOTIFY was
-- lost on a reconnect). A direct INSERT/UPDATE/DELETE is refused with RE009.
--
-- Scope vocabulary (ADR-0051):
--   global         target '*'                 -- everything with a side effect
--   agent_role     target 'scheduling', ...   -- one specialist
--   tool           target 'create_booking'    -- one tool, wherever it is called from
--   workflow_kind  target 'referral', ...     -- one kind of workflow
-- Mode vocabulary:
--   disable     -- the scope does nothing at all
--   read_only   -- reads continue, writes are refused (the fail-closed mode when the store is stale)
--   human_only  -- automation is refused, a human operator may still act
--
-- WS4's dispatch claim reads (global, '*') and (tool, 'create_booking') from here; there is no second
-- booking-halt table.

CREATE SEQUENCE control_switch_epoch_seq;

COMMENT ON SEQUENCE control_switch_epoch_seq IS
    'Monotonic propagation epoch. Every switch change takes the next value; a replica reports the epoch it last applied (/healthz.switch_epoch).';

CREATE TABLE control_switch (
    scope           text        NOT NULL,
    target          text        NOT NULL,
    mode            text        NOT NULL,
    reason          text        NOT NULL,
    engaged_by      text        NOT NULL,
    engaged_by_kind text        NOT NULL,
    engaged_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz,
    epoch           bigint      NOT NULL,

    PRIMARY KEY (scope, target),

    CONSTRAINT control_switch_scope_known
        CHECK (scope IN ('global', 'agent_role', 'tool', 'workflow_kind')),
    CONSTRAINT control_switch_mode_known
        CHECK (mode IN ('disable', 'read_only', 'human_only')),
    -- `global` has exactly one target, so "is everything stopped?" is one primary-key lookup.
    CONSTRAINT control_switch_target_shape
        CHECK ((scope = 'global') = (target = '*')
               AND (target = '*' OR target ~ '^[a-z][a-z0-9_]{0,63}$')),
    CONSTRAINT control_switch_reason_present
        CHECK (length(btrim(reason)) > 0 AND length(reason) <= 500),
    CONSTRAINT control_switch_expiry_after_engage
        CHECK (expires_at IS NULL OR expires_at > engaged_at)
);

COMMENT ON TABLE control_switch IS
    'Engaged kill switches, one row per (scope, target). Written only through control_switch_set/control_switch_release; history is in control_switch_event.';

CREATE TABLE control_switch_event (
    id         bigserial   PRIMARY KEY,
    epoch      bigint      NOT NULL UNIQUE,
    scope      text        NOT NULL,
    target     text        NOT NULL,
    action     text        NOT NULL,
    -- NULL on a release: the switch no longer has a mode.
    mode       text,
    reason     text        NOT NULL,
    actor_kind text        NOT NULL,
    actor_id   text        NOT NULL,
    expires_at timestamptz,
    at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT control_switch_event_action_known
        CHECK (action IN ('engaged', 'released')),
    CONSTRAINT control_switch_event_mode_matches_action
        CHECK ((action = 'engaged') = (mode IS NOT NULL)),
    CONSTRAINT control_switch_event_mode_known
        CHECK (mode IS NULL OR mode IN ('disable', 'read_only', 'human_only'))
);

COMMENT ON TABLE control_switch_event IS
    'Append-only history of every engage and release, with the actor taken from the transaction context.';

CREATE INDEX control_switch_event_target_idx ON control_switch_event (scope, target, at DESC);

CREATE TRIGGER control_switch_event_append_only_trigger
    BEFORE UPDATE OR DELETE ON control_switch_event
    FOR EACH ROW EXECUTE FUNCTION refuse_row_mutation();

CREATE TRIGGER control_switch_event_no_truncate_trigger
    BEFORE TRUNCATE ON control_switch_event
    FOR EACH STATEMENT EXECUTE FUNCTION refuse_row_mutation();

REVOKE UPDATE, DELETE, TRUNCATE ON control_switch_event FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- One writer
-- ---------------------------------------------------------------------------

-- The two functions below open a transaction-local "writer" flag around their own statements. Any
-- other write to either table finds the flag unset and is refused, so the audit row cannot be skipped
-- by writing the table directly.
CREATE OR REPLACE FUNCTION control_switch_refuse_direct_write() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF coalesce(current_setting('referraleasy.control_switch_writer', true), '') <> 'on' THEN
        RAISE EXCEPTION '% on % is refused: switches change only through control_switch_set() and control_switch_release()',
            TG_OP, TG_TABLE_NAME
            USING ERRCODE = 'RE009';
    END IF;
    RETURN coalesce(NEW, OLD);
END;
$$;

CREATE TRIGGER control_switch_one_writer_trigger
    BEFORE INSERT OR UPDATE OR DELETE ON control_switch
    FOR EACH ROW EXECUTE FUNCTION control_switch_refuse_direct_write();

CREATE TRIGGER control_switch_event_one_writer_trigger
    BEFORE INSERT ON control_switch_event
    FOR EACH ROW EXECUTE FUNCTION control_switch_refuse_direct_write();

-- An anonymous change is no change: "who pulled it" is half of what makes this a control.
CREATE OR REPLACE FUNCTION control_switch_actor() RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_actor text := current_actor_id();
BEGIN
    IF v_actor IS NULL OR length(btrim(v_actor)) = 0 THEN
        RAISE EXCEPTION 'a control switch change needs a named actor in referraleasy.actor_id'
            USING ERRCODE = 'RE009';
    END IF;
    RETURN v_actor;
END;
$$;

CREATE OR REPLACE FUNCTION control_switch_set(
    p_scope      text,
    p_target     text,
    p_mode       text,
    p_reason     text,
    p_expires_at timestamptz DEFAULT NULL
) RETURNS control_switch
LANGUAGE plpgsql AS $$
DECLARE
    v_actor text := control_switch_actor();
    v_kind  text := current_actor_kind();
    v_epoch bigint := nextval('control_switch_epoch_seq');
    v_row   control_switch;
BEGIN
    PERFORM set_config('referraleasy.control_switch_writer', 'on', true);

    INSERT INTO control_switch AS s
        (scope, target, mode, reason, engaged_by, engaged_by_kind, engaged_at, expires_at, epoch)
    VALUES
        (p_scope, p_target, p_mode, p_reason, v_actor, v_kind, now(), p_expires_at, v_epoch)
    ON CONFLICT (scope, target) DO UPDATE SET
        mode = EXCLUDED.mode,
        reason = EXCLUDED.reason,
        engaged_by = EXCLUDED.engaged_by,
        engaged_by_kind = EXCLUDED.engaged_by_kind,
        engaged_at = EXCLUDED.engaged_at,
        expires_at = EXCLUDED.expires_at,
        epoch = EXCLUDED.epoch
    RETURNING s.* INTO v_row;

    INSERT INTO control_switch_event
        (epoch, scope, target, action, mode, reason, actor_kind, actor_id, expires_at)
    VALUES
        (v_epoch, p_scope, p_target, 'engaged', p_mode, p_reason, v_kind, v_actor, p_expires_at);

    PERFORM set_config('referraleasy.control_switch_writer', '', true);

    PERFORM audit_record(
        'control.switch',
        'control_switch',
        p_scope || ':' || p_target,
        NULL,
        'allow',
        jsonb_build_object(
            'action', 'engaged', 'mode', p_mode, 'reason', p_reason,
            'expires_at', p_expires_at, 'epoch', v_epoch
        )
    );
    PERFORM pg_notify('control_switch', v_epoch::text);
    RETURN v_row;
END;
$$;

COMMENT ON FUNCTION control_switch_set IS
    'Engages a switch, or changes the mode of one already engaged. Actor from the transaction context; event, audit row and NOTIFY in the same transaction. Returns the current row.';

CREATE OR REPLACE FUNCTION control_switch_release(
    p_scope  text,
    p_target text,
    p_reason text
) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    v_actor text := control_switch_actor();
    v_kind  text := current_actor_kind();
    v_epoch bigint;
BEGIN
    PERFORM set_config('referraleasy.control_switch_writer', 'on', true);

    DELETE FROM control_switch WHERE scope = p_scope AND target = p_target;
    IF NOT FOUND THEN
        -- Releasing a switch that is not engaged changes nothing, so it writes nothing.
        PERFORM set_config('referraleasy.control_switch_writer', '', true);
        RETURN NULL;
    END IF;

    v_epoch := nextval('control_switch_epoch_seq');
    INSERT INTO control_switch_event
        (epoch, scope, target, action, mode, reason, actor_kind, actor_id)
    VALUES
        (v_epoch, p_scope, p_target, 'released', NULL, p_reason, v_kind, v_actor);

    PERFORM set_config('referraleasy.control_switch_writer', '', true);

    PERFORM audit_record(
        'control.switch',
        'control_switch',
        p_scope || ':' || p_target,
        NULL,
        'allow',
        jsonb_build_object('action', 'released', 'reason', p_reason, 'epoch', v_epoch)
    );
    PERFORM pg_notify('control_switch', v_epoch::text);
    RETURN v_epoch;
END;
$$;

COMMENT ON FUNCTION control_switch_release IS
    'Releases an engaged switch and returns the new epoch, or NULL (and writes nothing) when the switch was not engaged.';

-- ---------------------------------------------------------------------------
-- Readers
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW control_switch_active AS
    SELECT *
      FROM control_switch
     WHERE expires_at IS NULL OR expires_at > now();

COMMENT ON VIEW control_switch_active IS
    'The switches in force now. An expired row stays in control_switch until the next change; it is simply not in force.';

-- The epoch a replica compares with its cache. 0 means no switch has ever changed.
CREATE OR REPLACE FUNCTION control_switch_epoch() RETURNS bigint
LANGUAGE sql STABLE AS $$
    SELECT coalesce(max(epoch), 0) FROM control_switch_event
$$;
