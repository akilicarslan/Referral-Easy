-- 014_align_domain_vocabulary.sql
-- Align the persisted control-state vocabulary with the contract schemas.
--
-- WHY THIS EXISTS
-- ---------------
-- Migrations 003 and 005 invented their own vocabulary: nine coarse workflow
-- states (`intake`, `triaged`, `awaiting_approval`, `booking_pending`, ...),
-- five application agent roles and ten tool names. None of them match
-- `packages/contracts/schemas/common.json#/$defs/WorkflowState` or the agent
-- and tool table in §3 of the architecture, which is the specification.
--
-- The coarse vocabulary is not merely differently-named, it is *unable to
-- express the policy the spec requires*. Two examples, both live rows in 005:
--
--   * `booking_agent` was granted `find_slots` in BOTH `awaiting_approval` and
--     `booking_pending`. There was no state separating "choosing a slot" from
--     "a booking is in flight", so re-searching for slots mid-booking could not
--     be denied. The spec's flow (§4) settles slots *before* booking, so those
--     are different states with different permissions.
--
--   * There was no `READINESS_CHECK` state at all, so §2.3's document-readiness
--     step could not be policy-gated; any tool could run during it.
--
-- It also could not express §4's ordering rule that a *provider-level* network
-- check happens only after the doctor is known (`check_provider_network` is now
-- gated to AWAITING_DOCTOR_CHOICE alone, where before it had no home).
--
-- The contract vocabulary wins: it is derived from §4's flow and §2.5's booking
-- lifecycle, and the UI and decision layer both key off it. This migration
-- replaces the vocabulary and re-derives the policy table from §3.
--
-- Forward-only, so this is a new file rather than an edit of 003/005. The
-- tables are DATA, so this is a data migration plus one CREATE OR REPLACE of a
-- guard whose literals moved.
--
-- NOTHING HERE RELAXES A GUARD. The constraints, triggers and RE00x codes added
-- by 002-013 are unchanged; only the vocabulary they are written against.

-- ---------------------------------------------------------------------------
-- 1. Workflow states — now exactly WorkflowState from common.json
-- ---------------------------------------------------------------------------
INSERT INTO workflow_state (code, label, terminal, sort_order) VALUES
    ('INTAKE',                   'Intake',                      false,  10),
    ('COVERAGE_CHECK',           'Coverage check',              false,  20),
    ('PROVIDER_MATCHING',        'Provider matching',           false,  30),
    ('AWAITING_HOSPITAL_CHOICE', 'Awaiting hospital choice',    false,  40),
    ('AWAITING_DOCTOR_CHOICE',   'Awaiting doctor choice',      false,  50),
    ('READINESS_CHECK',          'Referral readiness check',    false,  60),
    ('AWAITING_SLOT_CHOICE',     'Awaiting slot choice',        false,  70),
    ('AWAITING_FINAL_APPROVAL',  'Awaiting final approval',     false,  80),
    ('BOOKING_IN_FLIGHT',        'Booking in flight',           false,  90),
    ('BOOKED',                   'Booked',                      false, 100),
    ('BOOKING_UNVERIFIED',       'Booking outcome unverified',  false, 110),
    ('RECOVERY',                 'Recovery',                    false, 120),
    ('HUMAN_HANDOFF',            'Human handoff',               false, 130),
    ('ABANDONED',                'Abandoned',                   true,  140),
    ('CANCELLED',                'Cancelled',                   true,  150),
    ('FAILED',                   'Failed',                      true,  160)
ON CONFLICT (code) DO UPDATE
    SET label = EXCLUDED.label, terminal = EXCLUDED.terminal, sort_order = EXCLUDED.sort_order;

COMMENT ON TABLE workflow_state IS
    'Workflow states. Codes are exactly WorkflowState in packages/contracts/schemas/common.json (§4, §2.5).';

