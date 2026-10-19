-- 008_booking_request.sql
-- The booking, and the two structural guards that make double booking
-- impossible rather than unlikely.
--
-- A booking request is not "linked to" an approval in the loose sense. It
-- REPRODUCES the approval's binding tuple (workflow, patient, payload hash) and
-- the composite foreign key below forces that reproduction to be exact. A
-- booking whose payload hash drifted from the approved one simply has no
-- matching parent row.
--
-- Double booking is then closed off twice over:
--   * UNIQUE (approval_id)   -- one booking per approval, for all time;
--   * consumption is one-way -- the approval is spent by the insert itself.
-- Either alone would do. Both together mean a retry, a race, or a replayed
-- command cannot produce a second booking.

CREATE TABLE booking_request (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Binding columns. Not decoration: they are the FK target tuple.
    approval_id   uuid        NOT NULL,
    workflow_id   uuid        NOT NULL,
    patient_ref   text        NOT NULL,
    payload_hash  text        NOT NULL,

    state         text        NOT NULL DEFAULT 'pending',
    external_ref  text,
    requested_by  text        NOT NULL,
    requested_at  timestamptz NOT NULL DEFAULT now(),
    confirmed_at  timestamptz,
    failed_at     timestamptz,
    last_error    text,
    payload       jsonb       NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT booking_request_state_known
        CHECK (state IN ('pending', 'confirmed', 'failed', 'cancelled')),
    CONSTRAINT booking_request_payload_hash_hex
        CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT booking_request_requested_by_present
        CHECK (length(btrim(requested_by)) > 0),
    CONSTRAINT booking_request_confirmed_has_time
        CHECK (state <> 'confirmed' OR confirmed_at IS NOT NULL),
    CONSTRAINT booking_request_confirmed_has_ref
        CHECK (state <> 'confirmed' OR nullif(btrim(external_ref), '') IS NOT NULL),
    CONSTRAINT booking_request_failed_has_time
        CHECK (state <> 'failed' OR failed_at IS NOT NULL),

    -- THE BINDING. A row can only exist if the approval it names agrees with it
    -- on workflow, patient AND payload hash. MATCH SIMPLE is why every binding
    -- column is NOT NULL: a NULL would silently disable the check.
    CONSTRAINT booking_request_approval_binding_fkey
        FOREIGN KEY (approval_id, workflow_id, patient_ref, payload_hash)
        REFERENCES approval (id, workflow_id, patient_ref, booking_payload_hash)
        ON DELETE RESTRICT ON UPDATE RESTRICT,

    -- One booking per approval, ever. Not "one live booking": an approval is a
    -- single-use capability, so a cancelled booking does not return it.
    CONSTRAINT booking_request_one_per_approval UNIQUE (approval_id)
);

COMMENT ON TABLE booking_request IS
    'A booking attempt authorised by exactly one approval. The composite FK to approval reproduces the consent binding; UNIQUE(approval_id) makes a second booking structurally impossible.';
COMMENT ON COLUMN booking_request.payload_hash IS
    'Must equal approval.booking_payload_hash and the workflow hash the approval was bound to. Enforced by the composite foreign key, not by the caller.';
COMMENT ON COLUMN booking_request.state IS
    'pending -> confirmed | failed | cancelled. Only `confirmed` allows the workflow to reach `booked`.';

CREATE INDEX booking_request_workflow_idx ON booking_request (workflow_id, requested_at DESC);
CREATE UNIQUE INDEX booking_request_external_ref_key
    ON booking_request (external_ref)
    WHERE external_ref IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Consumption: the insert itself spends the approval
-- ---------------------------------------------------------------------------

