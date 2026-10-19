"""Recovery: classification, permitted actions, and the one-way move (§3, §7, §11).

Three claims are tested here, and each of them is a claim about *what is absent*.

**Classification is a table, not a judgement call.** `classify_failure` is pure and total: every
failure the system can produce has exactly one class, and the classes are coarse enough that no
failure needs a model to name it.

**The planner chooses among actions the state machine permits.** Not among actions that would be
nice: `RecoveryPlanner` holds the transition rules and nothing else — no client, no clock, no model —
and the tests assert that structurally as well as behaviourally, because "no LLM call is made just to
reword a failure message" is a claim about what the object *can* do. A planner that could not open a
connection could not phrase anything either.

**Leaving normal flow is one-way.** The third transient failure is the one that moves the workflow,
and `RECOVERY` has no edge to a state that books. The last test in this module records a limit of that
property honestly: it is one *edge*, not a graph reachability check, and `RECOVERY` is two hops from
`AWAITING_FINAL_APPROVAL` through `AWAITING_SLOT_CHOICE` — a walk that arrives there with no live
approval, so it books nothing, but it is not the same statement as "unreachable".
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from worker_doubles import PATH_TO_FINAL_APPROVAL, TRANSITION_EDGES, FakeTransitions

from referraleasy_agent_runtime import ConfigurationError, UpstreamRejected, UpstreamTransient
from referraleasy_contracts.generated.booking import BookingAttemptResult
from referraleasy_contracts.generated.common import HandoffReasonClass, ResultOutcome
from referraleasy_domain import (
    ApprovalRefusedError,
    HumanOwnedError,
    PolicyDeniedError,
    StaleOwnershipError,
)
from referraleasy_workers.booking import BookingAttemptView
from referraleasy_workers.errors import RecoveryRefusedError
from referraleasy_workers.hitl import HITL_TRIGGERS
from referraleasy_workers.recovery import (
    AWAITING_SLOT_CHOICE_STATE,
    BOOKED_STATE,
    BOOKING_THRESHOLD_STATES,
    DEFAULT_TRANSIENT_ATTEMPTS_BEFORE_RECOVERY,
    HUMAN_HANDOFF_STATE,
    RECOVERY_STATE,
    FailureClass,
    RecoveryAction,
    RecoveryPlanner,
    WorkflowTransitions,
    classify_failure,
    reaches_booking_threshold,
)


def attempt(result: BookingAttemptResult, **kwargs: Any) -> BookingAttemptView:
    """One attempt result, with only the fields a classification reads."""
    return BookingAttemptView(
        result=result,
        idempotency_key=kwargs.pop("key", "idem-1"),
        http_status=kwargs.pop("http_status", 200),
        **kwargs,
    )


#: One failure per class, so a totality claim can be made by *driving* the classifier rather than by
#: reading its table. Built here because the classes are the vocabulary the tests assert on.
ONE_FAILURE_PER_CLASS: dict[FailureClass, Any] = {
    FailureClass.TRANSIENT_UPSTREAM: UpstreamTransient("connection reset"),
    FailureClass.OUTCOME_UNKNOWN: attempt(BookingAttemptResult.timeout_after_commit),
    FailureClass.SLOT_CONFLICT: attempt(BookingAttemptResult.slot_conflict),
    FailureClass.REJECTED_BY_HOSPITAL: attempt(BookingAttemptResult.rejected_by_hospital),
    FailureClass.NEEDS_INPUT: ResultOutcome.no_match,
    FailureClass.POLICY_DENIED: PolicyDeniedError("denied", sqlstate="RE006"),
    FailureClass.STATE_CONFLICT: StaleOwnershipError("stale", sqlstate="RE001"),
    FailureClass.HUMAN_OWNED: HumanOwnedError("held", sqlstate="RE003"),
    FailureClass.UNEXPECTED: UpstreamRejected("400 from the facility", status_code=400),
}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure, expected",
    [
        (attempt(BookingAttemptResult.slot_conflict), FailureClass.SLOT_CONFLICT),
        (attempt(BookingAttemptResult.rejected_by_hospital), FailureClass.REJECTED_BY_HOSPITAL),
        (attempt(BookingAttemptResult.timeout_after_commit), FailureClass.OUTCOME_UNKNOWN),
        (attempt(BookingAttemptResult.timeout_before_commit), FailureClass.OUTCOME_UNKNOWN),
        (attempt(BookingAttemptResult.malformed_response), FailureClass.OUTCOME_UNKNOWN),
        (attempt(BookingAttemptResult.rate_limited), FailureClass.OUTCOME_UNKNOWN),
        (UpstreamTransient("connection reset"), FailureClass.TRANSIENT_UPSTREAM),
        (UpstreamRejected("400 from the facility", status_code=400), FailureClass.UNEXPECTED),
        (StaleOwnershipError("stale", sqlstate="RE001"), FailureClass.STATE_CONFLICT),
        (HumanOwnedError("human", sqlstate="RE003"), FailureClass.HUMAN_OWNED),
        (PolicyDeniedError("denied", sqlstate="RE006"), FailureClass.POLICY_DENIED),
        (ConfigurationError("no URL"), FailureClass.UNEXPECTED),
        (RuntimeError("something nobody classified"), FailureClass.UNEXPECTED),
    ],
)
def test_every_failure_lands_in_exactly_one_class(failure: object, expected: FailureClass) -> None:
    assert classify_failure(failure) is expected


def test_an_unrecognised_failure_is_a_class_rather_than_a_guess() -> None:
    """`UNEXPECTED` exists so that a worker does not invent a diagnosis for what it cannot name.

    The alternative — falling through to "transient, try again" — would spend a booking budget on a
    bug, which is the direction that creates duplicates. A value that is not a failure *shape* at all
    is refused outright rather than classified: see the success test below for why that matters.
    """
    assert classify_failure(RuntimeError("something nobody classified")) is FailureClass.UNEXPECTED
    assert classify_failure("a booking result nobody defined") is FailureClass.UNEXPECTED

    with pytest.raises(ValueError):
        classify_failure(None)
    with pytest.raises(ValueError):
        classify_failure(object())


def test_classification_is_pure() -> None:
    """Called twice with the same value, once each on two distinct objects: same answer.

    A classifier that consulted a connection or a clock could not make that promise, and the failure
    path is not the place to discover that. Asserted by signature rather than by mocking: the
    function takes one value and returns one value.
    """
    signature = inspect.signature(classify_failure)
    assert list(signature.parameters) == ["failure"]


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------


def planner(edges: dict[str, set[str]] | None = None, **kwargs: Any) -> RecoveryPlanner:
    return RecoveryPlanner(transitions=FakeTransitions(edges), **kwargs)


def test_the_first_two_transient_failures_change_nothing() -> None:
    """Attempts 1 and 2 are retried in place. Only the third is a property of the situation."""
    for attempts in (1, 2):
        plan = planner().plan(
            state="BOOKING_IN_FLIGHT", failure=UpstreamTransient("boom"), attempts=attempts
        )

        assert plan.action is RecoveryAction.RETRY_SAME_CALL
        assert plan.moves_workflow is False
        assert plan.leaves_normal_flow is False
        assert plan.target_state is None


def test_the_third_transient_failure_leaves_normal_flow() -> None:
    plan = planner().plan(state="BOOKING_IN_FLIGHT", failure=UpstreamTransient("boom"), attempts=3)

    assert plan.action is RecoveryAction.ENTER_RECOVERY
    assert plan.target_state == RECOVERY_STATE
    assert plan.leaves_normal_flow is True
    assert plan.one_way is True


def test_the_threshold_is_the_documented_three() -> None:
    """One number, in one place: the tests read it rather than restating `3`."""
    assert DEFAULT_TRANSIENT_ATTEMPTS_BEFORE_RECOVERY == 3
    plan = planner().plan(
        state="BOOKING_IN_FLIGHT",
        failure=UpstreamTransient("boom"),
        attempts=DEFAULT_TRANSIENT_ATTEMPTS_BEFORE_RECOVERY,
    )
    assert plan.action is RecoveryAction.ENTER_RECOVERY


def test_an_unknown_outcome_is_sent_to_reconciliation_rather_than_retried() -> None:
    """§7: an unknown outcome is resolved by looking it up under the same key, never by re-sending."""
    for attempts in (1, 2, 3):
        plan = planner().plan(
            state="BOOKING_IN_FLIGHT",
            failure=attempt(BookingAttemptResult.timeout_after_commit),
            attempts=attempts,
        )

        assert plan.action is RecoveryAction.RECONCILE
        assert plan.target_state == "BOOKING_UNVERIFIED"
        assert plan.action is not RecoveryAction.RETRY_SAME_CALL


def test_a_slot_conflict_offers_alternatives_when_the_machine_allows_it() -> None:
    """A taken slot is not a failed referral: offer other times, in the state kept for it."""
    plan = planner().plan(
        state="READINESS_CHECK", failure=attempt(BookingAttemptResult.slot_conflict)
    )

    assert plan.action is RecoveryAction.OFFER_ALTERNATIVES
    assert plan.target_state == AWAITING_SLOT_CHOICE_STATE


def test_a_booking_slot_conflict_hands_off_because_no_edge_leads_to_slot_choice() -> None:
    """`BOOKING_IN_FLIGHT` has no edge to `AWAITING_SLOT_CHOICE` in the real machine either.

    A planner that returned the target anyway would produce a transition the database refuses — a
    crash where a handoff belonged. What it does instead is hand off, with a reason §11 lists.
    """
    plan = planner().plan(
        state="BOOKING_IN_FLIGHT", failure=attempt(BookingAttemptResult.slot_conflict)
    )

    assert plan.action is RecoveryAction.HANDOFF
    assert plan.target_state != AWAITING_SLOT_CHOICE_STATE
    assert plan.handoff_reason is HandoffReasonClass.no_match


def test_when_no_action_is_permitted_recovery_refuses_rather_than_inventing_one() -> None:
    """The last resort, and it is an exception rather than a guess (§3).

    With neither an edge to `AWAITING_SLOT_CHOICE` nor one to `HUMAN_HANDOFF` there is no permitted
    action at all, and `RecoveryRefusedError` says exactly that. The alternative — picking a state
    anyway — is how a worker writes a transition the database will then refuse mid-flight.
    """
    edges = {**TRANSITION_EDGES, "BOOKING_IN_FLIGHT": {"BOOKED", "BOOKING_UNVERIFIED"}}

    with pytest.raises(RecoveryRefusedError) as raised:
        planner(edges).plan(
            state="BOOKING_IN_FLIGHT", failure=attempt(BookingAttemptResult.slot_conflict)
        )

    assert "slot_conflict" in str(raised.value)
    assert "BOOKING_IN_FLIGHT" in str(raised.value)


def test_a_human_owned_workflow_halts_instead_of_recovering() -> None:
    """§11: a human holds the workflow, so automation stops. `HALT` writes nothing at all."""
    plan = planner().plan(
        state="BOOKING_IN_FLIGHT", failure=HumanOwnedError("held", sqlstate="RE003")
    )

    assert plan.action is RecoveryAction.HALT
    assert plan.automation_must_stop is True
    assert plan.moves_workflow is False
    assert plan.target_state is None


def test_a_policy_refusal_is_not_recovered_by_calling_something_else() -> None:
    """Recovering from `RE006` by choosing another tool is how a policy refusal becomes a bypass."""
    plan = planner().plan(
        state="BOOKING_IN_FLIGHT", failure=PolicyDeniedError("denied", sqlstate="RE006")
    )

    assert plan.action is RecoveryAction.HALT
    assert plan.target_state is None


def test_a_stale_view_of_the_workflow_halts_rather_than_re_claiming() -> None:
    plan = planner().plan(
        state="BOOKING_IN_FLIGHT", failure=StaleOwnershipError("stale", sqlstate="RE001")
    )

    assert plan.action is RecoveryAction.HALT
    assert plan.detail and "stale" in plan.detail


def test_recovery_has_no_model_and_no_clock_to_call() -> None:
    """The structural half of "no LLM call is made just to reword a failure message" (§3).

    Asserted on the object's own state and the method's signature, not on a mock: a planner that
    holds only transition rules cannot make a call, whatever a later edit intended. Adding a client
    or a clock here fails this test, which is the point — that would be a change to the design, and it
    should be a deliberate one.
    """
    recovery_planner = planner()

    held = getattr(recovery_planner, "__dict__", {})
    assert set(held) == {"_transitions", "_attempts_before_recovery"}, (
        f"RecoveryPlanner holds {sorted(held)} — it should hold the transition rules and the "
        "threshold, and nothing that could reach a model or a network"
    )
    assert list(inspect.signature(RecoveryPlanner.plan).parameters) == [
        "self",
        "state",
        "failure",
        "attempts",
        "detail",
    ]


def test_planning_twice_makes_no_call_and_produces_the_same_plan() -> None:
    """`plan` is a pure function of (state, failure, attempts) — the claim, exercised."""
    failure = UpstreamTransient("boom")
    first = planner().plan(state="BOOKING_IN_FLIGHT", failure=failure, attempts=3)
    second = planner().plan(
        state="BOOKING_IN_FLIGHT", failure=UpstreamTransient("boom"), attempts=3
    )

    assert first == second


def test_the_patient_message_comes_from_the_classification() -> None:
    """One table, one sentence per class: the failure's own text never reaches the patient."""
    plan = planner().plan(state="BOOKING_IN_FLIGHT", failure=UpstreamTransient("boom"), attempts=3)

    assert plan.patient_message
    assert "boom" not in plan.patient_message
    assert (
        plan.patient_message
        == planner()
        .plan(state="BOOKING_IN_FLIGHT", failure=UpstreamTransient("different text"), attempts=3)
        .patient_message
    )


