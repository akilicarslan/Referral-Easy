"""A failed reschedule is visible, and never narrated as a success (§7, §2.5, §11).

The ordering rule is the facility's and it is absolute: the replacement is created and verified
first, and the original is cancelled only after. So the two ways a move can end badly are not
symmetric — an unverified move with the original still in force costs the patient a delay, while a
move *reported as complete* when it is not falsifies the patient's calendar. The tests below are
mostly about the second one, because it is the failure that leaves the system looking healthy.

Three claims, in the order they matter:

* `narrated_as_success` is true only for a verified move — asserted as a property over every
  combination of outcome and cancellation, not as a list of examples.
* a failed move leaves a **visible** intermediate state rather than appearing unchanged, or hands off
  when the machine does not permit that state.
* the uncomfortable middle — the replacement exists and the original was never cancelled — is neither
  a success nor a failure, and goes to a person (§11 `conflicting_sources`).

And one limit, tested rather than glossed over: the move path cannot *enter recovery*, because
`BookingAttemptResult` has no transient member (see
`test_the_third_failure_of_anything_a_move_can_report_never_enters_recovery`), and when a move that is
already `BOOKED` fails in a way the machine has no edge for, the planner refuses with
`RecoveryRefusedError` instead of inventing one.
"""

from __future__ import annotations

from typing import Any

import pytest
from worker_doubles import FakeTransitions

from referraleasy_contracts.generated.booking import BookingAttemptResult
from referraleasy_contracts.generated.common import HandoffReasonClass
from referraleasy_workers.booking import RescheduleView
from referraleasy_workers.errors import RecoveryRefusedError
from referraleasy_workers.events import BOOKING_UNVERIFIED_STATE
from referraleasy_workers.recovery import (
    _RECOVERY_AS_RESCHEDULE,
    AWAITING_SLOT_CHOICE_STATE,
    BOOKED_STATE,
    HUMAN_HANDOFF_STATE,
    FailureClass,
    RecoveryAction,
    RescheduleAction,
    WorkflowTransitions,
    plan_reschedule,
)

#: The other state that can hold an unverified booking. There is no constant for it: the package names
#: it as a literal wherever it advances into it, and it lives in the transition table.
BOOKING_IN_FLIGHT_STATE = "BOOKING_IN_FLIGHT"

VIEW_MESSAGE_WHEN_UNVERIFIED = (
    "We could not complete the change to your appointment. Your existing appointment is still in "
    "place, and nothing has been cancelled."
)


def view(
    *,
    result: BookingAttemptResult = BookingAttemptResult.created,
    old_booking_cancelled: bool = False,
    detail: str | None = None,
    key: str | None = "idem-move-1",
) -> RescheduleView:
    """One reschedule answer, with the two fields the planning rule reads."""
    return RescheduleView(
        result=result,
        http_status=200,
        idempotency_key=key,
        booking_request_id="B-2",
        superseded_booking_request_id="B-1",
        old_booking_cancelled=old_booking_cancelled,
        detail=detail,
    )


def plan(
    *,
    answer: RescheduleView | None = None,
    state: str = BOOKED_STATE,
    attempts: int = 1,
    edges: dict[str, set[str]] | None = None,
) -> Any:
    return plan_reschedule(
        state=state,
        view=answer or view(),
        transitions=FakeTransitions(edges),
        attempts=attempts,
    )


# ---------------------------------------------------------------------------
# The one case that is a move
# ---------------------------------------------------------------------------


def test_a_verified_move_is_the_only_thing_called_a_move() -> None:
    """New booking verified *and* the old one gone — both, or it is not a move."""
    outcome = plan(answer=view(old_booking_cancelled=True))

    assert outcome.action is RescheduleAction.MOVED
    assert outcome.narrated_as_success is True
    assert outcome.original_in_force is False
    assert outcome.visible_state == BOOKED_STATE


# ---------------------------------------------------------------------------
# The failures
# ---------------------------------------------------------------------------


