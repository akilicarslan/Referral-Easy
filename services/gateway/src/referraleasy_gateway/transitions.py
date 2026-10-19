"""The legal state edges, for the profiles where the database is not the one holding them.

`workflow_transition` is a table in `migrations/014`, and **PostgreSQL is the authority**. The
gateway holds no copy of the state machine: `commands.py` says so, and this module says it again
louder, because a file called `transitions.py` in a service is exactly where a second state machine
would quietly grow.

So what is this for? One profile, and one only: `GATEWAY_CONTROL_BACKEND=memory`, the offline demo,
where there is no database to consult. That store is a *substitute* for the database's constraints, so
it has to carry the constraints — otherwise the memory profile accepts `INTAKE -> BOOKED`, and a demo
that walks the happy path through a state machine that is not one is worse than no demo. The edges
are transcribed from `migrations/014_align_domain_vocabulary.sql` verbatim, and
`tests/gateway/test_policy_table.py` re-parses the migration and fails if the two ever differ, which
is the only thing that keeps a transcription honest.

The other three rules that make a transition legal live in the same store and mirror the same
constraints: the state must be one of the sixteen (`CHECK`), it must not be a self-loop
(`workflow_transition_no_self_loop`), and the caller must present the current `state_version`
(RE002).

**Every edge is here, including the ones that leave a state the flow has finished with.** `BOOKED` is
`terminal = false` in the migration's `workflow_state` table precisely because §2.5 reschedules out of
it and cancels from it, and `HUMAN_HANDOFF` returns to a safe state under §11. A transcription that
stopped at the first of the migration's two sections would look complete and would refuse those moves
in the only profile that has no database to correct it — which is how this file was once wrong.
"""

from __future__ import annotations

from typing import Final

__all__ = ["LEGAL_EDGES", "TRANSITION_SOURCE", "is_legal_edge"]

#: Where these rows come from. Named in the module rather than only in a comment, so a reader (and
#: the drift test) knows which migration to look at when this file and reality disagree.
TRANSITION_SOURCE: Final = "migrations/014_align_domain_vocabulary.sql"

