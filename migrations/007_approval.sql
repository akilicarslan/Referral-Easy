-- 007_approval.sql
-- Patient approval (booking consent) and its binding.
--
-- THE INVARIANT
-- An approval is valid only when it is bound to:
--     patient + workflow + state_version + booking payload hash + expiry
-- and "any data change invalidates the approval" must be STRUCTURAL, not a
-- convention that the application remembers to apply.
--
-- HOW THIS FILE MAKES IT STRUCTURAL
--   1. The binding is a UNIQUE key, not five unrelated columns. A child row
--      that disagrees with the approval on any binding column does not match
--      the key, so the composite foreign key in 008 cannot be satisfied. The
--      database, not the handler, compares the hashes.
--   2. A transition into the booking flow is only reachable when a live,
--      unconsumed, unexpired approval matches the workflow's CURRENT payload
--      hash and CURRENT state_version. Change the payload -> the hash moves ->
--      the approval no longer matches. Move the state -> the version moves ->
--      the approval no longer matches.
--   3. Consumption is a one-way transition enforced by trigger. A consumed
--      approval cannot be un-consumed, and 008 makes a second booking against
--      the same approval impossible outright.
--   4. An approval is evidence of PATIENT consent, so it cannot be created
--      inside an operator session (see the "human" check below). That is what
--      keeps the approval model out of reach of an operator resolving a ticket.

