"""Classification, recovery, and the two things recovery must never do (§3, §7, §11).

Recovery exists because "it failed" is not a diagnosis. This module turns a failure into one of a
small set of *classes*, and a class into one of the actions the state machine actually permits —
which is a shorter list than it looks, because the transition table in `workflow_transition` has the
final say and it is the same table the guard trigger enforces.

Three rules are load-bearing, and each one is a rule because the alternative is a specific harm.

**Recovery never invents a state.** Every `target_state` in a plan is checked against
:class:`TransitionRules` before it is proposed, and when the intersection of "what this class needs"
and "what the machine permits" is empty, the planner raises
:class:`~referraleasy_workers.errors.RecoveryRefusedError` instead of picking something adjacent. A
worker that moved a workflow to a state the machine does not have would be refused by the guard
trigger anyway (`RE002`) — the difference is that the refusal here happens before anything else does.

**The move to recovery is one-way, and that is checked rather than asserted.** `RECOVERY` may not
reach a booking-threshold state (`BOOKED`, `AWAITING_FINAL_APPROVAL`, `BOOKING_IN_FLIGHT`); a
workflow that has been through recovery goes back through slot choice and the patient's own approval
before anything is booked. The planner verifies this against the live table every time it proposes
`RECOVERY` (`reaches_booking_threshold`). If an edge were ever added that made recovery reversible,
recovery would stop being a place you cannot accidentally book from, and the planner would fall back
to a human handoff rather than pretend otherwise.

**No model is called to phrase a failure.** The patient-facing sentence for each class is a constant
in :data:`_PATIENT_MESSAGES`. There is no LLM client anywhere in this module, and that absence is the
point: a reworded failure is still the same failure, the wording of a sentence about someone's
medical appointment should be reviewable in a diff, and a model call in this path would add latency
and a failure mode to the one code path that runs when things are already going wrong. The tests pin
both the absence (the planner's constructor takes no model) and the effect (a service-level run of a
recovery path makes zero model calls).

**The third transient failure leaves normal flow, and the first two do not.** A retry is not a state
change: attempts one and two answer `RETRY_SAME_CALL` and the workflow stays exactly where it is.
Only once the same call has failed transiently three times is the failure treated as a property of
the situation rather than of the attempt — and that threshold is a parameter
(:data:`DEFAULT_TRANSIENT_ATTEMPTS_BEFORE_RECOVERY`), not a magic number at a call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from referraleasy_agent_runtime import (
    BudgetExhausted,
    UpstreamRejected,
    UpstreamTransient,
    is_transient,
)
from referraleasy_contracts.generated.booking import BookingAttemptResult
from referraleasy_contracts.generated.common import HandoffReasonClass, ResultOutcome
from referraleasy_workers.booking import BookingAttemptView, RescheduleView
from referraleasy_workers.db import ConnectionLike, fetch_all
from referraleasy_workers.errors import (
    HumanOwnedError,
    IllegalTransitionError,
    PolicyDeniedError,
    RecoveryRefusedError,
    StaleOwnershipError,
)
from referraleasy_workers.events import BOOKING_UNVERIFIED_STATE

__all__ = [
    "AWAITING_SLOT_CHOICE_STATE",
    "BOOKED_STATE",
    "DEFAULT_TRANSIENT_ATTEMPTS_BEFORE_RECOVERY",
    "HUMAN_HANDOFF_STATE",
    "RECOVERY_STATE",
    "FailureClass",
    "RecoveryAction",
    "RecoveryPlan",
    "RecoveryPlanner",
    "RescheduleAction",
    "ReschedulePlan",
    "TransitionRules",
    "WorkflowTransitions",
    "classify_failure",
    "plan_reschedule",
    "reaches_booking_threshold",
]

RECOVERY_STATE: Final[str] = "RECOVERY"
HUMAN_HANDOFF_STATE: Final[str] = "HUMAN_HANDOFF"
AWAITING_SLOT_CHOICE_STATE: Final[str] = "AWAITING_SLOT_CHOICE"
BOOKED_STATE: Final[str] = "BOOKED"
FAILED_STATE: Final[str] = "FAILED"

#: How many transient failures of the same call are tolerated inside normal flow. The third one is
#: the one that moves the workflow — see the module docstring.
DEFAULT_TRANSIENT_ATTEMPTS_BEFORE_RECOVERY: Final[int] = 3

#: States from which a booking can be created without the patient approving it again. `RECOVERY` must
#: not reach any of them, or the one-way property is gone.
BOOKING_THRESHOLD_STATES: Final[frozenset[str]] = frozenset(
    {"BOOKING_IN_FLIGHT", "AWAITING_FINAL_APPROVAL", BOOKED_STATE}
)


class FailureClass(StrEnum):
    """What went wrong, at the resolution recovery needs — no finer than that, and no coarser."""

    #: The call could plausibly work if it were made again. Retried inside normal flow, up to the
    #: threshold, and only then treated as a property of the situation.
    TRANSIENT_UPSTREAM = "transient_upstream"
    #: The request left and no definite answer came back. Reconciliation's business, never a retry's.
    OUTCOME_UNKNOWN = "outcome_unknown"
    #: The facility answered, and the answer was "that slot is taken". A different slot is the fix;
    #: the same one will be taken again.
    SLOT_CONFLICT = "slot_conflict"
    #: A decision, not an outage: this provider or facility will not take this referral. Retrying
    #: cannot change a decision, and no automatic reroute may be invented from one.
    REJECTED_BY_HOSPITAL = "rejected_by_hospital"
    #: Something must be supplied before the flow can continue: a document, a preference, a choice.
    NEEDS_INPUT = "needs_input"
    #: `RE006`. The state machine or the policy table forbids this call in this state.
    POLICY_DENIED = "policy_denied"
    #: `RE001`/`RE002`. The caller's view of the workflow is not the workflow's.
    STATE_CONFLICT = "state_conflict"
    #: `RE003`. A human owns the workflow and automation is suspended until the ticket is resolved.
    HUMAN_OWNED = "human_owned"
    #: Nothing here recognised it — a 4xx we have no rule for, a configuration error, a bug. Named
    #: rather than guessed at: "unknown" is a classification, and it is the one that stops a worker
    #: from inventing a diagnosis for a failure it does not understand.
    UNEXPECTED = "unexpected"


class RecoveryAction(StrEnum):
    """What recovery decided. One enum, so no caller re-derives the decision from the failure."""

    #: Try the same call again, in the state the workflow is already in. Not a state change.
    RETRY_SAME_CALL = "retry_same_call"
    #: An unknown outcome: reconcile under the same key.
    RECONCILE = "reconcile"
    #: Offer different options (slots, providers) rather than repeating the doomed request.
    OFFER_ALTERNATIVES = "offer_alternatives"
    #: Ask the patient, or whoever holds the missing piece, for it.
    ASK_FOR_INPUT = "ask_for_input"
    #: Leave normal flow for `RECOVERY` — one-way, and only from states that permit it.
    ENTER_RECOVERY = "enter_recovery"
    #: Hand to a person. §11 defines when; this is the shape it arrives in.
    HANDOFF = "handoff"
    #: Stop, and do not touch the workflow (§11: automation is suspended while a human owns it).
    HALT = "halt"


class TransitionRules(Protocol):
    """The state machine, as much of it as recovery needs to see."""

    def permits(self, from_state: str, to_state: str) -> bool:  # pragma: no cover - protocol
        raise NotImplementedError

    def targets(self, from_state: str) -> frozenset[str]:  # pragma: no cover - protocol
        raise NotImplementedError


class WorkflowTransitions:
    """`workflow_transition`, read through. The same rows the guard trigger enforces.

    Read-only on purpose: nothing here may add an edge. A planner that could write the transitions it
    needs would be able to make any action permitted, which is the same as having no state machine.
    """

    def __init__(self, conn: ConnectionLike) -> None:
        self._conn = conn

    def permits(self, from_state: str, to_state: str) -> bool:
        rows = fetch_all(
            self._conn,
            "SELECT 1 FROM workflow_transition WHERE from_state = %s AND to_state = %s",
            (from_state, to_state),
        )
        return bool(rows)

    def targets(self, from_state: str) -> frozenset[str]:
        rows = fetch_all(
            self._conn,
            "SELECT to_state FROM workflow_transition WHERE from_state = %s",
            (from_state,),
        )
        return frozenset(row["to_state"] for row in rows)


def reaches_booking_threshold(rules: TransitionRules, from_state: str) -> bool:
    """Whether ``from_state`` can arrive at a state that books without a fresh patient approval.

    One step, not a graph walk: every path to a booking in this machine passes through one of the
    three threshold states, and a booking state is reached from one of them directly. A wider search
    would be a claim about reachability that this function could not keep honest, so it says what it
    checks.
    """
    return any(rules.permits(from_state, state) for state in BOOKING_THRESHOLD_STATES)


#: The classification table, written as data so that a new failure class cannot be added without
#: deciding what it is. The keys are the things a worker actually catches or reads.


def classify_failure(failure: object) -> FailureClass:
    """Classify a failure. Pure, table-driven, and with no model in it (§3).

    Accepts what a worker actually catches or reads: domain refusals by their type (the codes come
    from the schema, not from a string in an `except`), the runtime's HTTP errors, and the booking
    layer's views — because a `timeout_after_commit` is a failure the caller reads from a *view*
    rather than from an exception.

    A success is not a failure and is refused here rather than coerced into one: a classifier that
    answered "transient" for a `created` would be the single most expensive bug in this file.
    """
    if isinstance(failure, FailureClass):
        return failure
    if isinstance(failure, BaseException):
        return _classify_exception(failure)
    if isinstance(failure, BookingAttemptView):
        return _classify_attempt(failure.result)
    if isinstance(failure, RescheduleView):
        return _classify_attempt(failure.result)
    if isinstance(failure, ResultOutcome):
        return _classify_result_outcome(failure)
    if isinstance(failure, str):
        try:
            return _classify_attempt(BookingAttemptResult(failure))
        except ValueError:
            return FailureClass.UNEXPECTED
    raise ValueError(f"{failure!r} is not a failure; classification would be a guess")


def _classify_exception(failure: BaseException) -> FailureClass:
    if isinstance(failure, HumanOwnedError):
        return FailureClass.HUMAN_OWNED
    if isinstance(failure, (StaleOwnershipError, IllegalTransitionError)):
        return FailureClass.STATE_CONFLICT
    if isinstance(failure, PolicyDeniedError):
        return FailureClass.POLICY_DENIED
    if isinstance(failure, BudgetExhausted):
        # A deadline that expired on a write attempt is an unknown outcome, not a slow call.
        return (
            FailureClass.OUTCOME_UNKNOWN
            if failure.after_write_attempt
            else FailureClass.TRANSIENT_UPSTREAM
        )
    if isinstance(failure, UpstreamTransient) or is_transient(failure):
        return FailureClass.TRANSIENT_UPSTREAM
    if isinstance(failure, UpstreamRejected):
        # The far side answered and the answer was no. Which no it was is the caller's to explain;
        # what recovery can say without inventing anything is that the request needs a person.
        return FailureClass.UNEXPECTED
    return FailureClass.UNEXPECTED


def _classify_attempt(result: BookingAttemptResult) -> FailureClass:
    if result in (BookingAttemptResult.created, BookingAttemptResult.duplicate_replayed):
        raise ValueError(f"{result.value} is not a failure; classification would be a guess")
    if result is BookingAttemptResult.slot_conflict:
        return FailureClass.SLOT_CONFLICT
    if result is BookingAttemptResult.rejected_by_hospital:
        return FailureClass.REJECTED_BY_HOSPITAL
    # Both timeouts, `malformed_response`, `rate_limited` and `outcome_unknown` are the same question
    # with different timestamps: something left, nothing definitive came back (§7).
    return FailureClass.OUTCOME_UNKNOWN


def _classify_result_outcome(outcome: ResultOutcome) -> FailureClass:
    if outcome is ResultOutcome.completed:
        raise ValueError("a completed call is not a failure")
    if outcome in (ResultOutcome.retryable_error,):
        return FailureClass.TRANSIENT_UPSTREAM
    if outcome in (ResultOutcome.no_match, ResultOutcome.needs_input):
        return FailureClass.NEEDS_INPUT
    return FailureClass.OUTCOME_UNKNOWN


#: What a patient is told, per class. Constants, not generations — see the module docstring.
_PATIENT_MESSAGES: Final[dict[FailureClass, str]] = {
    FailureClass.TRANSIENT_UPSTREAM: (
        "The hospital's booking system is not responding at the moment. We are still trying."
    ),
    FailureClass.OUTCOME_UNKNOWN: (
        "We are checking with the hospital whether your appointment went through. "
        "We will not book anything else until we know."
    ),
    FailureClass.SLOT_CONFLICT: (
        "The appointment time we were trying for is no longer available. We can offer you another one."
    ),
    FailureClass.REJECTED_BY_HOSPITAL: (
        "The hospital cannot accept this referral. A person on our team is looking at what else is "
        "possible for you."
    ),
    FailureClass.NEEDS_INPUT: (
        "We need something more from you before we can continue with this referral."
    ),
    FailureClass.POLICY_DENIED: (
        "We stopped the automatic steps for this referral. A person on our team will pick it up."
    ),
    FailureClass.STATE_CONFLICT: (
        "This referral was picked up by someone else while we were working on it. "
        "Nothing was changed twice, and a person on our team is checking."
    ),
    FailureClass.HUMAN_OWNED: (
        "A member of our team is handling this referral by hand at the moment. "
        "Nothing is being done automatically while they do."
    ),
    FailureClass.UNEXPECTED: (
        "Something went wrong that we could not diagnose automatically, so we have stopped the "
        "automatic steps and asked a person on our team to look at this referral."
    ),
}

#: The handoff reason a class becomes, when the class ends in a handoff instead of an action.
_HANDOFF_REASONS: Final[dict[FailureClass, HandoffReasonClass]] = {
    FailureClass.TRANSIENT_UPSTREAM: HandoffReasonClass.technical_outage,
    FailureClass.OUTCOME_UNKNOWN: HandoffReasonClass.prolonged_unknown_booking,
    FailureClass.SLOT_CONFLICT: HandoffReasonClass.no_match,
    FailureClass.REJECTED_BY_HOSPITAL: HandoffReasonClass.no_reliable_recovery,
    FailureClass.NEEDS_INPUT: HandoffReasonClass.missing_document,
    FailureClass.POLICY_DENIED: HandoffReasonClass.no_reliable_recovery,
    FailureClass.STATE_CONFLICT: HandoffReasonClass.conflicting_sources,
    FailureClass.HUMAN_OWNED: HandoffReasonClass.patient_requested,
    FailureClass.UNEXPECTED: HandoffReasonClass.no_reliable_recovery,
}


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """What recovery decided, the evidence for it, and what the patient may be told."""

    state: str
    failure_class: FailureClass
    attempts: int
    action: RecoveryAction
    target_state: str | None = None
    handoff_reason: HandoffReasonClass | None = None
    #: True when the proposed move cannot be undone by the state machine (see the module docstring).
    one_way: bool = False
    detail: str = ""

    @property
    def moves_workflow(self) -> bool:
        """Whether this plan changes the workflow's state at all. A retry does not."""
        return self.target_state is not None and self.target_state != self.state

    @property
    def leaves_normal_flow(self) -> bool:
        """Whether this plan takes the workflow out of the ordinary path (§7: only after three)."""
        return self.action in (RecoveryAction.ENTER_RECOVERY, RecoveryAction.HANDOFF)

    @property
    def automation_must_stop(self) -> bool:
        return self.action is RecoveryAction.HALT

    @property
    def patient_message(self) -> str:
        return _PATIENT_MESSAGES[self.failure_class]