-- ---------------------------------------------------------------------------
-- 2. Move any existing workflow rows onto the new vocabulary
-- ---------------------------------------------------------------------------
-- No production data exists; this is here so an already-migrated development
-- database converges rather than failing on the foreign key below. The mapping
-- is lossy in one direction only: `triaged` collapsed the whole post-intake
-- journey, so it lands on the first state that journey reaches.
UPDATE workflow SET state = CASE state
    WHEN 'intake'            THEN 'INTAKE'
    WHEN 'triaged'           THEN 'PROVIDER_MATCHING'
    WHEN 'awaiting_approval' THEN 'AWAITING_FINAL_APPROVAL'
    WHEN 'booking_pending'   THEN 'BOOKING_IN_FLIGHT'
    WHEN 'booked'            THEN 'BOOKED'
    WHEN 'human_required'    THEN 'HUMAN_HANDOFF'
    WHEN 'resolved'          THEN 'BOOKED'
    WHEN 'failed'            THEN 'FAILED'
    WHEN 'cancelled'         THEN 'CANCELLED'
    ELSE state
END
WHERE state IN ('intake', 'triaged', 'awaiting_approval', 'booking_pending',
                'booked', 'human_required', 'resolved', 'failed', 'cancelled');

-- ---------------------------------------------------------------------------
-- 3. Transitions — §4's flow, §2.5's booking lifecycle, §11's safe return
-- ---------------------------------------------------------------------------
-- The edge set IS the state machine (003). Back edges are real: the patient may
-- change hospital after seeing doctors, and §2.4's negotiation may re-open the
-- search. What is NOT an edge is booking straight out of an approval state
-- without passing through BOOKING_IN_FLIGHT — that is what makes the consent
-- guard on that single edge meaningful.
DELETE FROM workflow_transition;

