-- The patient can approve a handoff summary. This action does not approve a booking.
INSERT INTO policy_rule (agent_role, tool_name, workflow_state, allowed, max_calls, note) VALUES
    ('system', 'capture_consent', 'HUMAN_HANDOFF', true, NULL, 'Approve only the displayed handoff summary.')
ON CONFLICT (agent_role, tool_name, workflow_state) DO NOTHING;

CREATE INDEX workflow_coordination_pending_idx ON workflow (created_at)
    WHERE payload->'coordination_job'->>'pending' = 'true';
