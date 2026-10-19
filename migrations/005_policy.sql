-- 005_policy.sql
-- Who may do what, in which state.
--
-- The threat this table answers: a coordinator agent that "decides" to call the
-- booking tool while the workflow is still in intake. A prompt cannot be a
-- boundary, so the boundary lives here, as data, and is consulted by a trigger
-- on every task insert.
--
-- The rule is DEFAULT DENY. An absent row is a refusal, not a permission. The
-- only way to widen the policy is to INSERT a row, which is an auditable,
-- reviewable act.

-- ---------------------------------------------------------------------------
-- Vocabulary
-- ---------------------------------------------------------------------------
CREATE TABLE agent_role (
    code  text PRIMARY KEY,
    label text NOT NULL,
    kind  text NOT NULL,
    CONSTRAINT agent_role_kind_known CHECK (kind IN ('agent', 'human', 'system')),
    CONSTRAINT agent_role_code_present CHECK (length(btrim(code)) > 0)
);

COMMENT ON TABLE agent_role IS
    'Actors that can be attributed on a session row. Kept as a table so policy_rule can be foreign-keyed to it.';

INSERT INTO agent_role (code, label, kind) VALUES
    ('intake_coordinator', 'Intake coordinator',      'agent'),
    ('triage_agent',       'Triage agent',            'agent'),
    ('consent_agent',      'Consent capture',         'agent'),
    ('booking_agent',      'Booking agent',           'agent'),
    ('notification_agent', 'Notification agent',      'agent'),
    ('operator',           'Human operator',          'human'),
    ('system',             'Platform system',         'system');

-- Tools are the operations a role may invoke. `writes_control_state` marks the
-- tools that can move a workflow; those are the ones policy cares about most.
CREATE TABLE tool_registry (
    name                text    PRIMARY KEY,
    label               text    NOT NULL,
    writes_control_state boolean NOT NULL DEFAULT false,
    CONSTRAINT tool_name_present CHECK (length(btrim(name)) > 0)
);

COMMENT ON TABLE tool_registry IS
    'Known tools. A task whose kind is not registered here cannot pass the policy gate.';

INSERT INTO tool_registry (name, label, writes_control_state) VALUES
    ('extract_referral',   'Extract referral fields',        false),
    ('classify_specialty', 'Classify specialty',             false),
    ('score_urgency',      'Score urgency',                  false),
    ('lookup_policy',      'Look up coverage policy',        false),
    ('capture_consent',    'Capture patient consent',        false),
    ('find_slots',         'Find appointment slots',         false),
    ('create_booking',     'Create booking request',         false),
    ('send_notification',  'Send notification',              false),
    ('escalate_to_human',  'Escalate to a human operator',   true),
    ('resolve_ticket',     'Resolve a human ticket',         true);

-- ---------------------------------------------------------------------------
-- Policy
-- ---------------------------------------------------------------------------
CREATE TABLE policy_rule (
    agent_role     text    NOT NULL REFERENCES agent_role (code) ON DELETE RESTRICT,
    tool_name      text    NOT NULL REFERENCES tool_registry (name) ON DELETE RESTRICT,
    workflow_state text    NOT NULL REFERENCES workflow_state (code) ON DELETE RESTRICT,
    allowed        boolean NOT NULL,
    max_calls      integer,
    note           text,

    PRIMARY KEY (agent_role, tool_name, workflow_state),

    CONSTRAINT policy_rule_max_calls_positive
        CHECK (max_calls IS NULL OR max_calls > 0)
);

COMMENT ON TABLE policy_rule IS
    'Explicit permission matrix: role x tool x workflow state. ABSENCE = DENY. A row with allowed=false is an explicit prohibition, used to document a decision that would otherwise be invisible.';
COMMENT ON COLUMN policy_rule.max_calls IS
    'Optional ceiling within one session. NULL means "no per-session limit"; it never means "unlimited states".';

-- ---------------------------------------------------------------------------
-- Default-deny evaluation
-- ---------------------------------------------------------------------------

-- True only for an explicit allowed=true row. Unknown roles, unknown tools and
-- unknown states all evaluate to false.
CREATE OR REPLACE FUNCTION policy_is_allowed(
    p_agent_role     text,
    p_tool_name      text,
    p_workflow_state text
) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT coalesce((
        SELECT r.allowed
          FROM policy_rule r
         WHERE r.agent_role     = p_agent_role
           AND r.tool_name      = p_tool_name
           AND r.workflow_state = p_workflow_state
    ), false)
