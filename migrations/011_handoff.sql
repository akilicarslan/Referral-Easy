-- 011_handoff.sql
-- Human ownership of a ticket.
--
-- Two rules, both of which have to be structural:
--
--  1. WHILE A HUMAN OWNS A WORKFLOW, AUTOMATION WRITES ARE SUSPENDED.
--     Owning means workflow.human_owner_id is set. The guard in 003 refuses any
--     update to that workflow from a non-human actor, so an agent that has
--     already started work simply cannot land its write.
--
--  2. A CONFLICTING CLAIM ON ONE TICKET MUST BE IMPOSSIBLE.
--     The claim is a compare-and-set on claim_generation, in one UPDATE. Two
--     operators reading generation N cannot both win: the loser's WHERE clause
--     stops matching the moment the winner commits its bump.
--
-- And the non-rule, which is just as important:
--
--  3. RESOLVING A TICKET IS NOT BOOKING CONSENT.
--     `handoff_resolve` can return a workflow to triage or close it out, and
--     that is all. The booking states are absent from its allow-list, the
--     approval guard (007) refuses consent created by a human actor, and the
--     consent guard (007) refuses the transition without an approval. An
--     operator cannot approve a booking by acting confident about it.

CREATE TABLE handoff (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id       uuid        NOT NULL REFERENCES workflow (id) ON DELETE RESTRICT,
    ticket_ref        text        NOT NULL,
    reason            text        NOT NULL,
    state             text        NOT NULL DEFAULT 'open',

    -- The compare-and-set token. It only ever moves forward, by exactly one,
    -- and only as part of a successful claim.
    claim_generation  bigint      NOT NULL DEFAULT 0,

    claimed_by        text,
    claimed_at        timestamptz,
    claim_expires_at  timestamptz,

    resolved_at       timestamptz,
    resolution        text,
    resolution_note   text,

    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT handoff_state_known
        CHECK (state IN ('open', 'claimed', 'resolved', 'cancelled')),
    CONSTRAINT handoff_reason_present
        CHECK (length(btrim(reason)) > 0),
    CONSTRAINT handoff_ticket_ref_present
        CHECK (length(btrim(ticket_ref)) > 0),
    CONSTRAINT handoff_claim_generation_non_negative
        CHECK (claim_generation >= 0),

    -- Claim metadata is all-or-nothing: an owner with no deadline cannot be
    -- reclaimed, and a deadline with no owner cannot be reported on.
    CONSTRAINT handoff_claim_is_whole
        CHECK ((claimed_by IS NULL) = (claim_expires_at IS NULL)),
    CONSTRAINT handoff_claimed_is_populated
        CHECK (state <> 'claimed' OR (claimed_by IS NOT NULL AND claim_expires_at IS NOT NULL)),

    CONSTRAINT handoff_resolved_is_populated
        CHECK ((state = 'resolved') = (resolved_at IS NOT NULL)),
    CONSTRAINT handoff_resolution_present
        CHECK (state <> 'resolved' OR nullif(btrim(resolution), '') IS NOT NULL),

    -- A closed handoff must say how it closed.
    CONSTRAINT handoff_terminal_has_resolution
        CHECK (state IN ('open', 'claimed') OR nullif(btrim(coalesce(resolution, '')), '') IS NOT NULL
               OR state = 'cancelled')
);

COMMENT ON TABLE handoff IS
    'A ticket raised for human attention. Holding a claim means holding workflow human ownership: automation is suspended for that workflow for as long as the claim stands.';
COMMENT ON COLUMN handoff.claim_generation IS
    'Compare-and-set token for the ticket claim. A claim whose expected generation is stale finds no row, so it cannot take a ticket away from a live operator.';
COMMENT ON COLUMN handoff.claim_expires_at IS
    'Claim lease. An expired claim is reclaimable, so an operator who walks away does not freeze the workflow.';
COMMENT ON COLUMN handoff.resolution IS
    'Free-text outcome. It is NOT consent: it documents what the operator decided about the ticket, never a patient authorisation to book.';

-- One live handoff per workflow. History is kept; a second escalation after the
-- first is resolved is a new row, not an edit of the old one.
CREATE UNIQUE INDEX handoff_one_live_per_workflow
    ON handoff (workflow_id)
    WHERE state IN ('open', 'claimed');

CREATE INDEX handoff_queue_idx
    ON handoff (state, created_at)
    WHERE state IN ('open', 'claimed');

