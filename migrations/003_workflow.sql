-- 003_workflow.sql
-- The workflow: the aggregate root of control state, plus the two invariants
-- that make distributed workers safe:
--
--   * optimistic concurrency  -> state_version must advance by exactly one,
--                                and only together with a legal transition
--   * owner + lease + monotonic generation -> a stale worker CANNOT write
--
-- Both are enforced here, in the table's trigger and in single-statement
-- functions, not in application code.

-- ---------------------------------------------------------------------------
-- State vocabulary
-- ---------------------------------------------------------------------------
CREATE TABLE workflow_state (
    code       text    PRIMARY KEY,
    label      text    NOT NULL,
    terminal   boolean NOT NULL DEFAULT false,
    sort_order integer NOT NULL
);

INSERT INTO workflow_state (code, label, terminal, sort_order) VALUES
    ('intake',            'Intake',               false, 10),
    ('triaged',           'Triaged',              false, 20),
    ('awaiting_approval', 'Awaiting approval',    false, 30),
    ('booking_pending',   'Booking pending',      false, 40),
    ('booked',            'Booked',               false, 50),
    ('human_required',    'Human required',       false, 60),
    ('resolved',          'Resolved',             true,  70),
    ('failed',            'Failed',               true,  80),
    ('cancelled',         'Cancelled',            true,  90);

-- The transition relation IS the legal state machine. An edge that is not in
-- this table is not a transition, whatever the caller believes.
CREATE TABLE workflow_transition (
    from_state text NOT NULL REFERENCES workflow_state (code),
    to_state   text NOT NULL REFERENCES workflow_state (code),
    PRIMARY KEY (from_state, to_state),
    CONSTRAINT workflow_transition_no_self_loop CHECK (from_state <> to_state)
);

COMMENT ON TABLE workflow_transition IS
    'Allowed workflow state edges. The guard trigger rejects any state change that has no row here.';

INSERT INTO workflow_transition (from_state, to_state) VALUES
    ('intake',            'triaged'),
    ('intake',            'human_required'),
    ('intake',            'failed'),
    ('intake',            'cancelled'),
    ('triaged',           'awaiting_approval'),
    ('triaged',           'human_required'),
    ('triaged',           'failed'),
    ('triaged',           'cancelled'),
    ('awaiting_approval', 'booking_pending'),
    ('awaiting_approval', 'triaged'),
    ('awaiting_approval', 'human_required'),
    ('awaiting_approval', 'failed'),
    ('awaiting_approval', 'cancelled'),
    ('booking_pending',   'booked'),
    ('booking_pending',   'awaiting_approval'),
    ('booking_pending',   'human_required'),
    ('booking_pending',   'failed'),
    ('booking_pending',   'cancelled'),
    ('booked',            'resolved'),
    ('booked',            'human_required'),
    ('human_required',    'triaged'),
    ('human_required',    'resolved'),
    ('human_required',    'failed'),
    ('human_required',    'cancelled');

-- ---------------------------------------------------------------------------
-- Workflow
-- ---------------------------------------------------------------------------
CREATE TABLE workflow (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_ref        text        NOT NULL,
    patient_ref       text        NOT NULL,
    specialty         text,
    state             text        NOT NULL DEFAULT 'intake' REFERENCES workflow_state (code),
    state_version     integer     NOT NULL DEFAULT 1,
    generation        bigint      NOT NULL DEFAULT 0,
    owner_worker_id   text,
    lease_expires_at  timestamptz,
    human_owner_id    text,
    human_claimed_at  timestamptz,
    payload           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT workflow_state_version_positive CHECK (state_version >= 1),
    CONSTRAINT workflow_generation_non_negative CHECK (generation >= 0),

    -- A lease is either complete (owner + expiry) or absent. Half a lease is a
    -- bug that would otherwise silently disable the staleness check.
    CONSTRAINT workflow_lease_is_whole
        CHECK ((owner_worker_id IS NULL) = (lease_expires_at IS NULL)),

    -- Human ownership is likewise all-or-nothing.
    CONSTRAINT workflow_human_ownership_is_whole
        CHECK ((human_owner_id IS NULL) = (human_claimed_at IS NULL)),

    -- Machine ownership and human ownership are mutually exclusive: while a
    -- human owns the workflow, automation is suspended (enforced again in the
    -- guard trigger, which is the part that actually blocks writes).
    CONSTRAINT workflow_single_owner
        CHECK (owner_worker_id IS NULL OR human_owner_id IS NULL),

    CONSTRAINT workflow_ticket_ref_present CHECK (length(btrim(ticket_ref)) > 0),
    CONSTRAINT workflow_patient_ref_present CHECK (length(btrim(patient_ref)) > 0)
);

