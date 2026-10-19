"""Recovery's rules, in code: the one-way door, and messages no model rewrites (§7, §8).

Two decisions live here because both are about *counts and wording* rather than
about judgement:

**The one-way door (§8).** The third transient failure at the same specialist
closes the normal path: the flow does not call that specialist again, and the move
to `RECOVERY` is one-way. That is arithmetic over a per-workflow counter, so it is
implemented as arithmetic. :class:`FailureLedger` counts consecutive transient
failures per role inside one workflow and answers :meth:`FailureLedger.may_retry`;
once :meth:`FailureLedger.enter_recovery` has been called for a role, the answer is
`False` for the rest of that workflow — not "for now", and not re-openable by
asking.

**The message is not a model's (§7).** *"No LLM call is made just to reword a
failure message."* §7 says it, so the messages are constants here, chosen by failure
class, and `tools/handoff.py` uses them as they are. A turn that produced a failure
still produces a patient sentence, and that sentence is composed by a dict lookup —
which also makes it reviewable and translatable, two things a generated sentence is
not.

The ledger is deliberately separate from `TurnBudget`. The budget counts what one
turn may spend; this counts what the *workflow* has already spent on a specialist
across turns, which is the count §8 is about.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from referraleasy_agent_runtime import AgentRole
from referraleasy_agents.errors import OneWayDoorRefused

__all__ = [
    "FAILURE_CLASSES",
    "PATIENT_MESSAGES",
    "TRANSIENT_ATTEMPT_LIMIT",
    "FailureLedger",
    "classify_failure",
    "one_way_door_reached",
    "patient_message",
]

#: §8's threshold: the third transient failure is the one-way door. Expressed as a
#: count of *failures already recorded*, so a specialist is called at most three
#: times: the call after the first and second failures is still normal, and the
#: third failure ends it.
TRANSIENT_ATTEMPT_LIMIT: Final = 3

#: The classes a failure is sorted into. Not free text: each one drives a different
#: plan, and a class that only labels would be a class nobody acts on.
FAILURE_CLASSES: Final = (
    "transient",
    "rejected",
    "policy_blocked",
    "guardrail_blocked",
    "data_missing",
    "document_missing",
    "document_unreadable",
    "coverage_unknown",
    "no_match",
    "booking_unknown",
    "unknown",
)

#: §7: a failure's patient-facing sentence, by class. Written to be true when
#: nothing is known and never to promise a next step the system cannot take.
PATIENT_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "transient": (
            "We are still working on your referral. One of the services we use is slow to answer, "
            "so this step is taking longer than usual. We will continue automatically."
        ),
        "rejected": (
            "One of the services we use could not accept this request as it stands. We are checking "
            "what has to change before we can continue."
        ),
        "policy_blocked": (
            "This step needs approval that has not been given yet, so we have stopped short of it "
            "rather than assume it. Nothing has been booked and nothing has been sent on your behalf."
        ),
        "guardrail_blocked": (
            "We stopped before completing this step because something in the request did not pass "
            "our own checks. A person from our team is looking at it."
        ),
        "data_missing": (
            "We are missing some information needed to continue. We have noted exactly what is "
            "missing and will ask for it rather than guess."
        ),
        "document_missing": (
            "There is a document the hospital needs that we do not have yet. Your referral is not "
            "blocked for good; it needs that document to move on."
        ),
        "document_unreadable": (
            "A document you sent could not be read. Please send it again in a clearer form, and we "
            "will continue from where we stopped."
        ),
        "coverage_unknown": (
            "We could not confirm how your plan treats this visit. Nothing has been booked, and a "
            "person from our team will check it with you."
        ),
        "no_match": (
            "We could not find a provider who fits everything you asked for. That is an answer, not "
            "an error: we will show you what could change, and you decide."
        ),
        "booking_unknown": (
            "We sent your booking request and did not receive a clear reply. Nothing is confirmed "
            "either way. We are checking with the hospital, and we will not send a second request."
        ),
        "unknown": (
            "Something went wrong while handling your referral and we could not work out what. "
            "Nothing has been booked. A person from our team will take this over."
        ),
    }
)


def classify_failure(
    *,
    error_class: str,
    reason_code: str | None = None,
    transient_attempts: int = 0,
) -> str:
    """Sort a failure into one of :data:`FAILURE_CLASSES`, in code.

    `reason_code` wins over `error_class` when both are present: the reason code is
    the controlled vocabulary the rest of the platform speaks, and it is more
    specific than a Python class name. `error_class` is the fallback for a failure
    that arrived without one — a crash, or a peer that sent nothing shaped.
    """
    by_reason: Mapping[str, str] = {
        "budget_exhausted": "transient",
        "deadline_exceeded": "transient",
        "upstream_timeout": "transient",
        "upstream_error": "transient",
        "upstream_rejected": "rejected",
        "policy_blocked": "policy_blocked",
        "guardrail_blocked": "guardrail_blocked",
        "missing_mandatory_field": "data_missing",
        "ambiguous_input": "data_missing",
        "conflicting_evidence": "data_missing",
        "stale_evidence": "data_missing",
        "document_missing": "document_missing",
        "document_unreadable": "document_unreadable",
        "coverage_unknown": "coverage_unknown",
        "provider_not_in_network": "coverage_unknown",
        "no_candidate_matched": "no_match",
        "no_slot_available": "no_match",
        "hard_constraint_unsatisfiable": "no_match",
        "booking_outcome_unknown": "booking_unknown",
        "approval_expired": "policy_blocked",
        "approval_payload_changed": "policy_blocked",
        "human_owned": "policy_blocked",
    }
    if reason_code and reason_code in by_reason:
        return by_reason[reason_code]
    lowered = error_class.casefold()
    for token, classification in (
        ("transient", "transient"),
        ("timeout", "transient"),
        ("budget", "transient"),
        ("rejected", "rejected"),
        ("policy", "policy_blocked"),
        ("guardrail", "guardrail_blocked"),
        ("unreadable", "document_unreadable"),
        ("document", "document_missing"),
        ("coverage", "coverage_unknown"),
        ("unknown", "booking_unknown"),
        ("missing", "data_missing"),
    ):
        if token in lowered:
            return classification
    # A caller that supplies neither a known reason code nor a recognisable class
    # gets `unknown`, which routes to a human. Guessing a class here would hand a
    # mis-classified failure to an automatic path.
    return "transient" if transient_attempts > 0 else "unknown"


def one_way_door_reached(transient_attempts: int) -> bool:
    """Whether §8's threshold has been reached for this specialist."""
    return transient_attempts >= TRANSIENT_ATTEMPT_LIMIT