CREATE INDEX handoff_operator_idx
    ON handoff (claimed_by, claim_expires_at)
    WHERE claimed_by IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Claim generation only moves forward
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION handoff_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.claim_generation < OLD.claim_generation THEN
        RAISE EXCEPTION 'stale claim_generation for handoff %: row is at %, writer supplied %',
            OLD.id, OLD.claim_generation, NEW.claim_generation
            USING ERRCODE = 'RE001',
                  HINT = 'Re-read the ticket and retry with the current claim generation.';
    END IF;

    IF NEW.claim_generation = OLD.claim_generation
       AND NEW.claimed_by IS DISTINCT FROM OLD.claimed_by THEN
        RAISE EXCEPTION 'handoff % changed owner without advancing claim_generation', OLD.id
            USING ERRCODE = 'RE001',
                  HINT = 'Every ownership change is a new claim generation.';
    END IF;

    IF NEW.claim_generation > OLD.claim_generation + 1 THEN
        RAISE EXCEPTION 'handoff % claim_generation jumped from % to %',
            OLD.id, OLD.claim_generation, NEW.claim_generation
            USING ERRCODE = 'RE001';
    END IF;

    -- Terminal tickets stay terminal.
    IF OLD.state IN ('resolved', 'cancelled') AND NEW.state <> OLD.state THEN
        RAISE EXCEPTION 'handoff % is % and cannot return to %', OLD.id, OLD.state, NEW.state
            USING ERRCODE = 'RE002';
    END IF;

    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER handoff_guard_trigger
    BEFORE UPDATE ON handoff
    FOR EACH ROW EXECUTE FUNCTION handoff_guard();

-- ---------------------------------------------------------------------------
-- Claim / release / resolve
-- ---------------------------------------------------------------------------

-- Compare-and-set claim. The WHERE clause is the entire concurrency control:
-- expected generation, a state that can be claimed, and no live claim by
-- somebody else.
CREATE OR REPLACE FUNCTION handoff_claim(
    p_handoff_id          uuid,
    p_operator_id         text,
    p_expected_generation bigint,
    p_lease_seconds       integer DEFAULT 900
) RETURNS handoff
LANGUAGE plpgsql AS $$
DECLARE
    v_handoff    handoff;
    v_workflow   workflow;
    v_prev_actor text;
BEGIN
    UPDATE handoff h
       SET state            = 'claimed',
           claimed_by       = p_operator_id,
           claimed_at       = now(),
           claim_expires_at = now() + make_interval(secs => p_lease_seconds),
           claim_generation = h.claim_generation + 1
     WHERE h.id = p_handoff_id
       AND h.state IN ('open', 'claimed')
       AND h.claim_generation = p_expected_generation
       AND (h.claimed_by IS NULL
            OR h.claim_expires_at <= now()
            OR h.claimed_by = p_operator_id)
    RETURNING * INTO v_handoff;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'claim of handoff % by % rejected: generation % is not current, the ticket is closed, or another operator holds a live claim',
            p_handoff_id, p_operator_id, p_expected_generation
            USING ERRCODE = 'RE001',
                  HINT = 'Re-read the ticket. An expired claim is reclaimable; a live one is not.';
    END IF;

    -- Suspend automation on the workflow while the human owns it. Nothing else
    -- has to remember to check: the workflow guard reads human_owner_id.
    --
    -- This update is attributed to the operator. It has to be: when the claim
    -- being taken over was left behind by another operator, human_owner_id is
    -- already set, and the workflow guard refuses a non-human writer there.
    v_prev_actor := current_actor_kind();
    PERFORM set_config('referraleasy.actor_kind', 'human', true);
    PERFORM set_config('referraleasy.actor_id', p_operator_id, true);

    UPDATE workflow w
       SET human_owner_id   = p_operator_id,
           human_claimed_at = v_handoff.claimed_at,
           owner_worker_id  = NULL,
           lease_expires_at = NULL,
           generation       = w.generation + 1
     WHERE w.id = v_handoff.workflow_id
    RETURNING * INTO v_workflow;

    PERFORM set_config('referraleasy.actor_kind', v_prev_actor, true);

    IF NOT FOUND THEN
        RAISE EXCEPTION 'handoff % references missing workflow %', v_handoff.id, v_handoff.workflow_id
            USING ERRCODE = 'RE001';
    END IF;

    RETURN v_handoff;
END;
$$;

COMMENT ON FUNCTION handoff_claim IS
    'Compare-and-set claim on a ticket, and on success takes human ownership of the workflow. Two operators claiming the same generation: exactly one UPDATE matches.';

-- Give the ticket back without resolving it. Automation resumes.
CREATE OR REPLACE FUNCTION handoff_release(
    p_handoff_id          uuid,
    p_operator_id         text,
    p_expected_generation bigint
) RETURNS handoff
LANGUAGE plpgsql AS $$
DECLARE
    v_handoff    handoff;
    v_prev_actor text;