INSERT INTO workflow_transition (from_state, to_state) VALUES
    -- intake fans out to the two independent workstreams (§4: coverage ∥ matching)
    ('INTAKE', 'COVERAGE_CHECK'),
    ('INTAKE', 'PROVIDER_MATCHING'),
    ('INTAKE', 'HUMAN_HANDOFF'),
    ('INTAKE', 'FAILED'),
    ('INTAKE', 'ABANDONED'),
    ('INTAKE', 'CANCELLED'),

    ('COVERAGE_CHECK', 'PROVIDER_MATCHING'),
    ('COVERAGE_CHECK', 'HUMAN_HANDOFF'),
    ('COVERAGE_CHECK', 'RECOVERY'),
    ('COVERAGE_CHECK', 'FAILED'),
    ('COVERAGE_CHECK', 'ABANDONED'),
    ('COVERAGE_CHECK', 'CANCELLED'),

    ('PROVIDER_MATCHING', 'AWAITING_HOSPITAL_CHOICE'),
    ('PROVIDER_MATCHING', 'COVERAGE_CHECK'),
    ('PROVIDER_MATCHING', 'HUMAN_HANDOFF'),
    ('PROVIDER_MATCHING', 'RECOVERY'),
    ('PROVIDER_MATCHING', 'FAILED'),
    ('PROVIDER_MATCHING', 'ABANDONED'),
    ('PROVIDER_MATCHING', 'CANCELLED'),

    -- §4: hospitals, then doctors at the chosen hospital, then slots
    ('AWAITING_HOSPITAL_CHOICE', 'AWAITING_DOCTOR_CHOICE'),
    ('AWAITING_HOSPITAL_CHOICE', 'PROVIDER_MATCHING'),
    ('AWAITING_HOSPITAL_CHOICE', 'HUMAN_HANDOFF'),
    ('AWAITING_HOSPITAL_CHOICE', 'ABANDONED'),
    ('AWAITING_HOSPITAL_CHOICE', 'CANCELLED'),

    -- the doctor-based network check (§4) happens here and nowhere earlier
    ('AWAITING_DOCTOR_CHOICE', 'READINESS_CHECK'),
    ('AWAITING_DOCTOR_CHOICE', 'AWAITING_SLOT_CHOICE'),
    ('AWAITING_DOCTOR_CHOICE', 'AWAITING_HOSPITAL_CHOICE'),
    ('AWAITING_DOCTOR_CHOICE', 'HUMAN_HANDOFF'),
    ('AWAITING_DOCTOR_CHOICE', 'ABANDONED'),
    ('AWAITING_DOCTOR_CHOICE', 'CANCELLED'),

    ('READINESS_CHECK', 'AWAITING_SLOT_CHOICE'),
    ('READINESS_CHECK', 'HUMAN_HANDOFF'),
    ('READINESS_CHECK', 'RECOVERY'),
    ('READINESS_CHECK', 'ABANDONED'),
    ('READINESS_CHECK', 'CANCELLED'),

    ('AWAITING_SLOT_CHOICE', 'AWAITING_FINAL_APPROVAL'),
    ('AWAITING_SLOT_CHOICE', 'READINESS_CHECK'),
    ('AWAITING_SLOT_CHOICE', 'HUMAN_HANDOFF'),
    ('AWAITING_SLOT_CHOICE', 'ABANDONED'),
    ('AWAITING_SLOT_CHOICE', 'CANCELLED'),

    ('AWAITING_FINAL_APPROVAL', 'BOOKING_IN_FLIGHT'),
    ('AWAITING_FINAL_APPROVAL', 'AWAITING_SLOT_CHOICE'),
    ('AWAITING_FINAL_APPROVAL', 'HUMAN_HANDOFF'),
    ('AWAITING_FINAL_APPROVAL', 'ABANDONED'),
    ('AWAITING_FINAL_APPROVAL', 'CANCELLED'),

    -- a booking in flight may return resolvable-but-unconfirmed, never straight to a retry
    ('BOOKING_IN_FLIGHT', 'BOOKED'),
    ('BOOKING_IN_FLIGHT', 'BOOKING_UNVERIFIED'),
    ('BOOKING_IN_FLIGHT', 'RECOVERY'),
    ('BOOKING_IN_FLIGHT', 'HUMAN_HANDOFF'),
    ('BOOKING_IN_FLIGHT', 'FAILED'),
    ('BOOKING_IN_FLIGHT', 'ABANDONED'),

    -- §7: an unknown outcome is resolved by reconciliation, not by re-booking
    ('BOOKING_UNVERIFIED', 'BOOKED'),
    ('BOOKING_UNVERIFIED', 'BOOKING_IN_FLIGHT'),
    ('BOOKING_UNVERIFIED', 'RECOVERY'),
    ('BOOKING_UNVERIFIED', 'HUMAN_HANDOFF'),
    ('BOOKING_UNVERIFIED', 'FAILED'),

    -- §2.5: a reschedule goes back through slot choice; the old booking is not
    -- cancelled before the new one is verified
    ('BOOKED', 'AWAITING_SLOT_CHOICE'),
    ('BOOKED', 'CANCELLED'),
    ('BOOKED', 'FAILED'),

    ('RECOVERY', 'PROVIDER_MATCHING'),
    ('RECOVERY', 'AWAITING_SLOT_CHOICE'),
    ('RECOVERY', 'READINESS_CHECK'),
    ('RECOVERY', 'HUMAN_HANDOFF'),
    ('RECOVERY', 'FAILED'),
    ('RECOVERY', 'ABANDONED'),
    ('RECOVERY', 'CANCELLED'),

    -- §11: the operator returns the workflow to a safe state; the final booking
    -- still needs patient approval, so no edge leads straight to booking
    ('HUMAN_HANDOFF', 'INTAKE'),
    ('HUMAN_HANDOFF', 'PROVIDER_MATCHING'),
    ('HUMAN_HANDOFF', 'READINESS_CHECK'),
    ('HUMAN_HANDOFF', 'AWAITING_SLOT_CHOICE'),
    ('HUMAN_HANDOFF', 'RECOVERY'),
    ('HUMAN_HANDOFF', 'FAILED'),
    ('HUMAN_HANDOFF', 'ABANDONED'),
    ('HUMAN_HANDOFF', 'CANCELLED');