def test_a_failed_move_shows_an_intermediate_state_rather_than_looking_unchanged() -> None:
    """The workflow goes to `AWAITING_SLOT_CHOICE`: a state that says "this is not settled"."""
    outcome = plan(answer=view(result=BookingAttemptResult.slot_conflict))

    assert outcome.action is RescheduleAction.AWAITING_SLOT_CHOICE
    assert outcome.visible_state == AWAITING_SLOT_CHOICE_STATE
    assert outcome.shows_intermediate_state is True
    assert outcome.narrated_as_success is False
    assert outcome.original_in_force is True


def test_the_third_failure_of_slot_conflict_keeps_the_move_visible_and_offers_other_slots() -> None:
    """Attempts 1 and 2 stay in flow; the third is planned by recovery, and its answer is a choice.

    A slot conflict is not transient, so the third one does not *enter recovery* — the planner offers
    alternatives instead, which from the move's side is the same visible intermediate state, just at
    the end of the retry budget. The unreduced action is on the plan, so this is not a guess about
    what recovery thought.
    """
    second = plan(answer=view(result=BookingAttemptResult.slot_conflict), attempts=2)
    third = plan(answer=view(result=BookingAttemptResult.slot_conflict), attempts=3)

    assert second.action is RescheduleAction.AWAITING_SLOT_CHOICE
    assert second.recovery_action is None, "the first two attempts never reach the recovery planner"
    assert third.action is RescheduleAction.AWAITING_SLOT_CHOICE
    assert third.recovery_action is RecoveryAction.OFFER_ALTERNATIVES
    assert third.visible_state == AWAITING_SLOT_CHOICE_STATE
    assert third.narrated_as_success is False
    assert third.original_in_force is True


def test_an_unknown_move_outcome_is_reconciled_under_the_same_key_never_rebooked() -> None:
    """A move whose answer was lost is resolved by looking it up, on the key it was sent with (§7).

    The state is one that can hold an unverified booking, because that is where reconciliation lives;
    the point of the assertion is that the planner answers with `RECONCILE` and *not* with a retry,
    which is what re-booking would be.
    """
    outcome = plan(
        answer=view(result=BookingAttemptResult.timeout_after_commit),
        state=BOOKING_IN_FLIGHT_STATE,
        attempts=3,
    )

    assert outcome.action is RescheduleAction.RECONCILE
    assert outcome.recovery_action is RecoveryAction.RECONCILE
    assert outcome.visible_state == BOOKING_UNVERIFIED_STATE
    assert outcome.narrated_as_success is False
    assert "lookup_request" in outcome.detail
    assert "never by re-booking" in outcome.detail


@pytest.mark.parametrize(
    "result",
    [
        BookingAttemptResult.slot_conflict,
        BookingAttemptResult.rejected_by_hospital,
        BookingAttemptResult.timeout_after_commit,
        BookingAttemptResult.timeout_before_commit,
        BookingAttemptResult.malformed_response,
        BookingAttemptResult.rate_limited,
        BookingAttemptResult.outcome_unknown,
    ],
)
def test_the_third_failure_of_anything_a_move_can_report_never_enters_recovery(
    result: BookingAttemptResult,
) -> None:
    """Entering recovery is the answer to three *transient* failures, and a move cannot report one.

    `BookingAttemptResult` has no transient member — a slot conflict is a definite no, a rejection is a
    definite no, and the rest are unknown outcomes — so `ENTER_RECOVERY` is in the vocabulary for
    completeness (`_RECOVERY_AS_RESCHEDULE` is total by construction) but is unreachable from this
    entry point. The state here is deliberately one that *permits* `RECOVERY`: if any of these classes
    was going to enter it, nothing would stop it.
    """
    outcome = plan(answer=view(result=result), state=BOOKING_IN_FLIGHT_STATE, attempts=3)

    assert outcome.action is not RescheduleAction.ENTER_RECOVERY
    assert outcome.action is not RescheduleAction.MOVED
    assert outcome.narrated_as_success is False
    assert outcome.patient_message != ""


