-- 019_patient_documents.sql
-- The patient can attach a missing document during the readiness check (§2.3, scenario 4).
--
-- When a hospital requires a document the referral did not include, and the patient holds one of
-- that kind, the workflow asks the patient for it in READINESS_CHECK instead of handing off: a
-- coordinator is not needed for something the patient can do. The answer is an ordinary patient
-- choice (the documents offered on the card), so it is the same tool the three picker states use.
-- Attaching a document approves nothing: the booking still needs the patient's final approval.
INSERT INTO policy_rule (agent_role, tool_name, workflow_state, allowed, max_calls, note) VALUES
    ('system', 'await_patient_choice', 'READINESS_CHECK', true, NULL, 'Attach a document the readiness check found missing.')
ON CONFLICT (agent_role, tool_name, workflow_state) DO NOTHING;
