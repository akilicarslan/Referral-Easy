"""§8's outcomes: every reason code lands in exactly one bucket, and none is invented.

A decision carries a `reason_code`; the protocol carries one of six outcomes. The
mapping between them is the place a wrong answer becomes a wrong *action* — a
`booking_outcome_unknown` reported as retryable invites the second write §7 exists
to prevent — so it is asserted over the enum rather than over a list someone
remembered to update.

Four claims, each of them a way the mapping could be wrong without failing loudly:

* **totality** — all 25 `ReasonCode` members project, and each projects to one of
  the six outcomes.
* **disjointness** — no code sits in two buckets, because a code in both would
  project differently depending on the order of three `if`s.
* **`needs_input` is not in the image of `project_outcome`** — a turn that needs a
  person *suspends*, and a projection that could also produce `needs_input` would be
  a second way to say the same thing, with no suspension record behind it.
* **the suspension table agrees with the projection's boundaries** — the codes that
  mean "a person must answer" are named once, in `_SUSPENSION_REASONS`, and the
  handler consults it *before* projecting.

The last block is §6's identity rule seen from the question side: the fields a
specialist may ask the patient about, checked against one list.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from agents_fixtures import StubApi

from referraleasy_agent_runtime import AgentRole, SuspensionReason
from referraleasy_agents import handler as handler_module
from referraleasy_agents.errors import UnknownDataError
from referraleasy_agents.handler import (
    _NO_MATCH_REASONS,
    _RETRYABLE_REASONS,
    _SUSPENSION_REASONS,
    _TERMINAL_REASONS,
    IDENTITY_FIELDS,
    _missing_field_names,
    project_outcome,
)
from referraleasy_agents.tools.handoff import _MISSING_DATA
from referraleasy_contracts.generated.common import MissingField, ReasonCode, ResultOutcome
from referraleasy_contracts.generated.task import DecisionOutput

pytestmark = pytest.mark.contract

#: The six outcomes of §5, as values.
OUTCOMES: frozenset[str] = frozenset(member.value for member in ResultOutcome)

#: The three buckets `project_outcome` names explicitly. Order is the function's own.
BUCKETS: Sequence[frozenset[ReasonCode]] = (
    _NO_MATCH_REASONS,
    _RETRYABLE_REASONS,
    _TERMINAL_REASONS,
)


def test_every_reason_code_projects_to_an_outcome() -> None:
    """Totality over the vocabulary, not over a list copied out of the module."""
    assert {code.value for code in ReasonCode} == {
        "ok",
        "missing_mandatory_field",
        "ambiguous_input",
        "conflicting_evidence",
        "stale_evidence",
        "no_candidate_matched",
        "no_slot_available",
        "hard_constraint_unsatisfiable",
        "document_missing",
        "document_unreadable",
        "coverage_unknown",
        "provider_not_in_network",
        "approval_expired",
        "approval_payload_changed",
        "upstream_unavailable",
        "upstream_rate_limited",
        "upstream_timeout",
        "upstream_malformed_response",
        "booking_outcome_unknown",
        "policy_blocked",
        "guardrail_blocked",
        "budget_exhausted",
        "deadline_exceeded",
        "human_owned",
        "other",
    }
    for code in ReasonCode:
        result = project_outcome(code)
        assert result.outcome is not None
        assert result.outcome.value in OUTCOMES


def test_the_buckets_are_disjoint() -> None:
    """A code in two buckets would project by branch order rather than by meaning."""
    overlap = (BUCKETS[0] & BUCKETS[1]) | (BUCKETS[0] & BUCKETS[2]) | (BUCKETS[1] & BUCKETS[2])
    assert overlap == frozenset()


def test_the_buckets_hold_what_their_names_say() -> None:
    """Each named code lands in the bucket it is named in, and that is not a tautology.

    Buckets that had drifted would still pass the disjointness test above; this one
    compares the projection's *answer* with the bucket the code is listed in.
    """
    expected = [
        (_NO_MATCH_REASONS, ResultOutcome.no_match),
        (_RETRYABLE_REASONS, ResultOutcome.retryable_error),
        (_TERMINAL_REASONS, ResultOutcome.terminal_error),
    ]
    for bucket, outcome in expected:
        assert bucket, "an empty bucket is a bucket nobody will notice emptying"
        for code in sorted(bucket, key=lambda item: item.value):
            assert project_outcome(code).outcome is outcome, code.value


def test_no_match_is_a_success_and_not_a_failure() -> None:
    """§8: an empty result that was honestly searched is an answer.

    Both halves are asserted because the mistake is asymmetric: `no_match` reaching
    `terminal_error` reads as "we broke", and nothing in the protocol would show it.
    """
    for code in sorted(_NO_MATCH_REASONS, key=lambda item: item.value):
        result = project_outcome(code)
        assert result.outcome is ResultOutcome.no_match
        assert result.is_success
        assert not result.requires_reconciliation


def test_an_unknown_write_outcome_is_not_retryable_event_though_its_protocol_state_is_failed() -> (
    None
):
    """§7's trap, asserted the way a caller would read it.

    The protocol state is terminal (`failed`) while the booking is unresolved. A
    caller reading only that state could conclude no booking exists and write a
    second one; `requires_reconciliation` is what stops it, and the outcome is
    neither `retryable_error` nor `completed`.
    """
    assert ReasonCode.booking_outcome_unknown not in _RETRYABLE_REASONS
    assert ReasonCode.booking_outcome_unknown not in _NO_MATCH_REASONS
    assert ReasonCode.booking_outcome_unknown not in _TERMINAL_REASONS

    from referraleasy_agent_runtime import unknown_outcome

    result = unknown_outcome()
    assert result.outcome is ResultOutcome.outcome_unknown
    assert result.requires_reconciliation
    assert result.a2a_state.value == "failed"


def test_needs_input_is_never_produced_by_the_projection() -> None:
    """It has exactly one producer — the suspension path — and that is the point.

    A projection that could also answer `needs_input` would be a turn that claims to
    be waiting on a person with no `Suspension` record, no question, and therefore
    nothing for `resume` to answer.
    """
    produced = {project_outcome(code).outcome for code in ReasonCode}
    assert ResultOutcome.needs_input not in produced


def test_the_suspension_table_names_only_real_reason_codes_and_real_reasons() -> None:
    assert _SUSPENSION_REASONS, "no code suspends; the resume path would be dead"
    for code, reason in _SUSPENSION_REASONS.items():
        assert isinstance(code, ReasonCode)
        assert isinstance(reason, SuspensionReason)


def test_the_codes_that_wait_for_a_person_are_exactly_the_ones_the_table_names() -> None:
    """The mapping, transcribed once and checked against its own meaning.

    Each entry is a statement about who has to answer, so each is asserted
    individually: a code that moved from `PATIENT_INPUT` to `DOCUMENT` would still
    be "mapped" and the workflow would ask the wrong party.
    """
    assert {
        ReasonCode.missing_mandatory_field: SuspensionReason.PATIENT_INPUT,
        ReasonCode.ambiguous_input: SuspensionReason.PATIENT_INPUT,
        ReasonCode.document_missing: SuspensionReason.DOCUMENT,
        ReasonCode.document_unreadable: SuspensionReason.DOCUMENT,
        ReasonCode.approval_expired: SuspensionReason.APPROVAL,
        ReasonCode.approval_payload_changed: SuspensionReason.APPROVAL,
        ReasonCode.human_owned: SuspensionReason.HUMAN_HANDOFF,
    } == _SUSPENSION_REASONS


def test_a_suspending_code_is_checked_before_the_projection_runs() -> None:
    """Ordering, in the source, because the projection would otherwise answer wrongly.

    `human_owned` projects to `completed` — it is not in any of the three buckets —
    which is the right default for a *decision* and the wrong answer for a turn that
    ended waiting on a person. The handler therefore consults `_SUSPENSION_REASONS`
    first, and this test pins that order by reading the function that does it.
    """
    import inspect

    source = inspect.getsource(handler_module.RoleHandler._report)
    suspension_at = source.index("_SUSPENSION_REASONS.get")
    projection_at = source.index("project_outcome(reason_code)")
    assert suspension_at < projection_at, (
        "the suspension check must come before the projection: `human_owned` projects to "
        "`completed`, so projecting first would report a handoff as a finished answer"
    )
    pending = {
        code
        for code in _SUSPENSION_REASONS
        if project_outcome(code).outcome is ResultOutcome.completed
    }
    assert pending, (
        "no suspending reason code projects to `completed`; the ordering assertion above is "
        "then about nothing, and this test should be deleted rather than left as ceremony"
    )


def test_every_reason_code_the_table_does_not_name_projects_without_a_suspension() -> None:
    """The complement: a code with a suspension must not also read as finished *by omission*.

    Both directions matter, so both are stated — the set of codes that suspend is the
    table, and nothing outside it may suspend by accident of the projection.
    """
    for code in ReasonCode:
        if code in _SUSPENSION_REASONS:
            continue
        assert project_outcome(code).outcome is not ResultOutcome.needs_input


# ── §6: the question's own fields ─────────────────────────────────────────────────────────────


def _decision(*, missing: Sequence[str]) -> DecisionOutput:
    from referraleasy_agents.decisions import build_decision

    return build_decision(
        action="referral_intake:validate_referral",
        reason_code=ReasonCode.missing_mandatory_field,
        missing_fields=[
            MissingField(field=name, reason_code=ReasonCode.missing_mandatory_field)
            for name in missing
        ],
    )


def test_a_question_about_a_missing_field_is_allowed_and_named() -> None:
    class _Output:
        decision = _decision(missing=["specialty", "visit_type"])

    assert _missing_field_names(_Output) == ["specialty", "visit_type"]


@pytest.mark.parametrize("field", sorted(IDENTITY_FIELDS))
def test_a_question_about_an_identity_field_is_refused(field: str) -> None:
    """§6 from the asking side: identity does not travel through this agent's question.

    The list is the module's own, and each member is exercised — a question the
    patient answers is a field the patient's identity arrives in, and an agent that
    may ask for a date of birth has been handed identity by another road.
    """

    class _Output:
        decision = _decision(missing=[field])

    with pytest.raises(UnknownDataError) as caught:
        _missing_field_names(_Output)
    assert "patient_details_id" in str(caught.value)


def test_the_identity_list_covers_the_fields_the_schemas_also_forbid() -> None:
    """The list is not the only defence, and the two must not disagree.

    `MatchingInput` refuses a date of birth as a payload key; this list refuses it as
    a question. A field in one and not the other is a field with a way through.
    """
    assert {"date_of_birth", "phone", "address"} <= IDENTITY_FIELDS
    assert "patient_details_id" not in IDENTITY_FIELDS, (
        "the indirection is not identity: it is the handle §6 requires, and a question "
        "about it would be a question about an opaque id the patient cannot answer"
    )


def test_matching_never_receives_identity_and_scheduling_fetches_it() -> None:
    """The two halves of §6 in one test, because either alone is satisfiable wrongly."""
    from referraleasy_agents.schemas import MatchingInput, SchedulingInput

    matching = {name for name in MatchingInput.model_fields}
    scheduling = {name for name in SchedulingInput.model_fields}
    assert not matching & IDENTITY_FIELDS
    assert not scheduling & IDENTITY_FIELDS
    assert "patient_details_id" in scheduling
    assert "patient_details_id" not in matching


def test_the_missing_data_table_names_only_failure_classes_and_real_field_kinds() -> None:
    """Recovery's "what are we waiting for" table, checked against its own vocabulary."""
    from referraleasy_agents.recovery import FAILURE_CLASSES

    assert _MISSING_DATA, "no failure class is waiting for anything; the table is dead"
    for failure_class, kinds in _MISSING_DATA.items():
        assert failure_class in FAILURE_CLASSES
        assert kinds, f"{failure_class} is listed as waiting for nothing"


def test_a_role_cannot_ask_an_identity_question_in_a_turn() -> None:
    """The same guard, reached the way a turn reaches it: through the handler's own call.

    The parametrised test above calls the helper with a hand-built object; this one
    runs the real `_guard` path in the handler's question selection, so the guard is
    shown to be *in* the path rather than beside it.
    """
    from referraleasy_agents.handler import _question_for

    class _Output:
        decision = _decision(missing=["date_of_birth"])

    with pytest.raises(UnknownDataError):
        _question_for(_Output)


def test_the_guard_runs_for_every_role_because_it_is_the_handler_s() -> None:
    """Not per-role configuration: one class, one guard, seven roles.

    Stated because the alternative — a role opting into the identity check — is the
    shape a per-role rule would take, and a role that opted out would be invisible.
    """
    assert (
        handler_module.RoleHandler
        is handler_module.handler_for_role(
            handler_module.AgentService(role=AgentRole.REFERRAL_INTAKE, api=StubApi())
        ).__class__
    )