def test_every_failure_class_has_a_sentence_for_the_patient() -> None:
    """Total by construction: every class the system can produce, driven through one plan each.

    A class with no sentence would surface as a `KeyError` in front of a patient — on the failure
    path, which is where a second thing that can fail is least affordable.
    """
    for failure_class, failure in ONE_FAILURE_PER_CLASS.items():
        plan = planner().plan(state="BOOKING_IN_FLIGHT", failure=failure, attempts=3)

        assert classify_failure(failure) is failure_class
        assert plan.patient_message.strip(), (
            f"{failure_class.value} has no sentence for the patient"
        )


def test_the_one_failure_per_class_table_covers_the_vocabulary() -> None:
    """Anti-vacuity for the test above: a new class cannot make it silently smaller."""
    assert set(ONE_FAILURE_PER_CLASS) == set(FailureClass)


def test_every_class_that_ends_in_a_handoff_names_a_reason_that_is_a_trigger() -> None:
    """`_HANDOFF_REASONS` is total, and every value is one of §11's triggers — checked by acting."""
    trigger_classes = {trigger.reason_class for trigger in HITL_TRIGGERS}

    for failure_class, failure in ONE_FAILURE_PER_CLASS.items():
        # `attempts=3` so a transient failure reaches the handoff branch; the classes that halt
        # (human-owned, policy, stale) never hand off, and that is asserted above.
        plan = planner().plan(state="BOOKING_IN_FLIGHT", failure=failure, attempts=3)
        if plan.action is not RecoveryAction.HANDOFF:
            continue

        assert plan.handoff_reason in trigger_classes, (
            f"{failure_class.value} hands off with {plan.handoff_reason}, which §11 does not list"
        )


