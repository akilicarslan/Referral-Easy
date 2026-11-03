"""The replay's observation → run record mapping (`referraleasy_eval.capture`), and the dataset's
vocabulary, which a captured run has to be able to match.

The mapping is where a capture could flatter the system, so most tests here pin a refusal: a field
nothing produced stays empty, an unknown constraint is not a satisfied one, and a journey that did not
settle produces no record at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import eval_fixtures as fx
import pytest

from referraleasy_contracts.generated.common import (
    HandoffReasonClass,
    ResultOutcome,
    WorkflowState,
)
from referraleasy_eval.capture import (
    DEFAULT_VARIANT,
    DelegationObservation,
    JourneyObservation,
    ToolObservation,
    journey_outcome,
    record_for,
    records_for,
    variant_of,
)
from referraleasy_eval.rules import RULES

PATIENT = "b0000000-0000-4000-8000-000000000001"
APPROVAL = "7d6ec885-c6b6-44be-b9c2-cab2803aef7f"
HARD = "e0000000-0000-4000-8000-000000000012"
SOFT = "e0000000-0000-4000-8000-000000000014"


def _specialist(role: str, output: Mapping[str, Any]) -> DelegationObservation:
    return DelegationObservation(
        workflow_state="X",
        inputs={role: {}},
        result={"output": {"specialist_results": {role: {"output": dict(output)}}}},
    )


def _booked(**changes: Any) -> JourneyObservation:
    """A standard booking as the replay observes it, with the shapes the agents really return."""
    observation = JourneyObservation(
        scenario_id="standard_booking",
        variant=DEFAULT_VARIANT,
        workflow_id="wf-1",
        final_state="BOOKED",
        payload={
            "source_data": {
                "preferences": {
                    "constraints": [
                        {"constraint_id": HARD, "kind": "hard", "superseded_by": None},
                        {"constraint_id": SOFT, "kind": "soft", "superseded_by": None},
                    ]
                }
            },
            "answers": [
                {
                    "state": "AWAITING_HOSPITAL_CHOICE",
                    "value": {"hospital_id": fx.STANDARD_HOSPITAL},
                },
                {"state": "AWAITING_DOCTOR_CHOICE", "value": {"provider_id": fx.STANDARD_PROVIDER}},
                {"state": "AWAITING_SLOT_CHOICE", "value": {"slot_id": fx.STANDARD_SLOT}},
            ],
            "booking_payload": {
                "patient_id": PATIENT,
                "hospital_id": fx.STANDARD_HOSPITAL,
                "provider_id": fx.STANDARD_PROVIDER,
                "slot_id": fx.STANDARD_SLOT,
            },
            "booking_authority": {"approval_id": APPROVAL},
        },
        bookings=({"booking_id": "bk-1", "state": "CONFIRMED", "approval_id": APPROVAL},),
        delegations=(
            _specialist("referral_intake", {"completed_intake": {}}),
            _specialist(
                "provider_matching",
                {
                    "candidates": [
                        {
                            "provider_id": fx.STANDARD_PROVIDER,
                            "hospital_id": fx.STANDARD_HOSPITAL,
                            "constraint_fit": [
                                {"constraint_id": HARD, "satisfied": True, "reason_code": "ok"},
                                {"constraint_id": SOFT, "satisfied": None, "reason_code": "ok"},
                            ],
                            "evidence_ids": [fx.STANDARD_EVIDENCE[0]],
                        }
                    ],
                    "decision": {"evidence_ids": [fx.STANDARD_EVIDENCE[1]]},
                },
            ),
            _specialist(
                "referral_readiness",
                {
                    "readiness": {
                        "checks": [
                            {"kind": "referral_letter", "status": "satisfied"},
                            {"kind": "prior_report", "status": "not_required"},
                        ]
                    }
                },
            ),
            _specialist("scheduling", {"slots": []}),
            _specialist("scheduling", {"booking_request": {}}),
        ),
        tool_calls=(
            ToolObservation("scheduling", "list_slots", {"hospital_id": fx.STANDARD_HOSPITAL}),
            ToolObservation(
                "scheduling", "create_booking", {"payload": {"slot_id": fx.STANDARD_SLOT}}
            ),
        ),
        latency_ms=420,
    )
    return replace(observation, **changes)


def _case(case_id: str) -> Any:
    return next(case for case in fx.dataset().cases if case.case_id == case_id)


STANDARD = "standard_booking/reaches_a_confirmed_booking"


def test_a_booked_journey_maps_to_a_complete_record() -> None:
    record = record_for(_case(STANDARD), _booked())

    assert record.outcome == "completed"
    assert record.final_state == "BOOKED"
    assert (record.selected_hospital_id, record.selected_provider_id, record.selected_slot_id) == (
        fx.STANDARD_HOSPITAL,
        fx.STANDARD_PROVIDER,
        fx.STANDARD_SLOT,
    )
    assert record.bookings[0].approval_id == APPROVAL
    assert record.bookings[0].slot_id == fx.STANDARD_SLOT
    assert record.live_approval_ids == (APPROVAL,)
    assert set(fx.STANDARD_EVIDENCE) <= set(record.evidence_ids)
    assert record.required_document_kinds == ("referral_letter",)
    assert record.attached_document_kinds == ("referral_letter",)
    assert record.booking_attempts == 1
    assert record.tool_calls_made == 2
    assert record.escalation is None


def test_delegated_roles_are_the_distinct_specialists_in_first_consulted_order() -> None:
    """The scheduler is asked twice; §14 asks which specialists were consulted, not how often."""
    record = record_for(_case(STANDARD), _booked())
    assert record.delegated_roles == (
        "referral_intake",
        "provider_matching",
        "referral_readiness",
        "scheduling",
    )


def test_an_unknown_constraint_fit_is_recorded_as_unsatisfied() -> None:
    by_code = {o.code: o for o in record_for(_case(STANDARD), _booked()).constraints}
    assert by_code[HARD].satisfied is True and by_code[HARD].kind == "hard"
    assert by_code[SOFT].satisfied is False and by_code[SOFT].detail == "unknown"


def test_no_constraint_fit_means_no_constraint_outcome_rather_than_a_pass() -> None:
    """What the matching agent ships today: `constraint_fit: []`. The rule must see nothing."""
    bare = _booked(
        delegations=(
            _specialist(
                "provider_matching",
                {"candidates": [{"provider_id": fx.STANDARD_PROVIDER, "constraint_fit": []}]},
            ),
        )
    )
    record = record_for(_case(STANDARD), bare)
    assert record.constraints == ()
    verdict = RULES["no_hard_constraint_relaxed"](_case(STANDARD), record)
    assert verdict.status == "not_applicable"


def test_a_journey_that_did_not_settle_produces_no_record() -> None:
    broken = JourneyObservation("standard_booking", DEFAULT_VARIANT, error="JourneyError: stuck")
    cases = [c for c in fx.dataset().cases if c.scenario_id == "standard_booking"]
    assert records_for(cases, {("standard_booking", DEFAULT_VARIANT): broken}) == ()


def test_a_case_is_scored_against_its_own_variant() -> None:
    variant_case = next(c for c in fx.dataset().cases if variant_of(c) != DEFAULT_VARIANT)
    other = _booked(scenario_id=variant_case.scenario_id)
    own = _booked(scenario_id=variant_case.scenario_id, final_state="HUMAN_HANDOFF", bookings=())
    records = records_for(
        [variant_case],
        {
            (variant_case.scenario_id, DEFAULT_VARIANT): other,
            (variant_case.scenario_id, variant_of(variant_case)): own,
        },
    )
    assert [r.final_state for r in records] == ["HUMAN_HANDOFF"]


def test_a_handoff_is_an_escalation_with_the_drafts_reason() -> None:
    handed_off = _booked(
        final_state="HUMAN_HANDOFF",
        bookings=(),
        payload={"handoff_draft": {"reason_class": "no_match"}},
    )
    record = record_for(_case(STANDARD), handed_off)
    assert record.escalation is not None
    assert record.escalation.reason_code == "no_match"
    assert record.outcome == "no_match"


def test_two_runs_that_settle_differently_are_reported_as_diverged() -> None:
    first = _booked()
    second = _booked(final_state="HUMAN_HANDOFF", bookings=())
    assert record_for(_case(STANDARD), replace(first, repeat=first)).outcome == "completed"
    assert record_for(_case(STANDARD), replace(first, repeat=second)).outcome == "diverged"


@pytest.mark.parametrize(
    ("state", "reason", "expected"),
    [
        ("BOOKED", None, "completed"),
        ("BOOKING_UNVERIFIED", None, "outcome_unknown"),
        ("AWAITING_SLOT_CHOICE", None, "needs_input"),
        ("HUMAN_HANDOFF", "no_match", "no_match"),
        ("HUMAN_HANDOFF", "missing_document", "terminal_error"),
        ("FAILED", None, "terminal_error"),
        ("RECOVERY", None, "retryable_error"),
    ],
)
def test_journey_outcome_is_the_contracts_vocabulary(
    state: str, reason: str | None, expected: str
) -> None:
    assert journey_outcome(state, reason) == expected
    assert expected in {o.value for o in ResultOutcome}


def test_the_booking_write_is_checked_inside_the_contracts_payload() -> None:
    """`create_booking` takes a `BookingRequest.payload`; the slot is inside it, not beside it."""
    record = record_for(_case(STANDARD), _booked())
    verdict = RULES["tool_arguments_match_the_outcome"](_case(STANDARD), record)
    assert verdict.score == 1.0
    wrong = _booked(
        tool_calls=(ToolObservation("scheduling", "create_booking", {"payload": {"slot_id": "x"}}),)
    )
    assert (
        RULES["tool_arguments_match_the_outcome"](
            _case(STANDARD), record_for(_case(STANDARD), wrong)
        ).score
        == 0.0
    )


# ---------------------------------------------------------------------------
# The dataset speaks the contract's vocabulary, or a capture can never match it
# ---------------------------------------------------------------------------


def test_every_expected_value_is_a_contract_value() -> None:
    outcomes = {o.value for o in ResultOutcome}
    states = {s.value for s in WorkflowState}
    reasons = {r.value for r in HandoffReasonClass}
    wrong = []
    for case in fx.dataset().cases:
        expectation = case.expectation
        if "outcome" in expectation and expectation["outcome"] not in outcomes:
            wrong.append((case.case_id, "outcome", expectation["outcome"]))
        if "recovered_state" in expectation and expectation["recovered_state"] not in states:
            wrong.append((case.case_id, "recovered_state", expectation["recovered_state"]))
        reason = expectation.get("escalation_reason_code")
        if reason is not None and reason not in reasons:
            wrong.append((case.case_id, "escalation_reason_code", reason))
    assert wrong == [], (
        "these expectations are not in the contract's vocabulary, so no captured run can match "
        f"them (ADR-0025): {wrong}"
    )
