"""§14's rules, and the one property that makes them trustworthy: they say when they cannot decide.

Every test here is written so that deleting the rule it names makes it fail. That is the discipline
the rest of this repository applies to guards, and it matters more here than anywhere: an evaluation
harness whose rules silently returned nothing would report a *perfect* system — no failures, no
low scores, no critical violations — and the report would look like the best possible outcome.

So the tests come in pairs. For each rule: the case it catches, and the case where it must decline
rather than guess. The second half is the one that is usually missing.
"""

from __future__ import annotations

import pytest
from eval_fixtures import case, dataset, record_for

from referraleasy_eval.models import (
    EVAL_ERROR,
    NOT_APPLICABLE,
    SCORED,
    BookingRecord,
    ConstraintOutcome,
    Escalation,
    ToolCall,
)
from referraleasy_eval.rules import RULES


def _verdict(rule: str, case_id: str, record):
    return RULES[rule](case(case_id), record)


# ---------------------------------------------------------------------------
# The outcome, and the difference between "wrong" and "not captured"
# ---------------------------------------------------------------------------


def test_a_wrong_outcome_scores_zero_and_says_what_it_saw() -> None:
    case_id = "standard_booking/reaches_a_confirmed_booking"
    record = record_for(case_id, outcome="needs_input")
    verdict = _verdict("outcome_correctness", case_id, record)
    assert verdict.status == SCORED
    assert verdict.score == 0.0
    # The reason names both sides, because "the run was wrong" is not actionable and "the run
    # reached needs_input where succeeded was expected" is.
    assert "needs_input" in verdict.reason and "succeeded" in verdict.reason


def test_an_uncaptured_run_is_an_error_not_a_zero() -> None:
    """The distinction this whole module is built around: no record is not a failed record.

    A zero here would report the *capture* as a system property, and a harness that lost its replay
    would look like a system that had started failing.
    """
    case_id = "standard_booking/reaches_a_confirmed_booking"
    verdict = _verdict("outcome_correctness", case_id, record_for(case_id, outcome=""))
    assert verdict.status == EVAL_ERROR
    assert verdict.score is None
    assert "captured" in verdict.reason


def test_scoring_one_case_against_another_run_is_an_error() -> None:
    """A mislabelled record is caught rather than attributed to the wrong scenario."""
    verdict = _verdict(
        "outcome_correctness",
        "standard_booking/reaches_a_confirmed_booking",
        record_for("network_conflict/conflict_is_shown_as_unknown"),
    )
    assert verdict.status == EVAL_ERROR
    assert "standard_booking/reaches_a_confirmed_booking" in verdict.reason


# ---------------------------------------------------------------------------
# Groundedness and completeness
# ---------------------------------------------------------------------------


def test_groundedness_scores_the_fraction_of_expected_evidence_present() -> None:
    case_id = "standard_booking/reaches_a_confirmed_booking"
    wanted = case(case_id).expected("required_evidence_ids")

    full = _verdict("groundedness", case_id, record_for(case_id, evidence_ids=tuple(wanted)))
    assert full.status == SCORED and full.score == 1.0

    half = _verdict("groundedness", case_id, record_for(case_id, evidence_ids=(wanted[0],)))
    assert half.score == pytest.approx(0.5)

    none = _verdict("groundedness", case_id, record_for(case_id, evidence_ids=()))
    assert none.score == 0.0


def test_groundedness_declines_when_the_case_names_no_evidence() -> None:
    """A case with no evidence ids is a dataset gap, and reporting 1.0 for it would hide it."""
    verdict = _verdict(
        "groundedness",
        "no_specialist_found/evidence_for_the_gap",
        record_for("no_specialist_found/evidence_for_the_gap", evidence_ids=()),
    )
    assert verdict.status == NOT_APPLICABLE
    assert "gap in the dataset" in verdict.reason


def test_completeness_distinguishes_a_missing_document_from_a_case_that_needs_none() -> None:
    case_id = "standard_booking/referral_letter_attached"
    missing = record_for(case_id, attached_document_kinds=())
    assert _verdict("completeness", case_id, missing).score == 0.0

    complete = record_for(case_id, attached_document_kinds=("referral_letter",))
    assert _verdict("completeness", case_id, complete).score == 1.0


def test_completeness_reports_no_required_kinds_as_a_capture_gap() -> None:
    """The case expects documents and the run never said which — an error, not a poor score."""
    case_id = "standard_booking/referral_letter_attached"
    verdict = _verdict("completeness", case_id, record_for(case_id, required_document_kinds=()))
    assert verdict.status == EVAL_ERROR


# ---------------------------------------------------------------------------
# Constraints — §4's hard/soft distinction
# ---------------------------------------------------------------------------