class RecoveryPlanner:
    """Chooses among state-machine-permitted actions for one failure (§3).

    Holds the transition rules and nothing else: no client, no model, no clock. Planning is a pure
    function of (state, failure, attempts), which is what makes it testable against the *real*
    transition table and what keeps a phrasing model out of the failure path structurally rather
    than by convention.
    """

    def __init__(
        self,
        *,
        transitions: TransitionRules,
        attempts_before_recovery: int = DEFAULT_TRANSIENT_ATTEMPTS_BEFORE_RECOVERY,
    ) -> None:
        if attempts_before_recovery < 1:
            raise ValueError("attempts_before_recovery must be at least 1")
        self._transitions = transitions
        self._attempts_before_recovery = attempts_before_recovery

    @property
    def attempts_before_recovery(self) -> int:
        return self._attempts_before_recovery

    def plan(
        self, *, state: str, failure: object, attempts: int = 1, detail: str = ""
    ) -> RecoveryPlan:
        """Decide what to do about ``failure``, from ``state``, on attempt ``attempts``.

        ``attempts`` counts the failures of this call *including* the one being planned, so
        ``attempts=3`` means "this is the third" and is the value that leaves normal flow.
        """
        failure_class = classify_failure(failure)
        move_detail = detail or f"classified as {failure_class.value}"

        if failure_class is FailureClass.HUMAN_OWNED:
            return RecoveryPlan(
                state=state,
                failure_class=failure_class,
                attempts=attempts,
                action=RecoveryAction.HALT,
                detail="a human owns this workflow; automation writes are suspended (§11)",
            )
        if failure_class is FailureClass.POLICY_DENIED:
            return RecoveryPlan(
                state=state,
                failure_class=failure_class,
                attempts=attempts,
                action=RecoveryAction.HALT,
                detail=(
                    "the policy table forbids this call in this state; recovering by calling something "
                    "else is how a policy refusal becomes a policy bypass"
                ),
            )
        if failure_class is FailureClass.STATE_CONFLICT:
            return RecoveryPlan(
                state=state,
                failure_class=failure_class,
                attempts=attempts,
                action=RecoveryAction.HALT,
                detail="this worker's view of the workflow is stale; stop rather than re-claim",
            )
        if failure_class is FailureClass.OUTCOME_UNKNOWN:
            return self._move(
                state=state,
                failure_class=failure_class,
                attempts=attempts,
                action=RecoveryAction.RECONCILE,
                target=BOOKING_UNVERIFIED_STATE,
                detail="resolve with lookup_request under the same key, never by re-booking",
            )
        if failure_class is FailureClass.NEEDS_INPUT:
            return RecoveryPlan(
                state=state,
                failure_class=failure_class,
                attempts=attempts,
                action=RecoveryAction.ASK_FOR_INPUT,
                detail=move_detail,
            )
        if failure_class in (FailureClass.REJECTED_BY_HOSPITAL, FailureClass.UNEXPECTED):
            return self._handoff(
                state=state, failure_class=failure_class, attempts=attempts, detail=move_detail
            )
        if failure_class is FailureClass.SLOT_CONFLICT:
            return self._move(
                state=state,
                failure_class=failure_class,
                attempts=attempts,
                action=RecoveryAction.OFFER_ALTERNATIVES,
                target=AWAITING_SLOT_CHOICE_STATE,
                detail="the time is gone, not the referral; offer other slots",
            )

        # TRANSIENT_UPSTREAM — the only class where the attempt count decides, and the only one where
        # the first two failures are not a state change at all.
        if attempts < self._attempts_before_recovery:
            return RecoveryPlan(
                state=state,
                failure_class=failure_class,
                attempts=attempts,
                action=RecoveryAction.RETRY_SAME_CALL,
                detail=f"{move_detail}; attempt {attempts} of {self._attempts_before_recovery}",
            )
        return self._enter_recovery(
            state=state, failure_class=failure_class, attempts=attempts, detail=move_detail
        )

    def _move(
        self,
        *,
        state: str,
        failure_class: FailureClass,
        attempts: int,
        action: RecoveryAction,
        target: str,
        detail: str,
    ) -> RecoveryPlan:
        """Propose a target state, or hand off when the machine does not permit it."""
        if not self._transitions.permits(state, target):
            return self._handoff(
                state=state,
                failure_class=failure_class,
                attempts=attempts,
                detail=f"{detail}; {state} has no edge to {target}, so a human is asked instead",
            )
        return RecoveryPlan(
            state=state,
            failure_class=failure_class,
            attempts=attempts,
            action=action,
            target_state=target,
            detail=detail,
        )

    def _enter_recovery(
        self, *, state: str, failure_class: FailureClass, attempts: int, detail: str
    ) -> RecoveryPlan:
        """Move to `RECOVERY`, but only from a state that cannot book and into one that stays there."""
        if not self._transitions.permits(state, RECOVERY_STATE):
            return self._handoff(
                state=state,
                failure_class=failure_class,
                attempts=attempts,
                detail=f"{detail}; {state} has no edge to {RECOVERY_STATE}",
            )
        if reaches_booking_threshold(self._transitions, RECOVERY_STATE):
            # The property that makes recovery a place to recover *from* is gone. Rather than enter a
            # state that can book without the patient, a human takes it.
            return self._handoff(
                state=state,
                failure_class=failure_class,
                attempts=attempts,
                detail=(
                    f"{detail}; {RECOVERY_STATE} can reach a booking threshold state, so the move is "
                    "not one-way and recovery would not be a safe place to be"
                ),
            )
        return RecoveryPlan(
            state=state,
            failure_class=failure_class,
            attempts=attempts,
            action=RecoveryAction.ENTER_RECOVERY,
            target_state=RECOVERY_STATE,
            one_way=True,
            detail=f"{detail}; attempt {attempts} of {self._attempts_before_recovery}",
        )

    def _handoff(
        self,
        *,
        state: str,
        failure_class: FailureClass,
        attempts: int,
        detail: str,
        reason: HandoffReasonClass | None = None,
    ) -> RecoveryPlan:
        if not self._handoff_permitted(state):
            raise RecoveryRefusedError(
                f"no recovery action is permitted from {state} for {failure_class.value}",
                workflow_state=state,
                failure_class=failure_class.value,
            )
        return RecoveryPlan(
            state=state,
            failure_class=failure_class,
            attempts=attempts,
            action=RecoveryAction.HANDOFF,
            target_state=HUMAN_HANDOFF_STATE,
            handoff_reason=reason or _HANDOFF_REASONS[failure_class],
            detail=detail,
        )

    def plan_escalation(
        self,
        *,
        state: str,
        reason_class: HandoffReasonClass,
        detail: str,
        failure_class: FailureClass = FailureClass.UNEXPECTED,
        attempts: int = 0,
    ) -> RecoveryPlan:
        """Hand off for a reason the caller knows better than the classifier does.

        Two of §11's triggers arrive this way rather than as a failure class: an exhausted
        reconciliation is `prolonged_unknown_booking` (not merely "something unknown happened"), and a
        patient asking for a person is `patient_requested`. Naming the reason at the call site keeps
        the trigger list and the reason vocabulary the same list.
        """
        return self._handoff(
            state=state,
            failure_class=failure_class,
            attempts=attempts,
            detail=detail,
            reason=reason_class,
        )

    def _handoff_permitted(self, state: str) -> bool:
        return self._transitions.permits(state, HUMAN_HANDOFF_STATE)