#: `(from_state, to_state)` pairs, transcribed from `INSERT INTO workflow_transition` in the migration
#: above. Order is the migration's, so a diff against it is readable.
LEGAL_EDGES: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("INTAKE", "COVERAGE_CHECK"),
        ("INTAKE", "PROVIDER_MATCHING"),
        ("INTAKE", "HUMAN_HANDOFF"),
        ("INTAKE", "FAILED"),
        ("INTAKE", "ABANDONED"),
        ("INTAKE", "CANCELLED"),
        ("COVERAGE_CHECK", "PROVIDER_MATCHING"),
        ("COVERAGE_CHECK", "HUMAN_HANDOFF"),
        ("COVERAGE_CHECK", "RECOVERY"),
        ("COVERAGE_CHECK", "FAILED"),
        ("COVERAGE_CHECK", "ABANDONED"),
        ("COVERAGE_CHECK", "CANCELLED"),
        ("PROVIDER_MATCHING", "AWAITING_HOSPITAL_CHOICE"),
        ("PROVIDER_MATCHING", "COVERAGE_CHECK"),
        ("PROVIDER_MATCHING", "HUMAN_HANDOFF"),
        ("PROVIDER_MATCHING", "RECOVERY"),
        ("PROVIDER_MATCHING", "FAILED"),
        ("PROVIDER_MATCHING", "ABANDONED"),
        ("PROVIDER_MATCHING", "CANCELLED"),
        ("AWAITING_HOSPITAL_CHOICE", "AWAITING_DOCTOR_CHOICE"),
        ("AWAITING_HOSPITAL_CHOICE", "PROVIDER_MATCHING"),
        ("AWAITING_HOSPITAL_CHOICE", "HUMAN_HANDOFF"),
        ("AWAITING_HOSPITAL_CHOICE", "ABANDONED"),
        ("AWAITING_HOSPITAL_CHOICE", "CANCELLED"),
        ("AWAITING_DOCTOR_CHOICE", "READINESS_CHECK"),
        ("AWAITING_DOCTOR_CHOICE", "AWAITING_SLOT_CHOICE"),
        ("AWAITING_DOCTOR_CHOICE", "AWAITING_HOSPITAL_CHOICE"),
        ("AWAITING_DOCTOR_CHOICE", "HUMAN_HANDOFF"),
        ("AWAITING_DOCTOR_CHOICE", "ABANDONED"),
        ("AWAITING_DOCTOR_CHOICE", "CANCELLED"),
        ("READINESS_CHECK", "AWAITING_SLOT_CHOICE"),
        ("READINESS_CHECK", "HUMAN_HANDOFF"),
        ("READINESS_CHECK", "RECOVERY"),
        ("READINESS_CHECK", "ABANDONED"),
        ("READINESS_CHECK", "CANCELLED"),
        ("AWAITING_SLOT_CHOICE", "AWAITING_FINAL_APPROVAL"),
        ("AWAITING_SLOT_CHOICE", "READINESS_CHECK"),
        ("AWAITING_SLOT_CHOICE", "HUMAN_HANDOFF"),
        ("AWAITING_SLOT_CHOICE", "ABANDONED"),
        ("AWAITING_SLOT_CHOICE", "CANCELLED"),
        ("AWAITING_FINAL_APPROVAL", "BOOKING_IN_FLIGHT"),
        ("AWAITING_FINAL_APPROVAL", "AWAITING_SLOT_CHOICE"),
        ("AWAITING_FINAL_APPROVAL", "HUMAN_HANDOFF"),
        ("AWAITING_FINAL_APPROVAL", "ABANDONED"),
        ("AWAITING_FINAL_APPROVAL", "CANCELLED"),
        ("BOOKING_IN_FLIGHT", "BOOKED"),
        ("BOOKING_IN_FLIGHT", "BOOKING_UNVERIFIED"),
        ("BOOKING_IN_FLIGHT", "RECOVERY"),
        ("BOOKING_IN_FLIGHT", "HUMAN_HANDOFF"),
        ("BOOKING_IN_FLIGHT", "FAILED"),
        ("BOOKING_IN_FLIGHT", "ABANDONED"),
        ("BOOKING_UNVERIFIED", "BOOKED"),
        # §7 resolves an unknown booking outcome by reconciliation, and this is the edge for it — legal
        # here, and **unreachable in the postgres profile without fresh consent**: 014's consent guard
        # demands an approval live at the version the workflow is on, and the booking that produced the
        # uncertainty spent the one that authorised it, so a second attempt at the same slot needs a new
        # approval. The memory profile has no liveness check at this transition and allows the move; the
        # asymmetry is asserted from the database's side by `test_reconciliation_needs_a_live_approval_
        # and_a_spent_one_is_not_it` in `tests/gateway/test_postgres_control_state.py`.
        ("BOOKING_UNVERIFIED", "BOOKING_IN_FLIGHT"),
        ("BOOKING_UNVERIFIED", "RECOVERY"),
        ("BOOKING_UNVERIFIED", "HUMAN_HANDOFF"),
        ("BOOKING_UNVERIFIED", "FAILED"),
        # §2.5: a reschedule goes back through slot choice; the old booking is not
        # cancelled before the new one is verified
        ("BOOKED", "AWAITING_SLOT_CHOICE"),
        ("BOOKED", "CANCELLED"),
        ("BOOKED", "FAILED"),
        ("RECOVERY", "PROVIDER_MATCHING"),
        ("RECOVERY", "AWAITING_SLOT_CHOICE"),
        ("RECOVERY", "READINESS_CHECK"),
        ("RECOVERY", "HUMAN_HANDOFF"),
        ("RECOVERY", "FAILED"),
        ("RECOVERY", "ABANDONED"),
        ("RECOVERY", "CANCELLED"),
        # §11: the operator returns the workflow to a safe state; the final booking
        # still needs patient approval, so no edge leads straight to booking
        ("HUMAN_HANDOFF", "INTAKE"),
        ("HUMAN_HANDOFF", "PROVIDER_MATCHING"),
        ("HUMAN_HANDOFF", "READINESS_CHECK"),
        ("HUMAN_HANDOFF", "AWAITING_SLOT_CHOICE"),
        ("HUMAN_HANDOFF", "RECOVERY"),
        ("HUMAN_HANDOFF", "FAILED"),
        ("HUMAN_HANDOFF", "ABANDONED"),
        ("HUMAN_HANDOFF", "CANCELLED"),
    }
)


def is_legal_edge(from_state: str, to_state: str) -> bool:
    """Whether `workflow_transition` holds this edge."""
    return (from_state, to_state) in LEGAL_EDGES