-- ---------------------------------------------------------------------------
-- 4. Roles — §3's agent table, verbatim
-- ---------------------------------------------------------------------------
-- 005's roles (`triage_agent`, `booking_agent`, ...) were invented and did not
-- correspond to any service the system has. These seven do.
DELETE FROM policy_rule;

INSERT INTO agent_role (code, label, kind) VALUES
    ('care_coordinator',   'Care Coordinator',     'agent'),
    ('referral_intake',    'Referral Intake',      'agent'),
    ('coverage_network',   'Coverage & Network',   'agent'),
    ('provider_matching',  'Provider Matching',    'agent'),
    ('referral_readiness', 'Referral Readiness',   'agent'),
    ('scheduling',         'Scheduling',           'agent'),
    ('recovery_handoff',   'Recovery & Handoff',   'agent'),
    ('operator',           'Human operator',       'human'),
    ('system',             'Platform system',      'system')
ON CONFLICT (code) DO UPDATE SET label = EXCLUDED.label, kind = EXCLUDED.kind;

-- ---------------------------------------------------------------------------
-- 5. Tools — the tools §3 actually names
-- ---------------------------------------------------------------------------
INSERT INTO tool_registry (name, label, writes_control_state) VALUES
    -- Referral Intake (§3)
    ('validate_referral',        'Validate referral',              false),
    ('resolve_location',         'Resolve patient location',       false),
    ('patient_preferences',      'Capture patient preferences',    false),
    -- Coverage & Network (§3)
    ('check_plan',               'Check plan',                     false),
    ('check_provider_network',   'Check provider network status',  false),
    ('get_coverage_rules',       'Get coverage rules',             false),
    -- Provider Matching (§3) — ranking and distance run in code, not as a tool
    ('find_candidates',          'Build the provider candidate pool', false),
    ('search_directory',         'Search the directory (hybrid RAG)',  false),
    -- Referral Readiness (§3)
    ('get_referral_requirements', 'Get referral requirements',     false),
    ('inspect_document',         'Inspect a document',             false),
    ('validate_referral_package', 'Validate the referral package', false),
    -- Scheduling (§3) — sole holder of booking:write
    ('list_slots',               'List appointment slots',         false),
    ('create_booking',           'Create booking request',         true),
    ('lookup_request',           'Look up a booking request',      false),
    ('cancel_booking',           'Cancel an appointment',          true),
    ('reschedule_booking',       'Reschedule an appointment',      true),
    -- Recovery & Handoff (§3)
    ('classify_error',           'Classify an error',              false),
    ('create_handoff',           'Create a human handoff',         true),
    -- Human / system
    ('resolve_ticket',           'Resolve a human ticket',         true),
    ('capture_consent',          'Record patient consent',         true),
    ('await_patient_choice',     'Await a patient choice',         false),
    ('delegate_task',            'Delegate to a specialist',       false)
ON CONFLICT (name) DO UPDATE
    SET label = EXCLUDED.label, writes_control_state = EXCLUDED.writes_control_state;