CREATE TABLE approval (
    id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id          uuid        NOT NULL REFERENCES workflow (id) ON DELETE RESTRICT,
    patient_ref          text        NOT NULL,
    evidence_id          uuid        NOT NULL REFERENCES evidence (id) ON DELETE RESTRICT,
    state_version        integer     NOT NULL,
    booking_payload_hash text        NOT NULL,
    channel              text        NOT NULL,
    approved_by          text        NOT NULL,
    approved_at          timestamptz NOT NULL DEFAULT now(),
    expires_at           timestamptz NOT NULL,
    consumed_at          timestamptz,
    consumed_by          text,

    CONSTRAINT approval_patient_present
        CHECK (length(btrim(patient_ref)) > 0),
    CONSTRAINT approval_state_version_positive
        CHECK (state_version >= 1),
    CONSTRAINT approval_payload_hash_hex
        CHECK (booking_payload_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT approval_approved_by_present
        CHECK (length(btrim(approved_by)) > 0),
    CONSTRAINT approval_channel_known
        CHECK (channel IN ('patient_self_service', 'patient_portal', 'consent_capture')),
    CONSTRAINT approval_expiry_after_approval
        CHECK (expires_at > approved_at),
    -- Consumption is all-or-nothing: no half-consumed approval.
    CONSTRAINT approval_consumption_is_whole
        CHECK ((consumed_at IS NULL) = (consumed_by IS NULL)),

    -- THE BINDING KEY. Every child that consumes this approval must reproduce
    -- patient_ref, workflow_id and booking_payload_hash exactly, because the
    -- composite FK in 008 points at this tuple.
    CONSTRAINT approval_binding_key
        UNIQUE (id, workflow_id, patient_ref, booking_payload_hash)
);

COMMENT ON TABLE approval IS
    'Patient approval to book. Valid only within its binding tuple (patient, workflow, state_version, payload hash, expiry). Consumed at most once, ever.';
COMMENT ON COLUMN approval.state_version IS
    'The workflow state_version the patient approved against. Any later transition moves the workflow past this version and invalidates the approval by construction.';
COMMENT ON COLUMN approval.booking_payload_hash IS
    'sha256 of the canonical booking payload, hex. Change one field of what will be booked and this no longer matches the workflow''s current hash.';
COMMENT ON COLUMN approval.consumed_at IS
    'Set exactly once, when a booking request is recorded against this approval. Immutable afterwards.';
COMMENT ON COLUMN approval.evidence_id IS
    'The consent evidence record. Required: an approval with no evidence is an assertion with no provenance.';

-- An approval may only be bound to the workflow epoch that is actually live,
-- and only while the workflow is waiting for it. A stale or premature approval
-- is refused at insert rather than filtered later.
CREATE OR REPLACE FUNCTION approval_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_workflow workflow;
    v_evidence evidence;
BEGIN
    -- (4) Operators cannot mint consent. The actor context is set only by the
    -- handoff functions in 011, so this check cannot be bypassed by a handler
    -- that merely believes it is automation.
    IF current_actor_kind() = 'human' THEN
        RAISE EXCEPTION 'approval refused: a human actor cannot grant booking consent'
            USING ERRCODE = 'RE004',
                  HINT = 'Consent is patient-side. Resolving a ticket is not consent (see handoff_resolve).';
    END IF;

    SELECT * INTO v_workflow FROM workflow w WHERE w.id = NEW.workflow_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'approval refused: workflow % does not exist', NEW.workflow_id
            USING ERRCODE = 'RE004';
    END IF;

    IF NEW.patient_ref <> v_workflow.patient_ref THEN
        RAISE EXCEPTION 'approval refused: patient % does not match workflow patient %',
            NEW.patient_ref, v_workflow.patient_ref
            USING ERRCODE = 'RE004';
    END IF;

    IF v_workflow.state <> 'awaiting_approval' THEN
        RAISE EXCEPTION 'approval refused: workflow % is in state %, not awaiting_approval',
            NEW.workflow_id, v_workflow.state
            USING ERRCODE = 'RE004',
                  HINT = 'An approval is recorded while the workflow waits for it; anything else is a stale intent.';
    END IF;

    IF NEW.state_version <> v_workflow.state_version THEN
        RAISE EXCEPTION 'approval refused: bound to state_version % but workflow % is at %',
            NEW.state_version, NEW.workflow_id, v_workflow.state_version
            USING ERRCODE = 'RE004';
    END IF;

    -- Evidence must exist, be authoritative, and still be fresh. A stale
    -- consent record is distinguishable from a fresh one, and only the fresh
    -- one authorises anything.
    SELECT * INTO v_evidence FROM evidence e WHERE e.id = NEW.evidence_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'approval refused: evidence % does not exist', NEW.evidence_id
            USING ERRCODE = 'RE004';
    END IF;
    IF NOT v_evidence.authoritative THEN
        RAISE EXCEPTION 'approval refused: evidence % is not authoritative', NEW.evidence_id
            USING ERRCODE = 'RE004';
    END IF;
    IF v_evidence.stale_after <= now() THEN
        RAISE EXCEPTION 'approval refused: evidence % went stale at %', NEW.evidence_id, v_evidence.stale_after
            USING ERRCODE = 'RE004',
                  HINT = 'Re-capture the evidence; do not extend fresh_for on the old row (it is append-only).';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER approval_guard_trigger
    BEFORE INSERT ON approval
    FOR EACH ROW EXECUTE FUNCTION approval_guard();

-- The binding is immutable and consumption is one-way. Together these make a
-- consumed approval permanently unusable rather than merely "used".
CREATE OR REPLACE FUNCTION approval_no_rebinding() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.workflow_id          <> OLD.workflow_id
       OR NEW.patient_ref       <> OLD.patient_ref
       OR NEW.state_version     <> OLD.state_version
       OR NEW.booking_payload_hash <> OLD.booking_payload_hash
       OR NEW.evidence_id       <> OLD.evidence_id
       OR NEW.expires_at        <> OLD.expires_at
       OR NEW.approved_at       <> OLD.approved_at THEN
        RAISE EXCEPTION 'approval % cannot be rebound: the binding is immutable', OLD.id
            USING ERRCODE = 'RE004',
                  HINT = 'Record a new approval instead. Rebinding is how a stale consent silently becomes a valid one.';
    END IF;

    IF OLD.consumed_at IS NOT NULL THEN
        RAISE EXCEPTION 'approval % was already consumed at %', OLD.id, OLD.consumed_at
            USING ERRCODE = 'RE004',
                  HINT = 'Consumption is one-way. A second booking needs a new approval.';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER approval_no_rebinding_trigger
    BEFORE UPDATE ON approval
    FOR EACH ROW EXECUTE FUNCTION approval_no_rebinding();

CREATE INDEX approval_workflow_idx ON approval (workflow_id, approved_at DESC);
CREATE INDEX approval_live_idx
    ON approval (workflow_id, state_version)
    WHERE consumed_at IS NULL;

-- ---------------------------------------------------------------------------
-- Live-approval lookup used by the workflow guard below
-- ---------------------------------------------------------------------------

-- An approval that is still able to authorise a write: unconsumed, unexpired,
-- and bound to the epoch and payload the caller is asking about.
CREATE OR REPLACE FUNCTION approval_is_live(
    p_workflow_id          uuid,
    p_patient_ref          text,
    p_state_version        integer,
    p_booking_payload_hash text,
    p_at                   timestamptz DEFAULT now()
) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1
          FROM approval a
         WHERE a.workflow_id          = p_workflow_id
           AND a.patient_ref          = p_patient_ref
           AND a.state_version        = p_state_version
           AND a.booking_payload_hash = p_booking_payload_hash
           AND a.consumed_at IS NULL
           AND a.expires_at > p_at
    )