def test_a_relaxed_hard_constraint_scores_zero_and_names_it() -> None:
    case_id = "multi_constraint_search/hard_constraints_held"
    codes = case(case_id).expected("constraint_codes")
    relaxed = tuple(
        ConstraintOutcome(code=code, kind="hard", satisfied=index > 0)
        for index, code in enumerate(codes)
    )
    verdict = _verdict(
        "no_hard_constraint_relaxed", case_id, record_for(case_id, constraints=relaxed)
    )
    assert verdict.score == 0.0
    assert codes[0] in verdict.reason


def test_a_rule_with_no_hard_constraint_declines_rather_than_passing() -> None:
    """1.0 for "there was nothing to relax" would be a passing check on an unexercised rule."""
    case_id = "standard_booking/reaches_a_confirmed_booking"
    verdict = _verdict("no_hard_constraint_relaxed", case_id, record_for(case_id, constraints=()))
    assert verdict.status == NOT_APPLICABLE
    assert "nothing to guard" in verdict.reason


def test_an_unevaluated_constraint_is_an_error_not_a_pass() -> None:
    """A constraint the run never reported on is unknown, and unknown is not satisfied."""
    case_id = "multi_constraint_search/hard_constraints_held"
    verdict = _verdict("constraints_satisfied", case_id, record_for(case_id, constraints=()))
    assert verdict.status == EVAL_ERROR
    assert "unknown, not satisfied" in verdict.reason


def test_a_soft_constraint_is_scored_the_same_way_as_a_hard_one() -> None:
    """The rules score satisfaction; §4's kind decides what a *relaxation* means, not the score."""
    # One case, scored twice — the record has to name the case it is scored against, or the
    # mislabelling rule fires (which is what this test's first draft discovered).
    case_id = "multi_constraint_search/hard_constraints_held"
    codes = case(case_id).expected("constraint_codes")
    soft = tuple(ConstraintOutcome(code=code, kind="soft", satisfied=True) for code in codes)
    record = record_for(case_id, constraints=soft)

    assert _verdict("constraints_satisfied", case_id, record).score == 1.0
    # And nothing is reported as a relaxed hard constraint, because none of them was hard.
    assert _verdict("no_hard_constraint_relaxed", case_id, record).status == NOT_APPLICABLE


# ---------------------------------------------------------------------------
# The write path: arguments, recovery, escalation, budgets
# ---------------------------------------------------------------------------


def test_a_booking_write_naming_another_slot_scores_zero() -> None:
    """Internal consistency, not path adherence: the write must name the slot the run decided on."""
    case_id = "standard_booking/booking_write_names_the_selected_slot"
    record = record_for(
        case_id,
        tool_calls=(ToolCall(name="create_booking", arguments={"slot_id": "somewhere-else"}),),
    )
    verdict = _verdict("tool_arguments_match_the_outcome", case_id, record)
    assert verdict.score == 0.0
    assert "somewhere-else" not in verdict.reason  # it names the *selected* slot instead


def test_a_run_that_wrote_no_booking_declines_the_argument_check() -> None:
    case_id = "standard_booking/booking_write_names_the_selected_slot"
    verdict = _verdict(
        "tool_arguments_match_the_outcome", case_id, record_for(case_id, tool_calls=())
    )
    assert verdict.status == NOT_APPLICABLE


def test_recovery_is_judged_by_the_end_state_not_by_the_absence_of_errors() -> None:
    case_id = "timeout_after_commit/reaches_reconciliation_not_success"
    recovered = record_for(case_id, final_state="RECONCILIATION_REQUIRED")
    assert _verdict("recovery_succeeded", case_id, recovered).score == 1.0

    stuck = record_for(case_id, final_state="BOOKING_UNVERIFIED")
    verdict = _verdict("recovery_succeeded", case_id, stuck)
    assert verdict.score == 0.0
    assert "RECONCILIATION_REQUIRED" in verdict.reason


def test_escalating_when_none_is_expected_is_a_failure_not_a_rounding_error() -> None:
    """An unnecessary handoff puts a person on a queue for a run that did not need one."""
    case_id = "standard_booking/no_unnecessary_escalation"
    record = record_for(case_id, escalation=Escalation(handoff_id="h1", reason_code="no_match"))
    verdict = _verdict("appropriate_escalation", case_id, record)
    assert verdict.score == 0.0
    assert "did not need one" in verdict.reason


def test_failing_to_escalate_when_expected_is_the_failure_that_strands_a_patient() -> None:
    case_id = "no_specialist_found/escalates_with_no_match_reason"
    verdict = _verdict("appropriate_escalation", case_id, record_for(case_id, escalation=None))
    assert verdict.score == 0.0
    assert "made none" in verdict.reason