COMMENT ON TABLE workflow IS
    'Control state of one referral workflow. PostgreSQL is the system of record; OpenSearch never is.';

COMMENT ON COLUMN workflow.state_version IS
    'Optimistic concurrency token. A state change MUST advance it by exactly one (guard trigger, RE002).';
COMMENT ON COLUMN workflow.generation IS
    'Monotonic ownership epoch. Every successful claim/advance bumps it, so a worker holding an older generation is provably stale.';
COMMENT ON COLUMN workflow.lease_expires_at IS
    'Worker lease deadline. An expired lease makes the owner stale even if it still claims to own the row.';

-- At most one live workflow per ticket. Terminal workflows release the ticket
-- reference so a fresh referral can be opened for the same ticket later.
CREATE UNIQUE INDEX workflow_active_ticket_key
    ON workflow (ticket_ref)
    WHERE state NOT IN ('resolved', 'failed', 'cancelled');

CREATE INDEX workflow_lease_idx
    ON workflow (lease_expires_at)
    WHERE owner_worker_id IS NOT NULL;

CREATE INDEX workflow_human_owned_idx
    ON workflow (human_owner_id)
    WHERE human_owner_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Guard: every UPDATE of a workflow passes through here
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION workflow_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- (3) Human ownership suspends automation. This is the constraint, not a hope.
    IF OLD.human_owner_id IS NOT NULL AND current_actor_kind() <> 'human' THEN
        RAISE EXCEPTION 'workflow % is owned by human %; automation writes are suspended',
            OLD.id, OLD.human_owner_id
            USING ERRCODE = 'RE003',
                  HINT = 'Release human ownership (handoff_resolve) before automation may write again.';
    END IF;

    -- (1) Generation is monotonic for everyone, humans included.
    IF NEW.generation < OLD.generation THEN
        RAISE EXCEPTION 'stale generation for workflow %: row is at %, writer supplied %',
            OLD.id, OLD.generation, NEW.generation
            USING ERRCODE = 'RE001',
                  HINT = 'Re-read the workflow and retry with the current generation.';
    END IF;

    -- Ownership handover must be visible as a generation bump, otherwise two
    -- owners can believe they hold the same epoch.
    IF NEW.owner_worker_id IS DISTINCT FROM OLD.owner_worker_id
       AND NEW.generation = OLD.generation THEN
        RAISE EXCEPTION 'workflow % ownership changed without advancing generation',
            OLD.id
            USING ERRCODE = 'RE001',
                  HINT = 'An ownership change is a new epoch: set generation = generation + 1.';
    END IF;

    -- (2) State changes: legal edge AND exactly one version step.
    IF NEW.state IS DISTINCT FROM OLD.state THEN
        IF NOT EXISTS (
            SELECT 1 FROM workflow_transition t
             WHERE t.from_state = OLD.state AND t.to_state = NEW.state
        ) THEN
            RAISE EXCEPTION 'illegal transition for workflow %: % -> %',
                OLD.id, OLD.state, NEW.state
                USING ERRCODE = 'RE002',
                      HINT = 'Transitions are the rows of workflow_transition; add an edge deliberately if the machine really changed.';
        END IF;

        IF NEW.state_version <> OLD.state_version + 1 THEN
            RAISE EXCEPTION 'workflow % state change must advance state_version by exactly one (had %, supplied %)',
                OLD.id, OLD.state_version, NEW.state_version
                USING ERRCODE = 'RE002';
        END IF;
    ELSIF NEW.state_version <> OLD.state_version THEN
        RAISE EXCEPTION 'workflow % changed state_version without changing state', OLD.id
            USING ERRCODE = 'RE002',
                  HINT = 'state_version is the transition counter; it moves only with a transition.';
    END IF;

    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER workflow_guard_trigger
    BEFORE UPDATE ON workflow
    FOR EACH ROW EXECUTE FUNCTION workflow_guard();