-- AFTER INSERT so the booking row is already visible to the same transaction;
-- raising here rolls the whole statement back, so there is no window in which
-- a booking exists against a spent approval.
CREATE OR REPLACE FUNCTION booking_request_consume_approval() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_consumed_at timestamptz;
BEGIN
    UPDATE approval a
       SET consumed_at = now(),
           consumed_by = NEW.requested_by
     WHERE a.id          = NEW.approval_id
       AND a.consumed_at IS NULL
       AND a.expires_at  > now()
    RETURNING a.consumed_at INTO v_consumed_at;

    IF v_consumed_at IS NULL THEN
        RAISE EXCEPTION
            'booking refused: approval % is already consumed or has expired', NEW.approval_id
            USING ERRCODE = 'RE004',
                  HINT = 'A spent approval cannot authorise a second write. Capture a fresh approval.';
    END IF;

    RETURN NULL;
END;
$$;

COMMENT ON FUNCTION booking_request_consume_approval IS
    'Consumes the named approval as part of the insert. Fails the insert when the approval was already consumed or has expired.';

CREATE TRIGGER booking_request_consume_trigger
    AFTER INSERT ON booking_request
    FOR EACH ROW EXECUTE FUNCTION booking_request_consume_approval();

-- ---------------------------------------------------------------------------
-- The booking lifecycle: a narrow, explicit state machine
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION booking_request_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- The binding is fixed for the life of the row. Re-pointing a booking at a
    -- different approval is exactly the move that would defeat 007.
    IF NEW.approval_id  <> OLD.approval_id
       OR NEW.workflow_id <> OLD.workflow_id
       OR NEW.patient_ref <> OLD.patient_ref
       OR NEW.payload_hash <> OLD.payload_hash THEN
        RAISE EXCEPTION 'booking_request % cannot be rebound to another approval or payload', OLD.id
            USING ERRCODE = 'RE004';
    END IF;

    IF NEW.state = OLD.state THEN
        RETURN NEW;
    END IF;

    IF NOT (   (OLD.state = 'pending' AND NEW.state IN ('confirmed', 'failed', 'cancelled'))
            OR (OLD.state = 'failed'  AND NEW.state = 'cancelled')) THEN
        RAISE EXCEPTION 'illegal booking_request transition for %: % -> %',
            OLD.id, OLD.state, NEW.state
            USING ERRCODE = 'RE002',
                  HINT = 'pending -> confirmed|failed|cancelled, failed -> cancelled. A confirmed booking is terminal.';
    END IF;

    IF NEW.state = 'confirmed' THEN
        NEW.confirmed_at := coalesce(NEW.confirmed_at, now());
    ELSIF NEW.state = 'failed' THEN
        NEW.failed_at := coalesce(NEW.failed_at, now());
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER booking_request_guard_trigger
    BEFORE UPDATE ON booking_request
    FOR EACH ROW EXECUTE FUNCTION booking_request_guard();

-- ---------------------------------------------------------------------------
-- Convenience: record a booking from a payload hash the caller believes is
-- approved. The FK does the comparing; this only names the operation.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION booking_request_create(
    p_approval_id  uuid,
    p_requested_by text,
    p_external_ref text DEFAULT NULL,
    p_payload      jsonb DEFAULT '{}'::jsonb
) RETURNS booking_request
LANGUAGE plpgsql AS $$
DECLARE
    v_approval approval;
    v_row      booking_request;
BEGIN
    SELECT * INTO v_approval FROM approval a WHERE a.id = p_approval_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'booking refused: approval % does not exist', p_approval_id
            USING ERRCODE = 'RE004';
    END IF;

    INSERT INTO booking_request (
        approval_id, workflow_id, patient_ref, payload_hash,
        requested_by, external_ref, payload
    ) VALUES (
        v_approval.id, v_approval.workflow_id, v_approval.patient_ref,
        v_approval.booking_payload_hash, p_requested_by, p_external_ref, p_payload
    )
    RETURNING * INTO v_row;

    RETURN v_row;
END;
$$;

COMMENT ON FUNCTION booking_request_create IS
    'Records a booking against an approval. The binding columns are copied from the approval itself, so the composite FK cannot be violated by a caller that passes the wrong hash.';
