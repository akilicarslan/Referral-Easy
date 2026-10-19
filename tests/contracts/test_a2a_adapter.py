"""The application-result -> A2A protocol-state adapter (§1.1, §5).

The mapping is normative: it is declared in the `$comment` on `A2ATaskState` in
`packages/contracts/schemas/common.json`, and these tests fail if the adapter drifts from it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from referraleasy_contracts.a2a import (
    A2A_TASK_STATE_BY_EVENT,
    A2A_TASK_STATE_BY_OUTCOME,
    TERMINAL_A2A_STATES,
    AgentTaskResult,
    ProtocolEvent,
    project_event,
    project_outcome,
)
from referraleasy_contracts.generated.common import A2ATaskState, ResultOutcome

pytestmark = pytest.mark.contract


# ── the table itself ────────────────────────────────────────────────────────────────────────


def test_mapping_is_total_over_result_outcome() -> None:
    """Every application outcome has a projection; adding one without updating the table fails."""
    assert set(A2A_TASK_STATE_BY_OUTCOME) == set(ResultOutcome)


def test_mapping_is_total_over_protocol_event() -> None:
    assert set(A2A_TASK_STATE_BY_EVENT) == set(ProtocolEvent)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (ResultOutcome.completed, A2ATaskState.completed),
        (ResultOutcome.needs_input, A2ATaskState.input_required),
        (ResultOutcome.no_match, A2ATaskState.completed),
        (ResultOutcome.retryable_error, A2ATaskState.failed),
        (ResultOutcome.terminal_error, A2ATaskState.failed),
        (ResultOutcome.outcome_unknown, A2ATaskState.failed),
    ],
)
def test_each_outcome_projects_as_declared(outcome: ResultOutcome, expected: A2ATaskState) -> None:
    assert project_outcome(outcome).a2a_state is expected


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (ProtocolEvent.PATIENT_CANCELLED, A2ATaskState.canceled),
        (ProtocolEvent.POLICY_REFUSED, A2ATaskState.rejected),
        (ProtocolEvent.AUTH_FAILED, A2ATaskState.auth_required),
    ],
)
def test_each_event_projects_as_declared(event: ProtocolEvent, expected: A2ATaskState) -> None:
    assert project_event(event).a2a_state is expected


def test_no_match_is_a_success_not_an_error() -> None:
    """§8: a valid empty result is a completed call, never retried as a failure."""
    result = project_outcome(ResultOutcome.no_match)
    assert result.is_success
    assert result.a2a_state is A2ATaskState.completed


# ── the trap: outcome_unknown is a terminal protocol state with unresolved semantics ────────


def test_outcome_unknown_is_protocol_terminal_yet_needs_reconciliation() -> None:
    """The single most dangerous projection in the system.

    `outcome_unknown` becomes `failed`, which is terminal at the protocol level, while a booking
    may exist and must still be reconciled with the SAME idempotency key (§7). A caller reading
    only the protocol state would conclude "no booking" and could double-book.
    """
    result = project_outcome(ResultOutcome.outcome_unknown)
    assert result.a2a_state is A2ATaskState.failed
    assert result.protocol_is_terminal is True
    assert result.requires_reconciliation is True
    assert result.may_have_committed_side_effects is True


def test_no_other_outcome_claims_unresolved_side_effects() -> None:
    for outcome in ResultOutcome:
        result = project_outcome(outcome)
        expected = outcome is ResultOutcome.outcome_unknown
        assert result.requires_reconciliation is expected, outcome
        assert result.may_have_committed_side_effects is expected, outcome


def test_terminal_protocol_states_are_the_expected_four() -> None:
    assert {
        A2ATaskState.completed,
        A2ATaskState.canceled,
        A2ATaskState.failed,
        A2ATaskState.rejected,
    } == TERMINAL_A2A_STATES
    assert A2ATaskState.input_required not in TERMINAL_A2A_STATES
    assert A2ATaskState.working not in TERMINAL_A2A_STATES


# ── construction cannot produce a dishonest projection ──────────────────────────────────────


def test_inconsistent_projection_is_rejected() -> None:
    with pytest.raises(ValidationError, match="is not the projection of"):
        AgentTaskResult(outcome=ResultOutcome.completed, a2a_state=A2ATaskState.failed)


def test_both_outcome_and_event_is_rejected() -> None:
    with pytest.raises(ValidationError, match="exactly one of"):
        AgentTaskResult(
            outcome=ResultOutcome.completed,
            event=ProtocolEvent.PATIENT_CANCELLED,
            a2a_state=A2ATaskState.completed,
        )


def test_neither_outcome_nor_event_is_rejected() -> None:
    with pytest.raises(ValidationError, match="exactly one of"):
        AgentTaskResult(a2a_state=A2ATaskState.completed)


def test_result_is_immutable() -> None:
    result = project_outcome(ResultOutcome.completed)
    with pytest.raises(ValidationError):
        result.a2a_state = A2ATaskState.failed  # type: ignore[misc]


# ── footgun guard ───────────────────────────────────────────────────────────────────────────


def test_strenum_cross_comparison_is_a_real_hazard_the_adapter_must_not_rely_on() -> None:
    """Both enums are StrEnum, so `==` compares VALUES, not identity.

    `ResultOutcome.completed == A2ATaskState.completed` is True even though they are different
    types with different meanings. The adapter therefore uses `is` and explicit table lookups, and
    this test pins the hazard so nobody "simplifies" it into a string comparison later.
    """
    # Typed as `object` so this reads as a runtime demonstration rather than a type error: mypy
    # rightly calls the two members non-overlapping, which is exactly the hazard being pinned.
    outcome_member: object = ResultOutcome.completed
    protocol_member: object = A2ATaskState.completed
    assert outcome_member == protocol_member
    assert outcome_member is not protocol_member
    # The values genuinely differ where it matters:
    assert ResultOutcome.needs_input.value == "needs_input"
    assert A2ATaskState.input_required.value == "input-required"
    assert project_outcome(ResultOutcome.needs_input).a2a_state is A2ATaskState.input_required