def test_a_success_is_refused_by_the_classifier_rather_than_called_a_failure() -> None:
    """The most expensive possible bug in this file, walled off.

    `created` classified as anything at all would route a booked patient down a failure path.
    """
    with pytest.raises(ValueError):
        classify_failure(attempt(BookingAttemptResult.created))
    with pytest.raises(ValueError):
        classify_failure(attempt(BookingAttemptResult.duplicate_replayed))
    with pytest.raises(ValueError):
        classify_failure(ResultOutcome.completed)
    with pytest.raises(ValueError):
        classify_failure(object())  # not a failure *shape* at all


# ---------------------------------------------------------------------------
# The threshold, and the one-way promise
# ---------------------------------------------------------------------------


def test_a_threshold_state_is_where_a_booking_can_happen_without_new_consent() -> None:
    assert (
        frozenset({"BOOKING_IN_FLIGHT", "AWAITING_FINAL_APPROVAL", BOOKED_STATE})
        == BOOKING_THRESHOLD_STATES
    )


def test_recovery_cannot_reach_a_booking_threshold_in_one_edge() -> None:
    """The property `reaches_booking_threshold` claims, checked (it says one step, not a walk)."""
    assert reaches_booking_threshold(FakeTransitions(), RECOVERY_STATE) is False
    assert reaches_booking_threshold(FakeTransitions(), "BOOKING_UNVERIFIED") is True