def test_a_move_the_machine_can_neither_show_nor_hand_off_is_refused() -> None:
    """`BOOKED` is a dead end for a move that goes wrong once the booking exists — and it says so.

    Read as a limit rather than worked around. `BOOKED` *does* permit `AWAITING_SLOT_CHOICE`, which is
    why the first two attempts are visible; what it has no edge to is `RECOVERY`,
    `BOOKING_UNVERIFIED` and `HUMAN_HANDOFF`, so the third failure of a class that needs one of those
    has no legal answer. The planner refuses instead of inventing a transition, and the workflow stays
    where it is with the patient's original appointment untouched — which is the least bad of the
    available lies. The honest fix is a migration adding the edge; nothing in this package may add one.
    """
    with pytest.raises(RecoveryRefusedError) as refusal:
        plan(answer=view(result=BookingAttemptResult.rejected_by_hospital), attempts=3)

    assert refusal.value.workflow_state == BOOKED_STATE
    assert refusal.value.failure_class == FailureClass.REJECTED_BY_HOSPITAL.value


def test_the_move_actions_are_the_recovery_actions_plus_moved() -> None:
    """The reduction is data, total over `RecoveryAction`, and can never be a completed move."""
    assert set(_RECOVERY_AS_RESCHEDULE) == set(RecoveryAction)
    assert RescheduleAction.MOVED not in _RECOVERY_AS_RESCHEDULE.values()
    assert set(RescheduleAction) == set(_RECOVERY_AS_RESCHEDULE.values()) | {RescheduleAction.MOVED}


def test_a_failed_move_that_the_machine_cannot_show_goes_to_a_person() -> None:
    """No edge to `AWAITING_SLOT_CHOICE` means a human, not an invented state.

    `BOOKING_IN_FLIGHT` is where this is real: it can be handed over and reconciled but it cannot show
    slot choice, so a failure with nothing left to try is a person's.
    """
    outcome = plan(
        answer=view(result=BookingAttemptResult.slot_conflict),
        state=BOOKING_IN_FLIGHT_STATE,
    )

    assert outcome.action is RescheduleAction.HANDOFF
    assert outcome.visible_state == HUMAN_HANDOFF_STATE
    assert outcome.narrated_as_success is False
    assert "no edge" in outcome.detail


def test_both_appointments_in_force_is_a_persons_decision_not_a_worker_s() -> None:
    """The replacement exists and the original was never cancelled: the patient may hold two (§11).

    Resolving it means cancelling an appointment the patient may be attending, which needs a person
    and evidence. It is not a booking failure, and it is emphatically not a success.
    """
    outcome = plan(
        answer=view(result=BookingAttemptResult.duplicate_replayed, old_booking_cancelled=False)
    )

    assert outcome.action is RescheduleAction.HANDOFF
    assert outcome.handoff_reason is HandoffReasonClass.conflicting_sources
    assert outcome.narrated_as_success is False
    assert outcome.original_in_force is True
    assert "A person on our team" in outcome.patient_message


def test_a_move_that_cancelled_the_original_but_could_not_be_verified_is_still_handled() -> None:
    """The other uncomfortable order: the old booking is gone and the new one is not confirmed.

    Not a move (nothing is verified), and not "your appointment is unchanged" either — the patient has
    no appointment in force, so the message says the change is still being confirmed.
    """
    outcome = plan(
        answer=view(result=BookingAttemptResult.slot_conflict, old_booking_cancelled=True)
    )

    assert outcome.narrated_as_success is False
    assert outcome.original_in_force is False
    assert "still being confirmed" in outcome.patient_message


# ---------------------------------------------------------------------------
# The property, over the whole space
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("old_booking_cancelled", [False, True])
@pytest.mark.parametrize(
    "result",
    [
        BookingAttemptResult.created,
        BookingAttemptResult.duplicate_replayed,
        BookingAttemptResult.slot_conflict,
        BookingAttemptResult.rejected_by_hospital,
        BookingAttemptResult.timeout_after_commit,
        BookingAttemptResult.timeout_before_commit,
        BookingAttemptResult.malformed_response,
        BookingAttemptResult.rate_limited,
        BookingAttemptResult.outcome_unknown,
    ],
)
@pytest.mark.parametrize("attempts", [1, 2])
def test_no_unverified_move_is_ever_narrated_as_a_success(
    result: BookingAttemptResult, old_booking_cancelled: bool, attempts: int
) -> None:
    """Every combination of outcome, cancellation and attempt count. One rule: verified or silence.

    The assertion is an implication in both directions, so neither half can drift: an outcome is
    narrated as a success *if and only if* the replacement exists and the original is gone.
    """
    answer = view(result=result, old_booking_cancelled=old_booking_cancelled)
    outcome = plan(answer=answer, attempts=attempts)

    assert outcome.narrated_as_success == answer.is_verified_move
    if outcome.narrated_as_success:
        assert outcome.action is RescheduleAction.MOVED
    else:
        assert outcome.action is not RescheduleAction.MOVED
        assert outcome.patient_message != ""


