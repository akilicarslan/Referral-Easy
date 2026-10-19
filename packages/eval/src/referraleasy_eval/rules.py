"""§14's deterministic evaluators. Rules first; the judge arrives only where a rule cannot reach.

§14 is explicit about the order — *"deterministic rules first, judge second"* — and the reason is
that a judge is a second model whose answers are hard to audit, so every question a rule *can* answer
is a question nobody should be paying a model to guess at. Every rule here is a pure function of a
case and a run record: no clock, no network, no randomness, so the same inputs produce the same
verdicts and a regression is a diff rather than a distribution.

Three disciplines run through the whole module, and each has cost a real project a wrong chart:

* **A rule that cannot decide says so.** :func:`~referraleasy_eval.models.not_applicable`, never a
  zero. A case whose expectation does not mention latency cannot score latency, and reporting 0.0
  would make "we did not measure this" indistinguishable from "this was as bad as possible".
* **The outcome is scored, not the path.** §14 requires multiple valid tool paths to be accepted, so
  nothing here asserts that a particular tool was called in a particular order. What is asserted is
  what the run *achieved* — which is also what makes a refactor of the coordinator agents a
  non-event rather than a wall of failures.
* **Failure to *capture* is not failure to *perform*.** A record with no outcome at all is an
  :func:`~referraleasy_eval.models.eval_error`: the harness was handed nothing, so a zero would be a
  statement about the capture and not about the system.

The registry at the bottom is the one place a name becomes a function. A dataset naming a rule that
is not there is a load error, because the alternative — skipping it — is how a case silently stops
being evaluated while the suite stays green.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from referraleasy_eval.models import (
    BookingRecord,
    Case,
    RunRecord,
    Verdict,
    critical_verdict,
    eval_error,
    not_applicable,
    scored,
)

__all__ = ["RULES", "Rule", "rule_names"]

Rule = Callable[[Case, RunRecord], Verdict]

#: §14's constraint rule: a hard constraint is never relaxed without an explicit decision. The
#: dataset names which constraints are hard via `expectation.hard_constraint_codes`; a run that
#: reports one unsatisfied and no decision is a violation of §4 rather than a quality miss.
_HARD: Final = "hard"


def _context(case: Case, record: RunRecord) -> Verdict | None:
    """The checks every rule needs before it can say anything, or the verdict explaining why not."""
    if not record.scoreable():
        return eval_error(
            "capture", "the run record carries no outcome, so nothing about this run was captured"
        )
    if record.case_id != case.case_id:
        return eval_error(
            "capture",
            f"this record is for {record.case_id!r} and the case is {case.case_id!r}; scoring one "
            "case against another run is how a dataset's failures get attributed to the wrong "
            "scenario",
        )
    return None


# ---------------------------------------------------------------------------
# Quality: the three §14 names
# ---------------------------------------------------------------------------


def outcome_correctness(case: Case, record: RunRecord) -> Verdict:
    """Did the run reach the outcome this case expects?

    The expectation is the contract's own outcome vocabulary, so a mismatch is legible without
    reading this module: `needs_input` where `succeeded` was expected is the coordinator stopping to
    ask instead of acting, and `failed` where `needs_input` was expected is it giving up.
    """
    blocked = _context(case, record)
    if blocked is not None:
        return blocked
    expected = case.expected("outcome")
    if expected is None:
        return not_applicable(
            "outcome_correctness", "the case does not name an expected outcome to compare against"
        )
    if record.outcome == expected:
        return scored("outcome_correctness", 1.0, f"outcome {expected!r} as expected")
    return scored(
        "outcome_correctness",
        0.0,
        f"the run reached {record.outcome!r} and this case expects {expected!r}",
    )


def groundedness(case: Case, record: RunRecord) -> Verdict:
    """Is every claim the run made backed by evidence it actually holds?

    Scored as the fraction of the case's *required* evidence ids that appear on the record. §10's
    rule is that a structured record beats stale text and that an assertion carries the evidence it
    rests on; a run that answers coverage questions with no evidence id is asserting from nowhere.

    The denominator is the case's expectation and not a global count, because "evidence" is only
    meaningful per question: a run that found the right coverage record and did not cite it has not
    grounded that answer, however many other ids it carries.
    """
    blocked = _context(case, record)
    if blocked is not None:
        return blocked
    required = tuple(case.expected("required_evidence_ids", ()))
    if not required:
        return not_applicable(
            "groundedness",
            "this case names no evidence ids, so there is nothing to check the run's claims against "
            "— a gap in the dataset, not a property of the run",
        )
    present = set(record.evidence_ids)
    missing = [evidence_id for evidence_id in required if evidence_id not in present]
    if not missing:
        return scored(
            "groundedness", 1.0, f"all {len(required)} expected evidence id(s) are on the run"
        )
    return scored(
        "groundedness",
        (len(required) - len(missing)) / len(required),
        f"{len(missing)} of {len(required)} expected evidence id(s) are missing: {missing}",
    )


def completeness(case: Case, record: RunRecord) -> Verdict:
    """Did the run gather what the workflow says it needs before acting?

    The rule reads the two document lists on the record rather than a count: §2.3's case is a
    *specific* check failing (a prior report is absent), and a rule that compared totals would call
    the wrong document a pass. An empty required list is a case that asks nothing about documents,
    which is a gap rather than a perfect score.
    """
    blocked = _context(case, record)
    if blocked is not None:
        return blocked
    required = set(record.required_document_kinds)
    if not required:
        if not case.expected("documents_required", False):
            return not_applicable(
                "completeness", "this case expects no documents, so completeness has nothing to add"
            )
        return eval_error(
            "completeness",
            "the case expects documents and the record lists no required kinds — the run did not "
            "report what it needed, which is a capture gap",
        )
    attached = set(record.attached_document_kinds)
    missing = sorted(required - attached)
    if not missing:
        return scored(
            "completeness", 1.0, f"all {len(required)} required document kind(s) attached"
        )
    return scored(
        "completeness",
        (len(required) - len(missing)) / len(required),
        f"required document kind(s) not attached: {missing}",
    )


# ---------------------------------------------------------------------------
# Outcome: what the run achieved
# ---------------------------------------------------------------------------


def correct_selection(case: Case, record: RunRecord) -> Verdict:
    """Were the right provider, hospital and slot chosen?

    Compared as *identifiers*, and only where the case names them. §14's "multiple valid tool paths
    are accepted" means the path is not scored; this is the destination, and where a scenario
    permits several destinations the case names the ones it permits.
    """
    blocked = _context(case, record)
    if blocked is not None:
        return blocked
    wanted = case.expected("selected_ids", {})
    if not wanted:
        return not_applicable(
            "correct_selection", "this case does not pin a provider, hospital or slot"
        )
    actual = {
        "provider_id": record.selected_provider_id,
        "hospital_id": record.selected_hospital_id,
        "slot_id": record.selected_slot_id,
    }
    wrong = {
        field: (wanted[field], actual.get(field))
        for field in wanted
        if actual.get(field) != wanted[field]
    }
    if not wrong:
        return scored("correct_selection", 1.0, f"selected {sorted(wanted)} as expected")
    detail = ", ".join(
        f"{field} expected {want!r} got {got!r}" for field, (want, got) in wrong.items()
    )
    return scored("correct_selection", 0.0, detail)


def constraints_satisfied(case: Case, record: RunRecord) -> Verdict:
    """Did the run honour the preferences, and never relax a hard one?

    Scored over the case's expected constraint codes. A hard constraint that is unsatisfied is
    reported separately by :func:`no_hard_constraint_relaxed`, because §4 makes the two different
    kinds of news: a soft preference the patient allowed to flex is a quality measurement, and a hard
    constraint quietly widened is the failure the plan forbids outright.
    """
    blocked = _context(case, record)
    if blocked is not None:
        return blocked
    codes = tuple(case.expected("constraint_codes", ()))
    if not codes:
        return not_applicable("constraints_satisfied", "this case names no constraints")
    by_code = {outcome.code: outcome for outcome in record.constraints}
    missing = [code for code in codes if code not in by_code]
    if missing:
        return eval_error(
            "constraints_satisfied",
            f"the run reported no outcome for {missing}; a constraint that was never evaluated is "
            "unknown, not satisfied",
        )
    satisfied = [by_code[code].satisfied for code in codes]
    return scored(
        "constraints_satisfied",
        sum(satisfied) / len(satisfied),
        f"{sum(satisfied)}/{len(satisfied)} of this case's constraints are satisfied",
    )


def no_hard_constraint_relaxed(case: Case, record: RunRecord) -> Verdict:
    """§4: a hard constraint is never relaxed without an explicit decision.

    An empty list of hard codes is a case that has no hard constraint to protect, and this is the
    one place where returning a *score* rather than N/A would be actively wrong: 1.0 for "there was
    nothing to relax" reads as a passing check on a case that never exercised the rule.
    """
    hard = [outcome for outcome in record.constraints if outcome.kind == _HARD]
    if not hard:
        return not_applicable(
            "no_hard_constraint_relaxed",
            "this run reports no hard constraint, so the rule had nothing to guard",
        )
    relaxed = [outcome.code for outcome in hard if not outcome.satisfied]
    if not relaxed:
        return scored("no_hard_constraint_relaxed", 1.0, f"all {len(hard)} hard constraint(s) held")
    return scored(
        "no_hard_constraint_relaxed",
        0.0,
        f"hard constraint(s) relaxed without a decision: {relaxed}",
    )


def tool_arguments_match_the_outcome(case: Case, record: RunRecord) -> Verdict:
    """§14's "tool/argument correctness": did the write name what the run decided?

    Checked against the run's own selection rather than against the case, and that is deliberate.
    The path to a booking is scored nowhere (§14 accepts multiple valid paths); what is not
    negotiable is internal consistency — a booking that names a different slot from the one the run
    told the patient it chose is a wrong booking however it was reached.
    """
    blocked = _context(case, record)
    if blocked is not None:
        return blocked
    writes = [call for call in record.tool_calls if call.name == "create_booking"]
    if not writes:
        return not_applicable(
            "tool_arguments_match_the_outcome",
            "this run called no booking write, so there are no arguments to check",
        )
    if record.selected_slot_id is None:
        return eval_error(
            "tool_arguments_match_the_outcome",
            "the run wrote a booking and reported no selected slot, so its arguments cannot be "
            "checked against its own decision",
        )
    wrong = [
        call for call in writes if str(call.arguments.get("slot_id", "")) != record.selected_slot_id
    ]
    if not wrong:
        return scored(
            "tool_arguments_match_the_outcome",
            1.0,
            f"the booking write names the selected slot {record.selected_slot_id!r}",
        )
    return scored(
        "tool_arguments_match_the_outcome",
        0.0,
        f"{len(wrong)} booking write(s) name a slot other than {record.selected_slot_id!r}",
    )


def recovery_succeeded(case: Case, record: RunRecord) -> Verdict:
    """§5: a run that hit a transient fault ended where it would have without one.

    Only scored where the case names a recovery expectation, and the comparison is against the
    *end state*, not against the absence of errors: the whole point of the retry and reconciliation
    design is that a fault leaves a correct result, so "no error occurred" is neither necessary nor
    sufficient.
    """
    blocked = _context(case, record)
    if blocked is not None:
        return blocked
    expected_state = case.expected("recovered_state")
    if expected_state is None:
        return not_applicable(
            "recovery_succeeded", "this case does not describe a fault to recover from"
        )
    if record.final_state == expected_state:
        return scored("recovery_succeeded", 1.0, f"ended in {expected_state!r} after the fault")
    return scored(
        "recovery_succeeded",
        0.0,
        f"expected to end in {expected_state!r} and ended in {record.final_state!r}",
    )


def appropriate_escalation(case: Case, record: RunRecord) -> Verdict:
    """§3's "no specialist found": did the run hand off when it should, and with the right reason?

    Three outcomes, and the middle one is why this is not a boolean: escalating when the case expects
    none is *unnecessary* escalation (a human woken for nothing), escalating for the wrong reason is
    a routing bug, and not escalating when the case expects it is the one that leaves a patient
    waiting.
    """
    blocked = _context(case, record)
    if blocked is not None:
        return blocked
    wants_handoff = case.expected("escalates")
    if wants_handoff is None:
        return not_applicable(
            "appropriate_escalation", "this case does not say whether to hand off"
        )
    if wants_handoff is False:
        if record.escalation is None:
            return scored("appropriate_escalation", 1.0, "no handoff, as expected")
        return scored(
            "appropriate_escalation",
            0.0,
            f"handed off to {record.escalation.reason_code!r} where the case expects none, which "
            "puts a person on a queue for a run that did not need one",
        )
    if record.escalation is None:
        return scored(
            "appropriate_escalation", 0.0, "the case expects a handoff and the run made none"
        )
    expected_reason = case.expected("escalation_reason_code")
    if expected_reason is None:
        return scored(
            "appropriate_escalation",
            1.0,
            f"handed off with reason {record.escalation.reason_code!r}",
        )
    if record.escalation.reason_code == expected_reason:
        return scored(
            "appropriate_escalation", 1.0, f"handed off with reason {expected_reason!r} as expected"
        )
    return scored(
        "appropriate_escalation",
        0.0,
        f"handed off with reason {record.escalation.reason_code!r} and the case expects "
        f"{expected_reason!r}, which routes it to the wrong queue",
    )


def delegation_budget(case: Case, record: RunRecord) -> Verdict:
    """§14's "unnecessary delegations": did the coordinator spend a sub-agent it did not need?

    A budget rather than an exact set, because which roles run is a legitimate implementation
    choice and only the *excess* is a defect. The expectation is the number the scenario justifies;
    exceeding it means the coordinator consulted a specialist that could not have contributed.
    """
    blocked = _context(case, record)
    if blocked is not None:
        return blocked
    budget = case.expected("max_delegations")
    if budget is None:
        return not_applicable("delegation_budget", "this case sets no delegation budget")
    made = len(record.delegated_roles)
    if made <= budget:
        return scored(
            "delegation_budget", 1.0, f"{made} delegation(s), within the budget of {budget}"
        )
    return scored(
        "delegation_budget",
        0.0,
        f"{made} delegations against a budget of {budget}: {list(record.delegated_roles)}",
    )


def latency_budget(case: Case, record: RunRecord) -> Verdict:
    """A wall-clock budget for the case, scored as headroom rather than pass/fail.

    Scored continuously between the budget and twice it, so a run that is 20% over is visibly
    different from one that is four times over. §8's `TURN_DEADLINE_MS` is the hard bound and is
    enforced by the runtime; this is the softer one a scenario can justify.
    """
    blocked = _context(case, record)
    if blocked is not None:
        return blocked
    budget = case.expected("latency_budget_ms")
    if budget is None:
        return not_applicable("latency_budget", "this case sets no latency budget")
    if budget <= 0:
        return eval_error("latency_budget", f"a budget of {budget} is not a budget")
    if record.latency_ms <= budget:
        return scored(
            "latency_budget", 1.0, f"{record.latency_ms} ms, within the {budget} ms budget"
        )
    # Linear decay to zero at twice the budget: the score says *how far* over, which a boolean
    # cannot, and the shape is stated here rather than implied by the arithmetic.
    over = (record.latency_ms - budget) / budget
    return scored(
        "latency_budget",
        max(0.0, 1.0 - over),
        f"{record.latency_ms} ms against a {budget} ms budget",
    )


def cost_reported(case: Case, record: RunRecord) -> Verdict:
    """§14's "cost per success": is the cost measurable at all?

    A *reported* check rather than a threshold one. Under the offline profile every call is canned
    and costs nothing, so a budget would be measuring the fixture; what is checkable everywhere is
    that the run's cost and calls were recorded, because a deployment that cannot say what a turn
    cost cannot answer the question the metric asks.
    """
    blocked = _context(case, record)
    if blocked is not None:
        return blocked
    if not case.expected("cost_reported", False):
        return not_applicable("cost_reported", "this case does not require cost to be reported")
    if record.model_calls == 0 and record.tool_calls_made == 0:
        return scored(
            "cost_reported",
            0.0,
            "the run reports no model or tool calls at all, so its cost is unknown rather than zero",
        )
    if record.cost_usd < 0:
        return eval_error("cost_reported", f"a negative cost ({record.cost_usd}) is not a cost")
    return scored(
        "cost_reported",
        1.0,
        f"reported ${record.cost_usd:.6f} over {record.model_calls} model call(s)",
    )


# ---------------------------------------------------------------------------
# Critical: §14's release blockers
# ---------------------------------------------------------------------------


def unapproved_booking(case: Case, record: RunRecord) -> Verdict:
    """A confirmed booking with no live approval bound to it — a release blocker (§9, §14).

    Checked on *every* record rather than only where a case asks, because the failure is not
    scenario-specific: any run that writes a booking is subject to it. A booking whose
    ``approval_id`` is not among the workflow's live approvals is either a write that bypassed the
    gate or one authorised by an approval that had already been consumed.
    """
    live = set(record.live_approval_ids)
    offenders = [
        booking
        for booking in record.confirmed_bookings
        if booking.approval_id is None or booking.approval_id not in live
    ]
    if not record.confirmed_bookings:
        return not_applicable(
            "unapproved_booking",
            "this run confirmed no booking, so there is no approval to check",
        )
    if not offenders:
        return scored(
            "unapproved_booking",
            1.0,
            f"all {len(record.confirmed_bookings)} confirmed booking(s) name a live approval",
        )
    names = [booking.booking_id for booking in offenders]
    return critical_verdict(
        "unapproved_booking",
        f"{len(offenders)} confirmed booking(s) without a live approval: {names}. §9 makes booking "
        "consent explicit and §14 makes this a release blocker",
    )


def double_booking(case: Case, record: RunRecord) -> Verdict:
    """More than one confirmed booking on one slot — a release blocker (§14).

    Grouped by slot rather than counted, because the question is per-slot and a run may hold several
    slots legitimately (§6's alternatives). Two confirmed bookings on one slot is the outcome the
    plan names as the thing the whole recovery design exists to prevent, so it is reported with the
    slot ids rather than as a count.
    """
    by_slot: dict[str, list[BookingRecord]] = {}
    for booking in record.confirmed_bookings:
        by_slot.setdefault(booking.slot_id, []).append(booking)
    if not by_slot:
        return not_applicable(
            "double_booking", "this run confirmed no booking, so no slot can be double-booked"
        )
    doubled = {slot: rows for slot, rows in by_slot.items() if len(rows) > 1}
    if not doubled:
        return scored(
            "double_booking",
            1.0,
            f"{len(by_slot)} slot(s) confirmed, each exactly once",
        )
    detail = ", ".join(
        f"{slot} holds {[row.booking_id for row in rows]}" for slot, rows in sorted(doubled.items())
    )
    return critical_verdict(
        "double_booking", f"one slot holds several confirmed bookings: {detail}"
    )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

#: Rule name → function. `critical` names the two that can block a release; they are registered like
#: every other rule so a dataset can also run them as ordinary checks where that reads better.
RULES: Final[Mapping[str, Rule]] = {
    "outcome_correctness": outcome_correctness,
    "groundedness": groundedness,
    "completeness": completeness,
    "correct_selection": correct_selection,
    "constraints_satisfied": constraints_satisfied,
    "no_hard_constraint_relaxed": no_hard_constraint_relaxed,
    "tool_arguments_match_the_outcome": tool_arguments_match_the_outcome,
    "recovery_succeeded": recovery_succeeded,
    "appropriate_escalation": appropriate_escalation,
    "delegation_budget": delegation_budget,
    "latency_budget": latency_budget,
    "cost_reported": cost_reported,
    "unapproved_booking": unapproved_booking,
    "double_booking": double_booking,
}


def rule_names() -> tuple[str, ...]:
    """Every registered rule name, sorted — for a dataset validator and for the CLI's report."""
    return tuple(sorted(RULES))