-- ---------------------------------------------------------------------------
-- 6. Policy — re-derived from §3 and §4
-- ---------------------------------------------------------------------------
-- Default deny is unchanged: an absent row is a refusal. Every row below is a
-- decision someone can read and argue with.
INSERT INTO policy_rule (agent_role, tool_name, workflow_state, allowed, max_calls, note) VALUES
    -- ── Care Coordinator: routes, and cannot do the specialists' work ──────
    ('care_coordinator', 'delegate_task', 'INTAKE',                   true, NULL, NULL),
    ('care_coordinator', 'delegate_task', 'COVERAGE_CHECK',           true, NULL, NULL),
    ('care_coordinator', 'delegate_task', 'PROVIDER_MATCHING',        true, NULL, NULL),
    ('care_coordinator', 'delegate_task', 'READINESS_CHECK',          true, NULL, NULL),
    ('care_coordinator', 'delegate_task', 'RECOVERY',                 true, NULL, NULL),
    -- §3: the coordinator cannot write bookings or change a patient's choice.
    ('care_coordinator', 'create_booking', 'BOOKING_IN_FLIGHT',       false, NULL, '§3: the Care Coordinator cannot write a booking.'),

    -- ── Referral Intake ───────────────────────────────────────────────────
    ('referral_intake', 'validate_referral',    'INTAKE',             true, NULL, NULL),
    ('referral_intake', 'resolve_location',     'INTAKE',             true, NULL, NULL),
    ('referral_intake', 'patient_preferences',  'INTAKE',             true, NULL, NULL),
    ('referral_intake', 'validate_referral',    'HUMAN_HANDOFF',      true, NULL, 'Re-triage after an operator resolves a ticket.'),
    ('referral_intake', 'create_booking',       'INTAKE',             false, NULL, '§3: no booking exists before the flow reaches it.'),

    -- ── Coverage & Network ────────────────────────────────────────────────
    ('coverage_network', 'check_plan',          'COVERAGE_CHECK',     true, NULL, '§4: plan-level rules run in parallel with matching.'),
    ('coverage_network', 'get_coverage_rules',  'COVERAGE_CHECK',     true, NULL, NULL),
    ('coverage_network', 'check_provider_network', 'AWAITING_DOCTOR_CHOICE', true, NULL,
        '§4: the doctor-level network check happens once the doctor is known, and only then.'),
    ('coverage_network', 'check_provider_network', 'COVERAGE_CHECK',  false, NULL,
        '§4: a provider-level check before a provider is chosen has no subject.'),

    -- ── Provider Matching ─────────────────────────────────────────────────
    ('provider_matching', 'find_candidates',   'PROVIDER_MATCHING',   true, NULL, NULL),
    ('provider_matching', 'search_directory',  'PROVIDER_MATCHING',   true, NULL, '§10 hybrid RAG; distance is computed in code, never from a score.'),
    ('provider_matching', 'find_candidates',   'RECOVERY',            true, NULL, '§2.4: recovery may widen the search after evidence.'),
    ('provider_matching', 'create_booking',    'PROVIDER_MATCHING',   false, NULL, '§3: Matching cannot book, and cannot choose for the patient.'),

    -- ── Referral Readiness ────────────────────────────────────────────────
    ('referral_readiness', 'get_referral_requirements', 'READINESS_CHECK', true, NULL, NULL),
    ('referral_readiness', 'inspect_document',          'READINESS_CHECK', true, NULL, NULL),
    ('referral_readiness', 'validate_referral_package', 'READINESS_CHECK', true, NULL, NULL),
    ('referral_readiness', 'validate_referral_package', 'HUMAN_HANDOFF',   true, NULL, 'Re-check after a missing document is supplied.'),
    -- §3: Readiness records readiness; it never grants clinical or admin approval.

    -- ── Scheduling: sole holder of booking:write (§3, §9) ─────────────────
    ('scheduling', 'list_slots',      'AWAITING_SLOT_CHOICE',    true,  NULL, NULL),
    ('scheduling', 'list_slots',      'AWAITING_FINAL_APPROVAL', true,  NULL, 'Re-read the slot before taking consent, so the approved payload is current.'),
    ('scheduling', 'list_slots',      'BOOKING_IN_FLIGHT',       false, NULL,
        'A booking is already in flight. Searching again here is how a second booking gets started.'),
    ('scheduling', 'list_slots',      'BOOKED',                  true,  NULL, '§2.5 reschedule: offer alternatives before touching the existing booking.'),
    ('scheduling', 'create_booking',  'BOOKING_IN_FLIGHT',       true,  1,
        'One attempt per epoch. A retry is a new policy decision, not a loop.'),
    ('scheduling', 'create_booking',  'AWAITING_FINAL_APPROVAL', false, NULL,
        '§9: booking requires a live approval bound to the payload. Asking for consent is not consent.'),
    ('scheduling', 'lookup_request',  'BOOKING_IN_FLIGHT',       true,  NULL, NULL),
    ('scheduling', 'lookup_request',  'BOOKING_UNVERIFIED',      true,  NULL, '§7: reconcile with the SAME key.'),
    ('scheduling', 'cancel_booking',  'BOOKED',                  true,  NULL, NULL),
    ('scheduling', 'reschedule_booking', 'BOOKED',               true,  NULL, '§2.5: the old booking is cancelled only after the new one is verified.'),

    -- ── Recovery & Handoff ────────────────────────────────────────────────
    ('recovery_handoff', 'classify_error',     'RECOVERY',        true, NULL, '§3: Recovery classifies the failure before choosing an action.'),
    ('recovery_handoff', 'search_directory',   'RECOVERY',        true, NULL, '§10: the deterministic seed fallback is reachable here.'),
    ('recovery_handoff', 'create_handoff',     'RECOVERY',        true, NULL, NULL),
    ('recovery_handoff', 'create_handoff',     'HUMAN_HANDOFF',   true, NULL, NULL),
    ('recovery_handoff', 'create_handoff',     'BOOKING_UNVERIFIED', true, NULL, '§7: an unresolvable reconciliation escalates.'),
    ('recovery_handoff', 'create_booking',     'RECOVERY',        false, NULL, '§3: Recovery has bounded actions; booking is not one.'),

    -- ── Human operator (§11) ──────────────────────────────────────────────
    ('operator', 'resolve_ticket',   'HUMAN_HANDOFF',  true,  NULL, 'Resolving a ticket is not consent (011).'),
    ('operator', 'create_booking',   'HUMAN_HANDOFF',  false, NULL, '§11: an operator action is never booking consent.'),
    ('operator', 'capture_consent',  'HUMAN_HANDOFF',  false, NULL, '§9: consent comes from the patient, not from the queue.'),

    -- ── Platform system ───────────────────────────────────────────────────
    ('system', 'await_patient_choice', 'AWAITING_HOSPITAL_CHOICE', true, NULL, NULL),
    ('system', 'await_patient_choice', 'AWAITING_DOCTOR_CHOICE',   true, NULL, NULL),
    ('system', 'await_patient_choice', 'AWAITING_SLOT_CHOICE',     true, NULL, NULL),
    ('system', 'capture_consent',      'AWAITING_FINAL_APPROVAL',  true, NULL, '§9: records the patient''s explicit approval against the current payload hash.'),
    ('system', 'capture_consent',      'BOOKING_IN_FLIGHT',        false, NULL, 'Consent is captured before booking, not during.');

