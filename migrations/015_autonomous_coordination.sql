-- Read permissions for the standard coordination plan. Booking grants do not change.
INSERT INTO policy_rule (agent_role, tool_name, workflow_state, allowed, max_calls, note) VALUES
    ('coverage_network', 'check_plan', 'PROVIDER_MATCHING', true, NULL, 'Plan coverage is independent of provider matching.'),
    ('care_coordinator', 'delegate_task', 'AWAITING_DOCTOR_CHOICE', true, NULL, 'Check the network after the patient chooses a doctor.'),
    ('care_coordinator', 'delegate_task', 'AWAITING_SLOT_CHOICE', true, NULL, 'Validate the chosen slot before review.'),
    ('care_coordinator', 'delegate_task', 'BOOKING_IN_FLIGHT', true, NULL, 'Delegate the approved booking to Scheduling.'),
    ('care_coordinator', 'delegate_task', 'BOOKING_UNVERIFIED', true, NULL, 'Reconcile the original booking key.'),
    ('scheduling', 'list_slots', 'READINESS_CHECK', true, NULL, 'Read slots after readiness succeeds.')
ON CONFLICT (agent_role, tool_name, workflow_state) DO NOTHING;
