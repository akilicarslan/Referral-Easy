"""Budget accounting: the caps are the spec's, and a sub-agent cannot exceed them.

Every test here runs offline and instantaneously: the clock is virtual, so
"29 999 ms into a 30 s turn" is a fixture call rather than a sleep.
"""

from __future__ import annotations

import pytest
from agent_runtime_doubles import SPEC_ENV, FakeClock

from referraleasy_agent_runtime.budget import (
    DEFAULT_ATTEMPT_TIMEOUT_MS,
    DEFAULT_BACKOFF_BASE_MS,
    DEFAULT_BACKOFF_MAX_MS,
    DEFAULT_FALLBACK_TIMEOUT_MS,
    DEFAULT_HTTP_TOOL_TIMEOUT_MS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_DELEGATIONS_PER_TURN,
    DEFAULT_MAX_MODEL_CALLS_PER_TURN,
    DEFAULT_MAX_TOOL_CALLS_PER_TURN,
    DEFAULT_TURN_DEADLINE_MS,
    BudgetCaps,
    BudgetExhausted,
    CapKind,
    TurnBudget,
)
from referraleasy_agent_runtime.errors import ConfigurationError
from referraleasy_agent_runtime.roles import AgentRole


def test_the_caps_are_the_numbers_the_spec_states() -> None:
    caps = BudgetCaps.from_env(SPEC_ENV)
    assert caps.attempt_timeout_ms == 3000
    assert caps.max_attempts == 3
    assert caps.turn_deadline_ms == 30_000
    assert caps.max_delegations == 8
    assert caps.max_model_calls == 12
    assert caps.max_tool_calls == 16
    assert caps.http_tool_timeout_ms == 1500
    assert caps.fallback_timeout_ms == 3000
    assert caps.retry_backoff_base_ms == 150
    assert caps.retry_backoff_max_ms == 500


def test_the_spec_numbers_are_also_the_built_in_defaults() -> None:
    caps = BudgetCaps.from_env({})
    assert caps == BudgetCaps(
        attempt_timeout_ms=DEFAULT_ATTEMPT_TIMEOUT_MS,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
        turn_deadline_ms=DEFAULT_TURN_DEADLINE_MS,
        max_delegations=DEFAULT_MAX_DELEGATIONS_PER_TURN,
        max_model_calls=DEFAULT_MAX_MODEL_CALLS_PER_TURN,
        max_tool_calls=DEFAULT_MAX_TOOL_CALLS_PER_TURN,
        http_tool_timeout_ms=DEFAULT_HTTP_TOOL_TIMEOUT_MS,
        fallback_timeout_ms=DEFAULT_FALLBACK_TIMEOUT_MS,
        retry_backoff_base_ms=DEFAULT_BACKOFF_BASE_MS,
        retry_backoff_max_ms=DEFAULT_BACKOFF_MAX_MS,
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [("AGENT_MAX_ATTEMPTS", "zero"), ("TURN_DEADLINE_MS", "0"), ("MAX_TOOL_CALLS_PER_TURN", "-1")],
)
def test_an_unenforceable_cap_fails_at_startup(name: str, value: str) -> None:
    with pytest.raises(ConfigurationError, match=name):
        BudgetCaps.from_env({**SPEC_ENV, name: value})