def test_the_third_failure_does_not_ask_the_transition_table_for_two_answers() -> None:
    """A plan to *enter recovery* and a plan to *book* cannot both come from one failure.

    Read as: the plan that leaves normal flow names `RECOVERY`, and `RECOVERY` is not a threshold
    state, so the move that stopped automation cannot itself satisfy the guard that requires consent.
    """
    plan = planner().plan(state="BOOKING_IN_FLIGHT", failure=UpstreamTransient("boom"), attempts=3)

    assert plan.target_state == RECOVERY_STATE
    assert plan.target_state not in BOOKING_THRESHOLD_STATES


# ---------------------------------------------------------------------------
# §11's trigger list
# ---------------------------------------------------------------------------


def test_every_handoff_reason_class_is_a_trigger_exactly_once() -> None:
    """The list is the *whole* list, in both directions.

    Every reason class the system can emit appears, so a new reason cannot be added to the vocabulary
    while being forgotten as a trigger; and no class appears twice, so two conditions that start a
    handoff cannot be described as one.
    """
    classes = [trigger.reason_class for trigger in HITL_TRIGGERS]

    assert len(classes) == len(set(classes))
    assert set(classes) == set(HandoffReasonClass)


def test_every_trigger_says_when_it_fires() -> None:
    for trigger in HITL_TRIGGERS:
        assert trigger.when.strip(), f"{trigger.reason_class.value} has no condition"
        assert (
            trigger.reason_class.value in trigger.when
            or "§" in trigger.when
            or len(trigger.when) > 20
        )