BEGIN
    UPDATE handoff h
       SET state            = 'open',
           claimed_by       = NULL,
           claim_expires_at = NULL,
           claim_generation = h.claim_generation + 1
     WHERE h.id = p_handoff_id
       AND h.state = 'claimed'
       AND h.claimed_by = p_operator_id
       AND h.claim_generation = p_expected_generation
    RETURNING * INTO v_handoff;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'release of handoff % rejected: not claimed by % at generation %',
            p_handoff_id, p_operator_id, p_expected_generation
            USING ERRCODE = 'RE001';
    END IF;

    -- Releasing is an operator action on a workflow the operator owns, so the
    -- transaction has to be attributed to them for the guard to let it through.
    v_prev_actor := current_actor_kind();
    PERFORM set_config('referraleasy.actor_kind', 'human', true);
    PERFORM set_config('referraleasy.actor_id', p_operator_id, true);

    UPDATE workflow w
       SET human_owner_id   = NULL,
           human_claimed_at = NULL,
           generation       = w.generation + 1
     WHERE w.id = v_handoff.workflow_id;

    PERFORM set_config('referraleasy.actor_kind', v_prev_actor, true);

    RETURN v_handoff;
END;
$$;

COMMENT ON FUNCTION handoff_release IS
    'Releases a claim and clears workflow human ownership. Automation may write to the workflow again once this commits.';

-- Resolve a ticket: close the handoff and return the workflow to automation.
--
-- The allow-list below is the guard against rule 3. There is no path from here
-- to booking_pending or booked, and even if there were, the consent guard in
-- 007 would refuse the transition for want of a live approval.
CREATE OR REPLACE FUNCTION handoff_resolve(
    p_handoff_id          uuid,
    p_operator_id         text,
    p_expected_generation bigint,
    p_resolution          text,
    p_workflow_state      text DEFAULT 'triaged',
    p_note                text DEFAULT NULL
) RETURNS handoff
LANGUAGE plpgsql AS $$
DECLARE
    v_handoff       handoff;
    v_prev_actor    text;
    v_workflow      workflow;
BEGIN
    -- Outcomes an operator is allowed to produce. Booking states are
    -- deliberately absent: resolving a ticket is not booking consent.
    IF p_workflow_state NOT IN ('triaged', 'resolved', 'failed', 'cancelled') THEN
        RAISE EXCEPTION
            'operator resolution cannot move a workflow to % (allowed: triaged, resolved, failed, cancelled)',
            p_workflow_state
            USING ERRCODE = 'RE004',
                  HINT = 'Booking consent comes from the patient via approval (007), never from the handoff queue.';
    END IF;

    UPDATE handoff h
       SET state            = 'resolved',
           resolved_at      = now(),
           resolution       = p_resolution,
           resolution_note  = p_note,
           claim_generation = h.claim_generation + 1
     WHERE h.id = p_handoff_id
       AND h.state = 'claimed'
       AND h.claimed_by = p_operator_id
       AND h.claim_generation = p_expected_generation
    RETURNING * INTO v_handoff;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'resolution of handoff % rejected: not claimed by % at generation %',
            p_handoff_id, p_operator_id, p_expected_generation
            USING ERRCODE = 'RE001';
    END IF;

    -- Attribute this transaction to the operator, so the workflow guard sees a
    -- human release rather than an automation write, then put the previous
    -- actor context back so the rest of the transaction is unaffected.
    v_prev_actor := current_actor_kind();
    PERFORM set_config('referraleasy.actor_kind', 'human', true);
    PERFORM set_config('referraleasy.actor_id', p_operator_id, true);

    UPDATE workflow w
       SET state            = p_workflow_state,
           state_version    = w.state_version + 1,
           generation       = w.generation + 1,
           human_owner_id   = NULL,
           human_claimed_at = NULL
     WHERE w.id = v_handoff.workflow_id
    RETURNING * INTO v_workflow;

    PERFORM set_config('referraleasy.actor_kind', v_prev_actor, true);

    IF NOT FOUND THEN
        RAISE EXCEPTION 'handoff % references missing workflow %', v_handoff.id, v_handoff.workflow_id
            USING ERRCODE = 'RE001';
    END IF;

    RETURN v_handoff;
END;
$$;

COMMENT ON FUNCTION handoff_resolve IS
    'Closes a ticket, releases human ownership and returns the workflow to the allow-listed state. It cannot grant booking consent: the booking states are not reachable from here.';

-- ---------------------------------------------------------------------------
-- Queue view for the operator console
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW handoff_queue AS
    SELECT h.id,
           h.workflow_id,
           h.ticket_ref,
           h.reason,
           h.state,
           h.claim_generation,
           h.claimed_by,
           h.claim_expires_at,
           (h.state = 'claimed' AND h.claim_expires_at > now()) AS claim_is_live,
           h.created_at,
           w.state AS workflow_state,
           w.patient_ref
      FROM handoff h
      JOIN workflow w ON w.id = h.workflow_id
     WHERE h.state IN ('open', 'claimed');

COMMENT ON VIEW handoff_queue IS
    'Open and claimed tickets with liveness already resolved, so a console does not re-derive it (and cannot disagree with the database about it).';