class RescheduleAction(StrEnum):
    """What happens to a move that did not complete.

    Six of these are the recovery vocabulary seen from the move's side — see
    :data:`_RECOVERY_AS_RESCHEDULE`, which is where the decision is written down — and `MOVED` is the
    one action recovery can never produce, because recovery only ever runs on a failure.
    """

    #: The new booking is verified and the old one is gone. The only case that may be called a move.
    MOVED = "moved"
    #: The move failed, the original is still in force, and the workflow shows a visible intermediate
    #: state rather than appearing unchanged.
    AWAITING_SLOT_CHOICE = "awaiting_slot_choice"
    #: Something left and nothing definitive came back: resolve it under the same key, never by
    #: re-booking (§7).
    RECONCILE = "reconcile"
    #: The move failed and the workflow has left normal flow.
    ENTER_RECOVERY = "enter_recovery"
    #: The move failed, it is not ours to retry, and a person takes it.
    HANDOFF = "handoff"
    #: Stop, and do not touch the workflow: a human owns it, the policy forbids the call, or this
    #: worker's view is stale (§11).
    HALT = "halt"


#: How a recovery action reads as a reschedule outcome. Total over `RecoveryAction` on purpose, and
#: asserted to be: a new recovery action must be given an answer here rather than defaulting to one.
#:
#: The reduction is honest rather than lossy, and it is worth being explicit about why three entries
#: share `AWAITING_SLOT_CHOICE`. In the move path all three mean the same thing to a caller — the move
#: did not happen, the workflow is still in normal flow, and the next thing it does is a *choice* —
#: while `RecoveryAction` needs them apart because it also plans failures that have no move in them.
#: The exact action is not lost: it is carried on the plan as `ReschedulePlan.recovery_action`.
#:
#: `MOVED` is absent because no recovery action can be a completed move.
_RECOVERY_AS_RESCHEDULE: Final[dict[RecoveryAction, RescheduleAction]] = {
    RecoveryAction.RETRY_SAME_CALL: RescheduleAction.AWAITING_SLOT_CHOICE,
    RecoveryAction.OFFER_ALTERNATIVES: RescheduleAction.AWAITING_SLOT_CHOICE,
    RecoveryAction.ASK_FOR_INPUT: RescheduleAction.AWAITING_SLOT_CHOICE,
    RecoveryAction.RECONCILE: RescheduleAction.RECONCILE,
    RecoveryAction.ENTER_RECOVERY: RescheduleAction.ENTER_RECOVERY,
    RecoveryAction.HANDOFF: RescheduleAction.HANDOFF,
    RecoveryAction.HALT: RescheduleAction.HALT,
}