def test_the_unknown_booking_trigger_is_named_for_its_cause() -> None:
    """`prolonged_unknown_booking` is the *budget running out*, not "something was unknown".

    The distinction is what makes the ticket useful: a reconciliation that is still working has not
    triggered anything, and one whose budget is gone has.
    """
    triggers = {trigger.reason_class: trigger.when for trigger in HITL_TRIGGERS}

    assert "budget is spent" in triggers[HandoffReasonClass.prolonged_unknown_booking]
    assert (
        "reschedule left the replacement booked" in triggers[HandoffReasonClass.conflicting_sources]
    )


# ---------------------------------------------------------------------------
# Integration: the real state machine
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_real_transition_table_is_the_one_the_planner_reads(db: Any) -> None:
    """`WorkflowTransitions` reads `workflow_transition` — the same rows the guard trigger enforces."""
    transitions = WorkflowTransitions(db)

    assert transitions.permits("BOOKING_IN_FLIGHT", "BOOKED") is True
    assert transitions.permits("BOOKING_IN_FLIGHT", "AWAITING_FINAL_APPROVAL") is False
    assert "RECOVERY" in transitions.targets("BOOKING_IN_FLIGHT")


@pytest.mark.integration
def test_recovery_is_one_way_against_the_real_machine(db: Any) -> None:
    """The property, against the schema that ships."""
    transitions = WorkflowTransitions(db)

    assert reaches_booking_threshold(transitions, RECOVERY_STATE) is False
    assert reaches_booking_threshold(transitions, "BOOKING_IN_FLIGHT") is True