def patient_message(failure_class: str) -> str:
    """The failure's sentence, from the table. No model is asked to write one (§7)."""
    return PATIENT_MESSAGES.get(failure_class, PATIENT_MESSAGES["unknown"])


@dataclass(slots=True)
class FailureLedger:
    """Per-workflow failure counts, and the one-way door once it closes.

    Not persisted here: the workflow's durable record is the gateway's. This ledger
    is what a turn reasons with, and it is rebuilt from the workflow's history at
    the start of a turn — which is why :meth:`record` takes the *total* rather than
    incrementing a value nobody persisted.
    """

    workflow_id: str
    _counts: dict[str, int] = field(default_factory=dict)
    _closed: set[str] = field(default_factory=set)

    def record(self, role: AgentRole | str, *, transient_attempts: int) -> None:
        """Set the specialist's recorded transient-failure count for this workflow."""
        self._counts[str(role)] = max(self._counts.get(str(role), 0), int(transient_attempts))

    def attempts(self, role: AgentRole | str) -> int:
        return self._counts.get(str(role), 0)

    def enter_recovery(self, role: AgentRole | str) -> None:
        """Close the door for ``role`` in this workflow. One-way, by construction."""
        self._closed.add(str(role))

    def is_closed(self, role: AgentRole | str) -> bool:
        return str(role) in self._closed

    def may_retry(self, role: AgentRole | str) -> bool:
        """Whether the normal flow may call ``role`` again in this workflow."""
        if self.is_closed(role):
            return False
        return not one_way_door_reached(self.attempts(role))

    def refuse_if_closed(self, role: AgentRole | str) -> None:
        """Refuse a normal-flow call to ``role`` once §8's door has closed.

        §8's door is one-way: after the third transient failure the normal flow does
        not call that specialist again, and the workflow transitions to recovery.
        `may_retry` answers the question; this refuses it, so a caller that merely
        *forgot* to ask cannot delegate anyway. The transition itself belongs to the
        state machine (Phase 5); what this guarantees is that the recommendation
        cannot be produced and that a caller checking gets a refusal rather than a
        `False` it can ignore.
        """
        if not self.may_retry(role):
            raise OneWayDoorRefused(
                f"§8's door has closed for {role!s} in workflow {self.workflow_id!r}: after "
                f"{TRANSIENT_ATTEMPT_LIMIT} transient failures the normal flow does not call it again. "
                "The permitted next steps are recovery's bounded options."
            )