-- ---------------------------------------------------------------------------
-- 7. Carry the consent guard onto the renamed states
-- ---------------------------------------------------------------------------
-- Logic is unchanged from 007 except for the two literals. It is restated in
-- full rather than patched because CREATE OR REPLACE FUNCTION takes a whole
-- body, and a diff of this against 007 should read as "same rule, new names".
CREATE OR REPLACE FUNCTION workflow_booking_consent_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_hash text;
BEGIN
    IF NEW.state = OLD.state THEN
        RETURN NEW;
    END IF;

    IF NEW.state = 'BOOKING_IN_FLIGHT' THEN
        v_hash := workflow_booking_payload_hash(NEW);

        IF v_hash IS NULL THEN
            RAISE EXCEPTION 'workflow % cannot enter BOOKING_IN_FLIGHT without a booking_payload_hash on payload',
                NEW.id
                USING ERRCODE = 'RE004';
        END IF;

        IF NOT approval_is_live(NEW.id, NEW.patient_ref, OLD.state_version, v_hash) THEN
            RAISE EXCEPTION
                'workflow % cannot enter BOOKING_IN_FLIGHT: no live approval bound to state_version % and payload hash %',
                NEW.id, OLD.state_version, v_hash
                USING ERRCODE = 'RE004',
                      HINT = 'Either the patient has not approved, the approval expired, it was already consumed, or the payload changed after approval.';
        END IF;
    END IF;

    -- §7: an unverified outcome is resolved by reconciliation, not by asserting
    -- a booking. Both `BOOKED` and `BOOKING_UNVERIFIED` are reachable from
    -- BOOKING_IN_FLIGHT, and only the former claims a confirmed request.
    IF NEW.state = 'BOOKED' THEN
        IF NOT EXISTS (
            SELECT 1
              FROM booking_request b
             WHERE b.workflow_id = NEW.id
               AND b.state = 'confirmed'
        ) THEN
            RAISE EXCEPTION 'workflow % cannot be marked BOOKED: no confirmed booking_request exists', NEW.id
                USING ERRCODE = 'RE004';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION workflow_booking_consent_guard IS
    'Second BEFORE UPDATE guard on workflow. Makes BOOKING_IN_FLIGHT unreachable without live patient consent, and BOOKED unreachable without a confirmed booking request.';