def test_attempts_are_capped_at_three_including_the_first(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    budget = TurnBudget(caps, clock=clock, role=AgentRole.SCHEDULING)

    assert [budget.next_attempt("sql:list_slots") for _ in range(3)] == [1, 2, 3]

    with pytest.raises(BudgetExhausted) as raised:
        budget.next_attempt("sql:list_slots")
    assert raised.value.cap is CapKind.ATTEMPTS
    assert raised.value.limit == 3
    assert raised.value.used == 3


def test_attempts_are_counted_per_call_site_not_per_turn(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    budget = TurnBudget(caps, clock=clock)

    budget.next_attempt("a")
    budget.next_attempt("b")
    budget.next_attempt("b")

    assert budget.attempts_used("a") == 1
    assert budget.attempts_used("b") == 2


def test_a_lease_timeout_shrinks_to_what_is_left_of_the_turn(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    budget = TurnBudget(caps, clock=clock)

    assert budget.lease(CapKind.MODEL_CALLS, site="model").timeout_ms == 3000

    clock.advance(29_500)
    assert budget.lease(CapKind.MODEL_CALLS, site="model").timeout_ms == 500
    # A tool call keeps its own, shorter nominal timeout until the turn binds first.
    assert budget.lease(CapKind.TOOL_CALLS, site="sql:list_slots").timeout_ms == 500


def test_a_sub_agent_cannot_outlive_or_outspend_its_parent(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    parent = TurnBudget(caps, clock=clock, role=AgentRole.CARE_COORDINATOR)
    clock.advance(1_000)

    with parent.delegation(AgentRole.PROVIDER_MATCHING) as child:
        assert child.role == AgentRole.PROVIDER_MATCHING.value
        assert child.depth == 1
        # Same absolute deadline: what is left, not a fresh 30 seconds.
        assert child.deadline_ms == parent.deadline_ms
        assert child.remaining_ms() == parent.remaining_ms() == 29_000
        assert child.remaining_ms() <= parent.remaining_ms()

        # The counters are the parent's: a child's calls are the turn's calls.
        child.lease(CapKind.MODEL_CALLS, site="model")
        assert parent.snapshot().model_calls == 1

    clock.advance(29_000)
    assert parent.expired()
    for budget in (parent, child):
        with pytest.raises(BudgetExhausted) as raised:
            budget.lease(CapKind.MODEL_CALLS, site="model")
        assert raised.value.cap is CapKind.DEADLINE


def test_a_grandchild_cannot_mint_time_the_root_never_had(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    root = TurnBudget(caps, clock=clock)
    clock.advance(25_000)

    with (
        root.delegation(AgentRole.CARE_COORDINATOR) as child,
        child.delegation(AgentRole.SCHEDULING) as grandchild,
    ):
        assert grandchild.remaining_ms() == 5_000
        assert grandchild.deadline_ms == root.deadline_ms
        assert grandchild.depth == 2


def test_the_delegation_cap_is_eight_and_is_shared_downward(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    root = TurnBudget(caps, clock=clock)

    for _ in range(8):
        with root.delegation(AgentRole.PROVIDER_MATCHING):
            pass

    with pytest.raises(BudgetExhausted) as raised, root.delegation(AgentRole.PROVIDER_MATCHING):
        pass
    assert raised.value.cap is CapKind.DELEGATIONS
    assert raised.value.limit == 8

    # A delegation made by a child consumes the same, turn-scoped cap.
    fresh = TurnBudget(caps, clock=clock)
    with (
        fresh.delegation(AgentRole.CARE_COORDINATOR) as child,
        child.delegation(AgentRole.SCHEDULING),
    ):
        pass
    assert fresh.snapshot().delegations == 2


def test_model_call_and_tool_call_caps_are_twelve_and_sixteen(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    budget = TurnBudget(caps, clock=clock)
    for _ in range(12):
        with budget.model_call(site="model"):
            pass
    with pytest.raises(BudgetExhausted) as models, budget.model_call(site="model"):
        pass
    assert models.value.cap is CapKind.MODEL_CALLS

    for _ in range(16):
        with budget.tool_call("sql:list_slots"):
            pass
    with pytest.raises(BudgetExhausted) as tools, budget.tool_call("sql:list_slots"):
        pass
    assert tools.value.cap is CapKind.TOOL_CALLS


def test_the_fallback_path_is_longer_but_still_charged_once(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    budget = TurnBudget(caps, clock=clock)
    with budget.fallback_call("directory:search") as lease:
        assert lease.timeout_ms == 3000
    assert budget.snapshot().tool_calls == 1
    assert budget.snapshot().remaining_ms == 30_000


def test_exhaustion_is_a_typed_outcome_not_a_generic_error(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    budget = TurnBudget(caps, clock=clock, role=AgentRole.PROVIDER_MATCHING)
    clock.advance(30_000)

    with pytest.raises(BudgetExhausted) as raised:
        budget.require_time()

    exhausted = raised.value
    assert exhausted.cap is CapKind.DEADLINE
    assert exhausted.outcome is not None
    assert exhausted.outcome.value == "retryable_error"
    result = exhausted.to_result()
    assert result.a2a_state.value == "failed"
    assert result.requires_reconciliation is False


def test_exhaustion_during_a_booking_write_is_an_unknown_outcome(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    """The distinction that stops a double booking.

    A deadline that expires while ``create_booking`` is in flight does not mean
    "no booking exists", so the exhaustion must not invite a retry.
    """
    budget = TurnBudget(caps, clock=clock, role=AgentRole.SCHEDULING)
    clock.advance(30_000)
    with pytest.raises(BudgetExhausted) as raised:
        budget.lease(CapKind.TOOL_CALLS, site="create_booking", after_write_attempt=True)

    exhausted = raised.value
    assert exhausted.after_write_attempt is True
    assert exhausted.outcome is not None
    assert exhausted.outcome.value == "outcome_unknown"
    assert exhausted.to_result().requires_reconciliation is True


def test_a_lease_is_never_taken_against_the_deadline_cap(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    budget = TurnBudget(caps, clock=clock)
    with pytest.raises(ConfigurationError):
        budget.lease(CapKind.DEADLINE, site="model")


def test_remaining_time_is_never_negative(clock: FakeClock, caps: BudgetCaps) -> None:
    budget = TurnBudget(caps, clock=clock)
    clock.advance(120_000)
    assert budget.remaining_ms() == 0
    assert budget.expired() is True


def test_the_snapshot_is_trace_ready(clock: FakeClock, caps: BudgetCaps) -> None:
    budget = TurnBudget(caps, clock=clock, role=AgentRole.SCHEDULING)
    budget.lease(CapKind.MODEL_CALLS, site="model")
    with budget.delegation(AgentRole.PROVIDER_MATCHING):
        pass

    entry = budget.snapshot().as_trace_entry()
    assert entry == {
        "role": "scheduling",
        "depth": 0,
        "remaining_ms": 30_000,
        "delegations": 1,
        "model_calls": 1,
        "tool_calls": 0,
    }