-- ---------------------------------------------------------------------------
-- Single-statement operations
-- ---------------------------------------------------------------------------

-- Claim (or re-claim after expiry) a workflow for a worker.
-- Compare-and-set on generation AND on lease state, in one UPDATE, so two
-- workers cannot both win: the loser's WHERE no longer matches after the
-- winner's row lock is released.
CREATE OR REPLACE FUNCTION workflow_claim(
    p_workflow_id       uuid,
    p_worker_id         text,
    p_expected_generation bigint,
    p_lease_seconds     integer DEFAULT 60
) RETURNS workflow
LANGUAGE plpgsql AS $$
DECLARE
    v_row workflow;
BEGIN
    UPDATE workflow w
       SET owner_worker_id  = p_worker_id,
           lease_expires_at = now() + make_interval(secs => p_lease_seconds),
           generation       = w.generation + 1
     WHERE w.id = p_workflow_id
       AND w.human_owner_id IS NULL
       AND w.generation = p_expected_generation
       AND (w.owner_worker_id IS NULL OR w.lease_expires_at < now())
    RETURNING * INTO v_row;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'claim of workflow % by % rejected: generation % is not current or the workflow is owned/leased elsewhere',
            p_workflow_id, p_worker_id, p_expected_generation
            USING ERRCODE = 'RE001',
                  HINT = 'Re-read the workflow and retry with the current generation; an expired lease is reclaimable.';
    END IF;

    RETURN v_row;
END;
$$;

COMMENT ON FUNCTION workflow_claim IS
    'Atomic claim: succeeds only when the caller holds the current generation, no human owns the row, and the lease is free or expired. Bumps generation.';

-- Advance the state of a workflow that the caller currently owns and leases.
-- The WHERE clause is the whole concurrency control: owner + live lease +
-- expected generation + expected state_version.
CREATE OR REPLACE FUNCTION workflow_advance(
    p_workflow_id           uuid,
    p_worker_id             text,
    p_expected_generation   bigint,
    p_expected_state_version integer,
    p_to_state              text,
    p_lease_seconds         integer DEFAULT 30
) RETURNS workflow
LANGUAGE plpgsql AS $$
DECLARE
    v_row workflow;
BEGIN
    UPDATE workflow w
       SET state            = p_to_state,
           state_version    = w.state_version + 1,
           generation       = w.generation + 1,
           lease_expires_at = now() + make_interval(secs => p_lease_seconds)
     WHERE w.id = p_workflow_id
       AND w.owner_worker_id = p_worker_id
       AND w.human_owner_id IS NULL
       AND w.generation = p_expected_generation
       AND w.state_version = p_expected_state_version
       AND w.lease_expires_at > now()
    RETURNING * INTO v_row;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'advance of workflow % to % rejected: lease expired, owner differs, or generation/state_version is stale',
            p_workflow_id, p_to_state
            USING ERRCODE = 'RE001',
                  HINT = 'A stale worker cannot write. Re-read the workflow; if the lease expired, another worker owns it now.';
    END IF;

    RETURN v_row;
END;
$$;

COMMENT ON FUNCTION workflow_advance IS
    'Atomic transition: requires current ownership, a live lease, the current generation and the current state_version. Bumps generation and state_version.';