@dataclass(frozen=True, slots=True)
class ReschedulePlan:
    """The outcome of a failed (or interrupted) move, in the shape the caller acts on."""

    state: str
    action: RescheduleAction
    attempts: int
    failure_class: FailureClass | None = None
    visible_state: str | None = None
    handoff_reason: HandoffReasonClass | None = None
    original_in_force: bool = True
    #: Whether this outcome may be described to the patient as a completed move. False unless the
    #: new booking was verified *and* the old one was cancelled — see
    #: :attr:`~referraleasy_workers.booking.RescheduleView.is_verified_move`.
    narrated_as_success: bool = False
    patient_message: str = ""
    detail: str = ""
    #: The recovery action this outcome came from, when it came from the recovery planner. Kept
    #: because `RescheduleAction` is a reduction of it (`_RECOVERY_AS_RESCHEDULE`), and a caller that
    #: needs the unreduced answer — the attempt count, "offer alternatives" versus "retry" — reads it
    #: here rather than re-deriving it from the failure.
    recovery_action: RecoveryAction | None = None

    @property
    def shows_intermediate_state(self) -> bool:
        """Whether the workflow ends up somewhere that visibly says "this is not settled"."""
        return self.visible_state is not None and self.visible_state != self.state


def _from_recovery(
    recovery: RecoveryPlan,
    *,
    state: str,
    attempts: int,
    view: RescheduleView,
    detail: str | None = None,
) -> ReschedulePlan:
    """Put a recovery decision into the move-shaped answer, unreduced action included."""
    return ReschedulePlan(
        state=state,
        action=_RECOVERY_AS_RESCHEDULE[recovery.action],
        attempts=attempts,
        failure_class=recovery.failure_class,
        visible_state=recovery.target_state,
        handoff_reason=recovery.handoff_reason,
        original_in_force=view.original_still_in_force,
        narrated_as_success=False,
        patient_message=view.patient_message,
        detail=recovery.detail if detail is None else detail,
        recovery_action=recovery.action,
    )


