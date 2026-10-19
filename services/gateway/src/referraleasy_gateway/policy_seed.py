"""The policy matrix (§4) as the offline profile carries it: data, not code.

A copy of the `policy_rule` rows `migrations/014_align_domain_vocabulary.sql` seeds, in the order the
database is read (role, tool, state, allowed, note). Two reasons this exists as a transcription
rather than as a query:

* the **memory** control-state profile has no database to ask, and the gateway must still be able to
  answer "may this action run in this state" without one;
* a copy that is allowed to drift silently would be a second policy, and the second one is always
  the one that is wrong.

So the drift is not allowed to be silent: `tests/gateway/test_policy_table.py` parses the `INSERT`
block out of migration 014 and fails if a row here is not there, naming the row. Adding a rule to the
migration does not break it; changing or deleting one does, and the message says to re-copy.

`max_calls` is deliberately not carried. It bounds how often a tool may be called within one
coordinator turn (§8), which is the turn budget's job, and the gateway does not count calls per turn.

Row shape: `(agent_role, tool_name, workflow_state, allowed, note)`.
"""

from __future__ import annotations

from typing import Final

__all__ = ["POLICY_SEED_SOURCE", "SEED_ROWS"]

#: Where the rows below come from, quoted in the drift test's failure message.
POLICY_SEED_SOURCE: Final = "migrations/014_align_domain_vocabulary.sql"

#: `(agent_role, tool_name, workflow_state, allowed, note)`.
COORDINATION_ROWS: Final[tuple[tuple[str, str, str, bool, str | None], ...]] = (
    (
        "system",
        "capture_consent",
        "HUMAN_HANDOFF",
        True,
        "Approve only the displayed handoff summary.",
    ),
    (
        "coverage_network",
        "check_plan",
        "PROVIDER_MATCHING",
        True,
        "Plan coverage is independent of provider matching.",
    ),
    (
        "care_coordinator",
        "delegate_task",
        "AWAITING_DOCTOR_CHOICE",
        True,
        "Check the network after the patient chooses a doctor.",
    ),
    (
        "care_coordinator",
        "delegate_task",
        "AWAITING_SLOT_CHOICE",
        True,
        "Validate the chosen slot before review.",
    ),
    (
        "care_coordinator",
        "delegate_task",
        "BOOKING_IN_FLIGHT",
        True,
        "Delegate the approved booking to Scheduling.",
    ),
    (
        "care_coordinator",
        "delegate_task",
        "BOOKING_UNVERIFIED",
        True,
        "Reconcile the original booking key.",
    ),
    ("scheduling", "list_slots", "READINESS_CHECK", True, "Read slots after readiness succeeds."),
)