-- ---------------------------------------------------------------------------
-- 8. Drop what is now unused
-- ---------------------------------------------------------------------------
-- Only after workflow rows have been moved (step 2) and policy rows deleted
-- (step 4), so no foreign key is violated on the way out.
DELETE FROM workflow_state
 WHERE code NOT IN (
    'INTAKE','COVERAGE_CHECK','PROVIDER_MATCHING','AWAITING_HOSPITAL_CHOICE',
    'AWAITING_DOCTOR_CHOICE','READINESS_CHECK','AWAITING_SLOT_CHOICE',
    'AWAITING_FINAL_APPROVAL','BOOKING_IN_FLIGHT','BOOKED','BOOKING_UNVERIFIED',
    'RECOVERY','HUMAN_HANDOFF','ABANDONED','CANCELLED','FAILED'
 );

DELETE FROM agent_role
 WHERE code NOT IN (
    'care_coordinator','referral_intake','coverage_network','provider_matching',
    'referral_readiness','scheduling','recovery_handoff','operator','system'
 );

DELETE FROM tool_registry
 WHERE name NOT IN (
    'validate_referral','resolve_location','patient_preferences','check_plan',
    'check_provider_network','get_coverage_rules','find_candidates','search_directory',
    'get_referral_requirements','inspect_document','validate_referral_package',
    'list_slots','create_booking','lookup_request','cancel_booking','reschedule_booking',
    'classify_error','create_handoff','resolve_ticket','capture_consent',
    'await_patient_choice','delegate_task'
 );

-- ---------------------------------------------------------------------------
-- 9. Assert the result
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_states integer;
    v_roles  integer;
    v_hard   integer;