$$;

COMMENT ON FUNCTION approval_is_live IS
    'True when an unconsumed, unexpired approval matches the given binding tuple exactly. All five binding parts must agree.';

-- ---------------------------------------------------------------------------
-- The workflow side of the binding
-- ---------------------------------------------------------------------------

-- The booking payload hash travels with the workflow in `payload`, under a
-- fixed key. It is the single value the consent is compared against.
CREATE OR REPLACE FUNCTION workflow_booking_payload_hash(p_workflow workflow)
RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT nullif(p_workflow.payload ->> 'booking_payload_hash', '')
$$;

COMMENT ON FUNCTION workflow_booking_payload_hash IS
    'The canonical booking payload hash carried on workflow.payload. Changing any booking-relevant field changes this value.';

-- Entering the booking flow REQUIRES live consent. Entering `booked` requires
-- the booking request itself. Neither is reachable by operator action alone:
-- an operator session cannot insert the approval (007) and cannot insert the
-- booking request (008).
CREATE OR REPLACE FUNCTION workflow_booking_consent_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_hash text;
BEGIN
    IF NEW.state = OLD.state THEN
        RETURN NEW;
    END IF;

    IF NEW.state = 'booking_pending' THEN
        v_hash := workflow_booking_payload_hash(NEW);

        IF v_hash IS NULL THEN
            RAISE EXCEPTION 'workflow % cannot enter booking_pending without a booking_payload_hash on payload',
                NEW.id
                USING ERRCODE = 'RE004';
        END IF;

        IF NOT approval_is_live(NEW.id, NEW.patient_ref, OLD.state_version, v_hash) THEN
            RAISE EXCEPTION
                'workflow % cannot enter booking_pending: no live approval bound to state_version % and payload hash %',
                NEW.id, OLD.state_version, v_hash
                USING ERRCODE = 'RE004',
                      HINT = 'Either the patient has not approved, the approval expired, it was already consumed, or the payload changed after approval.';
        END IF;
    END IF;

    IF NEW.state = 'booked' THEN
        IF NOT EXISTS (
            SELECT 1
              FROM booking_request b
             WHERE b.workflow_id = NEW.id
               AND b.state = 'confirmed'
        ) THEN
            RAISE EXCEPTION 'workflow % cannot be marked booked: no confirmed booking_request exists', NEW.id
                USING ERRCODE = 'RE004';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION workflow_booking_consent_guard IS
    'Second BEFORE UPDATE guard on workflow. Makes booking_pending unreachable without live patient consent, and booked unreachable without a confirmed booking request.';

-- PostgreSQL fires BEFORE ROW triggers in name order, and this one
-- (`workflow_booking_consent_trigger`) sorts before `workflow_guard_trigger`
-- from 003. That ordering is intentional: the specific, actionable rule
-- ("this booking has no consent behind it") is reported in preference to the
-- generic one ("this edge is not in the state machine"), because it is the one
-- that tells the caller what to fix. Both refuse; a caller cannot mistake one
-- for a success.
CREATE TRIGGER workflow_booking_consent_trigger
    BEFORE UPDATE ON workflow
    FOR EACH ROW EXECUTE FUNCTION workflow_booking_consent_guard();