SEED_ROWS: Final[tuple[tuple[str, str, str, bool, str | None], ...]] = (
    (
        "care_coordinator",
        "create_booking",
        "BOOKING_IN_FLIGHT",
        False,
        "§3: the Care Coordinator cannot write a booking.",
    ),
    ("care_coordinator", "delegate_task", "COVERAGE_CHECK", True, None),
    ("care_coordinator", "delegate_task", "INTAKE", True, None),
    ("care_coordinator", "delegate_task", "PROVIDER_MATCHING", True, None),
    ("care_coordinator", "delegate_task", "READINESS_CHECK", True, None),
    ("care_coordinator", "delegate_task", "RECOVERY", True, None),
    (
        "coverage_network",
        "check_plan",
        "COVERAGE_CHECK",
        True,
        "§4: plan-level rules run in parallel with matching.",
    ),
    (
        "coverage_network",
        "check_provider_network",
        "AWAITING_DOCTOR_CHOICE",
        True,
        "§4: the doctor-level network check happens once the doctor is known, and only then.",
    ),
    (
        "coverage_network",
        "check_provider_network",
        "COVERAGE_CHECK",
        False,
        "§4: a provider-level check before a provider is chosen has no subject.",
    ),
    ("coverage_network", "get_coverage_rules", "COVERAGE_CHECK", True, None),
    (
        "operator",
        "capture_consent",
        "HUMAN_HANDOFF",
        False,
        "§9: consent comes from the patient, not from the queue.",
    ),
    (
        "operator",
        "create_booking",
        "HUMAN_HANDOFF",
        False,
        "§11: an operator action is never booking consent.",
    ),
    (
        "operator",
        "resolve_ticket",
        "HUMAN_HANDOFF",
        True,
        "Resolving a ticket is not consent (011).",
    ),
    (
        "provider_matching",
        "create_booking",
        "PROVIDER_MATCHING",
        False,
        "§3: Matching cannot book, and cannot choose for the patient.",
    ),
    ("provider_matching", "find_candidates", "PROVIDER_MATCHING", True, None),
    (
        "provider_matching",
        "find_candidates",
        "RECOVERY",
        True,
        "§2.4: recovery may widen the search after evidence.",
    ),
    (
        "provider_matching",
        "search_directory",
        "PROVIDER_MATCHING",
        True,
        "§10 hybrid RAG; distance is computed in code, never from a score.",
    ),
    (
        "recovery_handoff",
        "classify_error",
        "RECOVERY",
        True,
        "§3: Recovery classifies the failure before choosing an action.",
    ),
    (
        "recovery_handoff",
        "create_booking",
        "RECOVERY",
        False,
        "§3: Recovery has bounded actions; booking is not one.",
    ),
    (
        "recovery_handoff",
        "create_handoff",
        "BOOKING_UNVERIFIED",
        True,
        "§7: an unresolvable reconciliation escalates.",
    ),
    ("recovery_handoff", "create_handoff", "HUMAN_HANDOFF", True, None),
    ("recovery_handoff", "create_handoff", "RECOVERY", True, None),
    (
        "recovery_handoff",
        "search_directory",
        "RECOVERY",
        True,
        "§10: the deterministic seed fallback is reachable here.",
    ),
    (
        "referral_intake",
        "create_booking",
        "INTAKE",
        False,
        "§3: no booking exists before the flow reaches it.",
    ),
    ("referral_intake", "patient_preferences", "INTAKE", True, None),
    ("referral_intake", "resolve_location", "INTAKE", True, None),
    (
        "referral_intake",
        "validate_referral",
        "HUMAN_HANDOFF",
        True,
        "Re-triage after an operator resolves a ticket.",
    ),
    ("referral_intake", "validate_referral", "INTAKE", True, None),
    ("referral_readiness", "get_referral_requirements", "READINESS_CHECK", True, None),
    ("referral_readiness", "inspect_document", "READINESS_CHECK", True, None),
    (
        "referral_readiness",
        "validate_referral_package",
        "HUMAN_HANDOFF",
        True,
        "Re-check after a missing document is supplied.",
    ),
    ("referral_readiness", "validate_referral_package", "READINESS_CHECK", True, None),
    ("scheduling", "cancel_booking", "BOOKED", True, None),
    (
        "scheduling",
        "create_booking",
        "AWAITING_FINAL_APPROVAL",
        False,
        "§9: booking requires a live approval bound to the payload. Asking for consent is not consent.",
    ),
    (
        "scheduling",
        "create_booking",
        "BOOKING_IN_FLIGHT",
        True,
        "One attempt per epoch. A retry is a new policy decision, not a loop.",
    ),
    (
        "scheduling",
        "list_slots",
        "AWAITING_FINAL_APPROVAL",
        True,
        "Re-read the slot before taking consent, so the approved payload is current.",
    ),
    ("scheduling", "list_slots", "AWAITING_SLOT_CHOICE", True, None),
    (
        "scheduling",
        "list_slots",
        "BOOKED",
        True,
        "§2.5 reschedule: offer alternatives before touching the existing booking.",
    ),
    (
        "scheduling",
        "list_slots",
        "BOOKING_IN_FLIGHT",
        False,
        "A booking is already in flight. Searching again here is how a second booking gets started.",
    ),
    ("scheduling", "lookup_request", "BOOKING_IN_FLIGHT", True, None),
    (
        "scheduling",
        "lookup_request",
        "BOOKING_UNVERIFIED",
        True,
        "§7: reconcile with the SAME key.",
    ),
    (
        "scheduling",
        "reschedule_booking",
        "BOOKED",
        True,
        "§2.5: the old booking is cancelled only after the new one is verified.",
    ),
    ("system", "await_patient_choice", "AWAITING_DOCTOR_CHOICE", True, None),
    ("system", "await_patient_choice", "AWAITING_HOSPITAL_CHOICE", True, None),
    ("system", "await_patient_choice", "AWAITING_SLOT_CHOICE", True, None),
    (
        "system",
        "capture_consent",
        "AWAITING_FINAL_APPROVAL",
        True,
        "§9: records the patient's explicit approval against the current payload hash.",
    ),
    (
        "system",
        "capture_consent",
        "BOOKING_IN_FLIGHT",
        False,
        "Consent is captured before booking, not during.",
    ),
    *COORDINATION_ROWS,
)