BEGIN
    SELECT count(*) INTO v_states FROM workflow_state;
    IF v_states <> 16 THEN
        RAISE EXCEPTION 'expected 16 workflow states after alignment, found %', v_states;
    END IF;

    SELECT count(*) INTO v_roles FROM agent_role WHERE kind = 'agent';
    IF v_roles <> 7 THEN
        RAISE EXCEPTION 'expected the 7 specialist roles from §3, found %', v_roles;
    END IF;

    -- The two rows 005 could not express. If either reappears, the vocabulary
    -- has drifted back to being too coarse to carry §4's policy.
    SELECT count(*) INTO v_hard FROM policy_rule
     WHERE agent_role = 'scheduling' AND tool_name = 'list_slots'
       AND workflow_state = 'BOOKING_IN_FLIGHT' AND allowed;
    IF v_hard <> 0 THEN
        RAISE EXCEPTION 'list_slots must not be permitted while a booking is in flight';
    END IF;

    SELECT count(*) INTO v_hard FROM policy_rule
     WHERE agent_role = 'scheduling' AND tool_name = 'create_booking'
       AND workflow_state = 'AWAITING_FINAL_APPROVAL' AND allowed;
    IF v_hard <> 0 THEN
        RAISE EXCEPTION 'create_booking must not be permitted before consent is recorded';
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- 10. Two guards that gate on a state NAME, not just on a message
-- ---------------------------------------------------------------------------
-- These are functional changes, not cosmetic. `approval_guard` decides whether
-- an approval may be recorded *by checking the workflow's state*, and
-- `handoff_resolve` bounds an operator's return state with an allow-list. Both
-- named states from the old vocabulary, so both are restated here. Everything
-- else about them is unchanged from 007 and 011 respectively — a diff should
-- read as "same rule, new names".

-- An approval may only be recorded while the workflow is waiting for one (007).
CREATE OR REPLACE FUNCTION approval_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_workflow workflow;
    v_evidence evidence;
BEGIN
    -- (4) Operators cannot mint consent.
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

    IF v_workflow.state <> 'AWAITING_FINAL_APPROVAL' THEN
        RAISE EXCEPTION 'approval refused: workflow % is in state %, not AWAITING_FINAL_APPROVAL',
            NEW.workflow_id, v_workflow.state
            USING ERRCODE = 'RE004',
                  HINT = 'An approval is recorded while the workflow waits for it; anything else is a stale intent.';
    END IF;

    IF NEW.state_version <> v_workflow.state_version THEN
        RAISE EXCEPTION 'approval refused: bound to state_version % but workflow % is at %',
            NEW.state_version, NEW.workflow_id, v_workflow.state_version
            USING ERRCODE = 'RE004';
    END IF;

    -- Evidence must exist, be authoritative, and still be fresh.
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

COMMENT ON FUNCTION approval_guard IS
    'Approval is refused unless the workflow is in AWAITING_FINAL_APPROVAL, the patient matches, the state_version is current, and the consent evidence is authoritative and fresh.';

-- An operator's return state is bounded by an allow-list (§11).
DROP FUNCTION IF EXISTS handoff_resolve(uuid, text, bigint, text, text, text);

CREATE OR REPLACE FUNCTION handoff_resolve(
    p_handoff_id          uuid,
    p_operator_id         text,
    p_expected_generation bigint,
    p_resolution          text,
    p_workflow_state      text DEFAULT 'PROVIDER_MATCHING',
    p_note                text DEFAULT NULL
) RETURNS handoff
LANGUAGE plpgsql AS $$
DECLARE
    v_handoff       handoff;
    v_prev_actor    text;
    v_workflow      workflow;
BEGIN
    -- The states an operator may return a workflow to: places where the normal
    -- flow can resume, plus the terminals. The pre-booking states are
    -- deliberately absent. Resolving a ticket is not booking consent, so an
    -- operator can send the workflow back to search or readiness but cannot put
    -- it on the threshold of a booking.
    IF p_workflow_state NOT IN (
        'INTAKE', 'COVERAGE_CHECK', 'PROVIDER_MATCHING', 'READINESS_CHECK',
        'AWAITING_SLOT_CHOICE', 'RECOVERY', 'FAILED', 'CANCELLED', 'ABANDONED'
    ) THEN
        RAISE EXCEPTION
            'operator resolution cannot move a workflow to % (allowed: INTAKE, COVERAGE_CHECK, PROVIDER_MATCHING, READINESS_CHECK, AWAITING_SLOT_CHOICE, RECOVERY, FAILED, CANCELLED, ABANDONED)',
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

    -- Attribute this transaction to the operator, then restore the context.
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
    'Closes a ticket, releases human ownership and returns the workflow to an allow-listed state. It cannot grant booking consent: no pre-booking state is reachable from here.';