def test_escalating_for_the_wrong_reason_routes_to_the_wrong_queue() -> None:
    case_id = "no_specialist_found/escalates_with_no_match_reason"
    record = record_for(
        case_id, escalation=Escalation(handoff_id="h1", reason_code="unknown_outcome")
    )
    verdict = _verdict("appropriate_escalation", case_id, record)
    assert verdict.score == 0.0
    assert "wrong queue" in verdict.reason


def test_the_delegation_budget_is_an_upper_bound_not_an_exact_set() -> None:
    """Which roles run is an implementation choice; only the excess is the defect (§14)."""
    case_id = "multi_constraint_search/delegation_budget_respected"
    budget = case(case_id).expected("max_delegations")

    within = record_for(case_id, delegated_roles=tuple(f"r{i}" for i in range(budget)))
    assert _verdict("delegation_budget", case_id, within).score == 1.0

    over = record_for(case_id, delegated_roles=tuple(f"r{i}" for i in range(budget + 1)))
    verdict = _verdict("delegation_budget", case_id, over)
    assert verdict.score == 0.0
    assert f"budget of {budget}" in verdict.reason


def test_latency_scores_headroom_rather_than_a_boolean() -> None:
    """A run 20% over and one four times over are different findings, and the score says so."""
    case_id = "three_transient_errors/latency_within_budget"
    budget = case(case_id).expected("latency_budget_ms")

    assert _verdict("latency_budget", case_id, record_for(case_id, latency_ms=budget)).score == 1.0
    at_double = _verdict("latency_budget", case_id, record_for(case_id, latency_ms=budget * 2))
    assert at_double.score == 0.0
    at_one_and_a_half = _verdict(
        "latency_budget", case_id, record_for(case_id, latency_ms=int(budget * 1.5))
    )
    assert 0.0 < at_one_and_a_half.score < 1.0, "the score collapsed to a boolean"


def test_a_run_that_reports_no_calls_at_all_cannot_claim_a_cost_of_zero() -> None:
    """§14's cost metric is "can you say what it cost", and "no calls" is not an answer."""
    case_id = "standard_booking/reaches_a_confirmed_booking"
    record = record_for(case_id, model_calls=0, tool_calls_made=0, cost_usd=0.0)
    verdict = _verdict("cost_reported", case_id, record)
    assert verdict.score == 0.0
    assert "unknown rather than zero" in verdict.reason


# ---------------------------------------------------------------------------
# The registry itself
# ---------------------------------------------------------------------------


def test_every_case_in_the_committed_dataset_names_only_registered_rules() -> None:
    """Belt as well as braces: `dataset.py` refuses an unknown name at load, and so does this."""
    registered = set(RULES)
    for entry in dataset().cases:
        unknown = [name for name in entry.checks if name not in registered]
        assert not unknown, f"{entry.case_id} names unregistered rule(s) {unknown}"


def test_every_registered_rule_declines_or_scores_but_never_raises() -> None:
    """A rule that raises on an empty record would take a whole run down for one bad input.

    Asserted over the registry rather than over a hand-listed subset, so a rule added later is
    covered by this test the moment it is registered.
    """
    empty = record_for("standard_booking/reaches_a_confirmed_booking", outcome="succeeded")
    for name, rule in RULES.items():
        verdict = rule(case("standard_booking/reaches_a_confirmed_booking"), empty)
        assert verdict.status in (SCORED, NOT_APPLICABLE, EVAL_ERROR), name
        assert verdict.reason, f"{name} returned a verdict with no reason"


def test_a_rule_that_raises_becomes_one_cases_error_not_the_whole_run() -> None:
    """The runner's safety net, asserted because a raise would lose the other forty-three cases."""
    from referraleasy_eval.rules import RULES as REGISTRY
    from referraleasy_eval.runner import case_result

    def exploding(entry, record):  # type: ignore[no-untyped-def]
        raise RuntimeError("a rule with a bug")

    entry = case("standard_booking/reaches_a_confirmed_booking")
    patched = dict(REGISTRY)
    patched["outcome_correctness"] = exploding
    import referraleasy_eval.runner as runner_module

    original = runner_module.RULES
    try:
        runner_module.RULES = patched  # type: ignore[assignment]
        result = case_result(entry, record_for(entry.case_id), None)
    finally:
        runner_module.RULES = original  # type: ignore[assignment]

    errors = [verdict for verdict in result.verdicts if verdict.status == EVAL_ERROR]
    assert errors, "a raising rule did not become a visible error"
    assert "RuntimeError" in errors[0].reason


def test_a_booking_record_defaults_to_having_no_approval() -> None:
    """The dataclass default is the unsafe one on purpose: an unapproved booking is the failure.

    A default of "some approval" would make the critical rule pass for a record that never named
    one, which is exactly the direction a safety default must not point.
    """
    booking = BookingRecord(
        booking_id="b1", state="CONFIRMED", provider_id="p", hospital_id="h", slot_id="s"
    )
    assert booking.approval_id is None