def plan_reschedule(
    *,
    state: str,
    view: RescheduleView,
    transitions: TransitionRules,
    attempts: int = 1,
    attempts_before_recovery: int = DEFAULT_TRANSIENT_ATTEMPTS_BEFORE_RECOVERY,
) -> ReschedulePlan:
    """Decide what a reschedule attempt leaves behind (§7, §2.5).

    The ordering rule is the facility's and it is absolute: the replacement is created and verified
    first, and the original is cancelled only after. So the two ways a move can end badly are not
    symmetric — an unverified move with the original still in force costs the patient a delay, while a
    move reported as complete when it is not falsifies the patient's calendar.

    This function refuses the second one outright: ``narrated_as_success`` is true only for
    :attr:`RescheduleView.is_verified_move`, and the message comes from the view. There is no branch
    here that can describe an unverified move as a move.

    A failed move is visible rather than silent: the workflow goes to `AWAITING_SLOT_CHOICE`, which is
    the state the machine keeps for exactly this (a reschedule returns through slot choice). When the
    workflow is somewhere that cannot show that state, or on the third attempt, the decision comes from
    the recovery planner and arrives here through :data:`_RECOVERY_AS_RESCHEDULE` — unreduced, on
    `ReschedulePlan.recovery_action`.

    A state from which the machine permits **nothing** legal raises
    :class:`~referraleasy_workers.errors.RecoveryRefusedError` rather than guessing: the shipped
    machine gives `BOOKED` no edge to `RECOVERY`, `BOOKING_UNVERIFIED` or `HUMAN_HANDOFF`, so a move
    that is rejected or lost once it is already booked has no expressible answer and says so.
    """
    if view.is_verified_move:
        return ReschedulePlan(
            state=state,
            action=RescheduleAction.MOVED,
            attempts=attempts,
            visible_state=BOOKED_STATE,
            original_in_force=False,
            narrated_as_success=True,
            patient_message=view.patient_message,
            detail="the new booking is verified and the original was cancelled",
        )

    detail = (
        view.detail
        or view.cancellation_deferred_reason
        or f"the reschedule answered {view.result.value}"
    )

    if view.both_in_force:
        # The replacement exists and the original was never cancelled: the patient may hold two
        # appointments. This is not a booking failure and not a success, and no automatic path may
        # choose between them — resolving it means cancelling an appointment a patient may be
        # attending, which is a person's decision with a person's evidence.
        return ReschedulePlan(
            state=state,
            action=RescheduleAction.HANDOFF,
            attempts=attempts,
            failure_class=FailureClass.OUTCOME_UNKNOWN,
            visible_state=HUMAN_HANDOFF_STATE,
            handoff_reason=HandoffReasonClass.conflicting_sources,
            original_in_force=True,
            narrated_as_success=False,
            patient_message=(
                "We could not confirm the change to your appointment, and your original appointment "
                "is still in place. A person on our team is checking now and will tell you which "
                "appointment is yours."
            ),
            detail=f"{detail}; the replacement exists and the original was not cancelled",
        )

    failure_class = classify_failure(view)

    if attempts >= attempts_before_recovery:
        # The same threshold as everywhere else, and the same planner: a reschedule that keeps
        # failing is not a booking problem with a booking-shaped fix.
        recovery = RecoveryPlanner(
            transitions=transitions, attempts_before_recovery=attempts_before_recovery
        ).plan(state=state, failure=view, attempts=attempts, detail=detail)
        return _from_recovery(recovery, state=state, attempts=attempts, view=view)

    visible = (
        AWAITING_SLOT_CHOICE_STATE
        if transitions.permits(state, AWAITING_SLOT_CHOICE_STATE)
        else None
    )
    if visible is None:
        # The machine cannot show this failure where the workflow is, so the answer comes from the
        # planner — and the reason it was asked is kept in the detail, because "we handed this over"
        # and "we handed this over because the state machine leaves no legal move" are different
        # operator stories.
        recovery = RecoveryPlanner(
            transitions=transitions, attempts_before_recovery=attempts_before_recovery
        ).plan(state=state, failure=view, attempts=attempts, detail=detail)
        return _from_recovery(
            recovery,
            state=state,
            attempts=attempts,
            view=view,
            detail=f"{recovery.detail}; {state} has no edge to {AWAITING_SLOT_CHOICE_STATE}",
        )

    return ReschedulePlan(
        state=state,
        action=RescheduleAction.AWAITING_SLOT_CHOICE,
        attempts=attempts,
        failure_class=failure_class,
        visible_state=visible,
        original_in_force=view.original_still_in_force,
        narrated_as_success=False,
        patient_message=view.patient_message,
        detail=f"{detail}; attempt {attempts} of {attempts_before_recovery}",
    )