$$;

COMMENT ON FUNCTION policy_is_allowed IS
    'Default-deny check. Returns false when no rule row exists; callers cannot exceed the matrix because the matrix is the only source of truth.';

-- Assertion wrapper used by the task gate. Raising (rather than returning a
-- status) is what makes this a constraint instead of an advisory call.
CREATE OR REPLACE FUNCTION policy_assert(
    p_agent_role     text,
    p_tool_name      text,
    p_workflow_state text
) RETURNS void
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF NOT policy_is_allowed(p_agent_role, p_tool_name, p_workflow_state) THEN
        RAISE EXCEPTION
            'policy denies role % calling tool % in workflow state %',
            coalesce(p_agent_role, '<none>'), p_tool_name, coalesce(p_workflow_state, '<none>')
            USING ERRCODE = 'RE006',
                  HINT = 'Policy is default-deny. Add an explicit policy_rule row to permit this, deliberately.';
    END IF;
END;
$$;

COMMENT ON FUNCTION policy_assert IS
    'Raises RE006 when the role/tool/state triple is not explicitly permitted. The coordinator cannot exceed policy_rule.';

-- ---------------------------------------------------------------------------
-- The gate: every task insert is checked against the live workflow state
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION task_policy_gate() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_role  text;
    v_state text;
BEGIN
    SELECT s.agent_role INTO v_role
      FROM session s
     WHERE s.id = NEW.session_id;

    SELECT w.state INTO v_state
      FROM workflow w
     WHERE w.id = NEW.workflow_id;

    -- task.kind names the tool being invoked; policy is evaluated against the
    -- workflow state as it is NOW, not as the caller last saw it.
    PERFORM policy_assert(v_role, NEW.kind, v_state);

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION task_policy_gate IS
    'BEFORE INSERT gate on task: refuses any task whose kind is not permitted for the session role in the workflow''s current state.';

CREATE TRIGGER task_policy_gate_trigger
    BEFORE INSERT ON task
    FOR EACH ROW EXECUTE FUNCTION task_policy_gate();

-- ---------------------------------------------------------------------------
-- Seed: the demo referral path, stated explicitly
-- ---------------------------------------------------------------------------
INSERT INTO policy_rule (agent_role, tool_name, workflow_state, allowed, max_calls, note) VALUES
    ('intake_coordinator', 'extract_referral',   'intake',            true, NULL, 'Read the inbound referral.'),
    ('intake_coordinator', 'classify_specialty', 'intake',            true, NULL, NULL),
    ('intake_coordinator', 'escalate_to_human',  'intake',            true, NULL, 'Coordinator may always hand off.'),

    ('triage_agent',       'score_urgency',      'triaged',           true, NULL, NULL),
    ('triage_agent',       'lookup_policy',      'triaged',           true, NULL, NULL),
    ('triage_agent',       'escalate_to_human',  'triaged',           true, NULL, NULL),

    ('consent_agent',      'capture_consent',    'awaiting_approval', true, NULL, 'Consent is captured only while the workflow waits for approval.'),

    ('booking_agent',      'find_slots',         'awaiting_approval', true, NULL, NULL),
    ('booking_agent',      'find_slots',         'booking_pending',   true, NULL, NULL),
    ('booking_agent',      'create_booking',     'booking_pending',   true, 1,    'One booking attempt per epoch; retries are a new policy decision.'),
    ('booking_agent',      'escalate_to_human',  'booking_pending',   true, NULL, NULL),

    ('notification_agent', 'send_notification',  'booked',            true, NULL, NULL),
    ('notification_agent', 'send_notification',  'resolved',          true, NULL, NULL),

    ('operator',           'resolve_ticket',     'human_required',    true, NULL, 'A human may resolve a ticket. Resolving is not consent (see 011).'),
    ('operator',           'escalate_to_human',  'human_required',    true, NULL, NULL),

    -- Explicit prohibitions. These rows are documentation: they record that the
    -- decision was made, and they would survive a careless INSERT...SELECT.
    ('operator',           'create_booking',     'human_required',    false, NULL, 'An operator action is never booking consent.'),
    ('operator',           'capture_consent',    'human_required',    false, NULL, 'Consent comes from the patient, not from the queue.'),
    ('intake_coordinator', 'create_booking',     'intake',            false, NULL, 'No booking before triage.');