@pytest.mark.parametrize("old_booking_cancelled", [False, True])
@pytest.mark.parametrize(
    "result",
    [
        BookingAttemptResult.created,
        BookingAttemptResult.duplicate_replayed,
        BookingAttemptResult.slot_conflict,
        BookingAttemptResult.rejected_by_hospital,
        BookingAttemptResult.timeout_after_commit,
        BookingAttemptResult.timeout_before_commit,
        BookingAttemptResult.malformed_response,
        BookingAttemptResult.rate_limited,
        BookingAttemptResult.outcome_unknown,
    ],
)
def test_the_same_rule_holds_on_the_third_attempt_where_the_answers_are_planned(
    result: BookingAttemptResult, old_booking_cancelled: bool
) -> None:
    """The same property at the attempt count that hands the decision to recovery.

    Same space, same rule, different planner — and on the third attempt from `BOOKING_IN_FLIGHT`,
    which is a state that can express every answer recovery gives (it permits `RECOVERY`,
    `BOOKING_UNVERIFIED` and `HUMAN_HANDOFF`). `BOOKED` cannot, which is why the third-attempt rows
    that need those edges are pinned separately, as a refusal, in
    `test_a_move_the_machine_can_neither_show_nor_hand_off_is_refused`.
    """
    answer = view(result=result, old_booking_cancelled=old_booking_cancelled)
    outcome = plan(answer=answer, state=BOOKING_IN_FLIGHT_STATE, attempts=3)

    assert outcome.narrated_as_success == answer.is_verified_move
    if outcome.narrated_as_success:
        assert outcome.action is RescheduleAction.MOVED
    else:
        assert outcome.action is not RescheduleAction.MOVED
        assert outcome.patient_message != ""


@pytest.mark.parametrize("old_booking_cancelled", [False, True])
def test_the_patient_is_never_told_a_move_happened_when_it_did_not(
    old_booking_cancelled: bool,
) -> None:
    """The message for an unverified move is the view's own, and it never claims a change.

    The view is where the sentence lives, so this also pins that the planner did not invent one: a
    planner that composed its own optimistic text would fail here even if the flag stayed honest.
    """
    answer = view(
        result=BookingAttemptResult.slot_conflict, old_booking_cancelled=old_booking_cancelled
    )
    outcome = plan(answer=answer)

    assert outcome.patient_message == answer.patient_message
    assert "has been moved" not in outcome.patient_message


def test_the_classification_of_a_failed_move_is_not_a_second_vocabulary() -> None:
    """A failed move is classified by the same table everything else uses."""
    outcome = plan(answer=view(result=BookingAttemptResult.slot_conflict))

    assert outcome.failure_class is FailureClass.SLOT_CONFLICT


# ---------------------------------------------------------------------------
# Integration: against the machine that ships
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a_move_from_booked_can_be_shown_as_unsettled_in_the_real_machine(db: Any) -> None:
    """`BOOKED -> AWAITING_SLOT_CHOICE` is a real edge, so the intermediate state is reachable."""
    transitions = WorkflowTransitions(db)

    assert transitions.permits(BOOKED_STATE, AWAITING_SLOT_CHOICE_STATE) is True

    outcome = plan_reschedule(
        state=BOOKED_STATE,
        view=view(result=BookingAttemptResult.slot_conflict),
        transitions=transitions,
    )
    assert outcome.action is RescheduleAction.AWAITING_SLOT_CHOICE
    assert transitions.permits(BOOKED_STATE, outcome.visible_state) is True