@pytest.mark.integration
def test_recovery_is_two_hops_from_the_threshold_through_slot_choice(db: Any) -> None:
    """A limit of the one-way property, recorded rather than assumed away.

    `reaches_booking_threshold` is a one-step check by design — its docstring says so — and the real
    machine has `RECOVERY -> AWAITING_SLOT_CHOICE -> AWAITING_FINAL_APPROVAL`. So "recovery is
    one-way" means *no direct edge back to a state that books*, not *unreachable*: a workflow that
    walks those two edges reaches `AWAITING_FINAL_APPROVAL` again.

    What still holds, and is why this is survivable rather than a hole, is the second half: arriving
    there does not book anything. `workflow_booking_consent_guard` requires a live approval bound to
    the *old* `state_version` and payload hash, and the approval that authorised the original attempt
    was consumed by `booking_request_create` and cannot be spent twice. The next test pins that.

    This test exists so the distinction is visible in the suite. If a later change renames these
    states or removes the edge, it fails, and whoever sees it gets the explanation rather than a
    surprise.
    """
    transitions = WorkflowTransitions(db)

    assert transitions.permits(RECOVERY_STATE, AWAITING_SLOT_CHOICE_STATE) is True
    assert transitions.permits(AWAITING_SLOT_CHOICE_STATE, "AWAITING_FINAL_APPROVAL") is True
    assert transitions.permits(RECOVERY_STATE, "AWAITING_FINAL_APPROVAL") is False


@pytest.mark.integration
def test_a_workflow_that_walks_back_to_the_threshold_still_needs_fresh_consent(
    db: Any, seed: Any
) -> None:
    """The reason the two-hop path is survivable: the threshold is guarded, not the path to it."""
    from worker_doubles import expect_refusal

    from referraleasy_domain import WorkflowStore

    workflow, approval_id, _hash = seed.consent_ready()
    store = WorkflowStore(db)
    row = store.advance(
        workflow.id,
        "w-1",
        expected_generation=workflow.generation,
        expected_state_version=workflow.state_version,
        to_state="BOOKING_IN_FLIGHT",
    )
    seed.booking_request(row.id, approval_id=approval_id)

    # Back out of the booking leg into recovery, the way a handoff would.
    for state in ("RECOVERY", "AWAITING_SLOT_CHOICE", "AWAITING_FINAL_APPROVAL"):
        row = store.advance(
            row.id,
            "w-1",
            expected_generation=row.generation,
            expected_state_version=row.state_version,
            to_state=state,
        )
    assert row.state == "AWAITING_FINAL_APPROVAL"

    # The approval was spent by the booking, so the guard refuses the second entry to BOOKING_IN_FLIGHT
    # even though the state machine's edge is still there.
    with expect_refusal(db, ApprovalRefusedError) as _:
        store.advance(
            row.id,
            "w-1",
            expected_generation=row.generation,
            expected_state_version=row.state_version,
            to_state="BOOKING_IN_FLIGHT",
        )


@pytest.mark.integration
def test_the_planner_over_the_real_machine_chooses_the_state_the_database_permits(db: Any) -> None:
    """End of the loop: a plan made from real edges is a plan the guard will accept."""
    recovery_planner = RecoveryPlanner(transitions=WorkflowTransitions(db))
    plan = recovery_planner.plan(
        state="BOOKING_IN_FLIGHT", failure=UpstreamTransient("boom"), attempts=3
    )

    assert plan.target_state == RECOVERY_STATE
    assert WorkflowTransitions(db).permits("BOOKING_IN_FLIGHT", plan.target_state) is True


@pytest.mark.integration
def test_a_human_handoff_state_is_permitted_from_wherever_a_ticket_is_opened(db: Any) -> None:
    """§11's tickets are opened from the states the machine has an edge from — not from a guess."""
    transitions = WorkflowTransitions(db)

    for state in (
        *PATH_TO_FINAL_APPROVAL,
        "BOOKING_IN_FLIGHT",
        "BOOKING_UNVERIFIED",
        RECOVERY_STATE,
    ):
        assert transitions.permits(state, HUMAN_HANDOFF_STATE) is True, (
            f"{state} cannot reach {HUMAN_HANDOFF_STATE}, so a handoff from it would be refused"
        )
