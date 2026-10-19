"""Retries: exactly one layer owns them, and a valid empty answer is not an error.

The multiplier this module guards against is subtle — httpx retrying once, the
A2A client retrying once and a model SDK retrying once turn "3 attempts" into nine
requests while every counter still reads three. So the transport is asserted, not
assumed, and the attempt accounting is asserted against the number of calls the
function under test actually made.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from agent_runtime_doubles import SPEC_ENV, FakeClock

from referraleasy_agent_runtime.budget import (
    BudgetCaps,
    BudgetExhausted,
    CallLease,
    CapKind,
    TurnBudget,
)
from referraleasy_agent_runtime.errors import UpstreamRejected, UpstreamTransient
from referraleasy_agent_runtime.http import build_http_client
from referraleasy_agent_runtime.retries import (
    NO_RETRIES,
    backoff_delay_ms,
    is_transient,
    parse_retry_after,
    retry_after_of,
    run_with_retries,
)


def _budget(clock: FakeClock, caps: BudgetCaps) -> TurnBudget:
    return TurnBudget(caps, clock=clock)


@pytest.mark.asyncio
async def test_the_transport_retries_nothing() -> None:
    """If this fails, the budget is being multiplied without the counters knowing."""
    client, profile = build_http_client(BudgetCaps.from_env(SPEC_ENV))
    try:
        assert profile.retries == NO_RETRIES == 0
        # The claim is about the real transport, not just our record of it: httpx
        # hands `retries` to the httpcore connection pool, which is where a retry
        # would actually happen, so that is the value asserted.
        pool = vars(vars(client)["_transport"])["_pool"]
        assert vars(pool)["_retries"] == 0
        assert profile.follow_redirects is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a_valid_empty_result_is_not_retried(clock: FakeClock, caps: BudgetCaps) -> None:
    """`no_match` is a completed call whose answer is "nothing matched" (§8)."""
    calls: list[int] = []

    async def search(_: object) -> list[str]:
        calls.append(1)
        return []

    outcome = await run_with_retries(
        search,
        budget=_budget(clock, caps),
        site="search_directory",
        spend=CapKind.TOOL_CALLS,
        valid_empty=lambda result: not result,
    )

    assert calls == [1], "an empty result must not be retried"
    assert outcome.attempts == 1
    assert outcome.outcome.value == "no_match"
    assert outcome.is_empty is True
    assert outcome.to_result().a2a_state.value == "completed"
    assert outcome.to_result().is_success is True


@pytest.mark.asyncio
async def test_attempts_are_capped_at_three_including_the_first(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    calls: list[int] = []

    async def flaky(_: object) -> str:
        calls.append(1)
        raise TimeoutError("upstream was slow")

    budget = _budget(clock, caps)
    with pytest.raises(BudgetExhausted) as raised:
        await run_with_retries(flaky, budget=budget, site="model:plan")

    assert len(calls) == 3
    exhausted = raised.value
    assert exhausted.cap is CapKind.ATTEMPTS
    assert exhausted.limit == 3
    assert exhausted.site == "model:plan"
    assert isinstance(exhausted.cause, TimeoutError), "the cause must survive into the trace"
    assert budget.snapshot().model_calls == 3, "each attempt is a real model call"


@pytest.mark.asyncio
async def test_a_terminal_failure_is_not_retried(clock: FakeClock, caps: BudgetCaps) -> None:
    calls: list[int] = []

    async def refused(_: object) -> str:
        calls.append(1)
        raise UpstreamRejected("peer said no", status_code=422)

    with pytest.raises(UpstreamRejected):
        await run_with_retries(refused, budget=_budget(clock, caps), site="model:plan")

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancellation_aborts_the_turn_instead_of_retrying(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    calls: list[int] = []

    async def cancelled(_: object) -> str:
        calls.append(1)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_with_retries(cancelled, budget=_budget(clock, caps), site="model:plan")

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_retry_after_is_honoured_over_the_local_curve(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    calls: list[int] = []

    async def throttled(_: object) -> str:
        calls.append(1)
        if len(calls) == 1:
            raise UpstreamTransient("429", status_code=429, retry_after_s=0.2)
        return "ok"

    outcome = await run_with_retries(throttled, budget=_budget(clock, caps), site="model:plan")

    assert outcome.value == "ok"
    assert outcome.attempts == 2
    assert clock.slept_ms == [200], "Retry-After wins over the 150 ms base backoff"


@pytest.mark.asyncio
async def test_the_backoff_grows_and_stops_at_the_max(clock: FakeClock, caps: BudgetCaps) -> None:
    calls: list[int] = []

    async def always_flaky(_: object) -> str:
        calls.append(1)
        raise TimeoutError

    with pytest.raises(BudgetExhausted):
        await run_with_retries(always_flaky, budget=_budget(clock, caps), site="model:plan")

    assert clock.slept_ms == [150, 300], "base, then double, then the turn would end"


@pytest.mark.asyncio
async def test_no_attempt_is_started_when_the_backoff_would_overrun_the_turn(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    calls: list[int] = []

    async def flaky(_: object) -> str:
        calls.append(1)
        raise TimeoutError

    # 100 ms left, and the first retry alone would wait 150 ms.
    budget = TurnBudget(caps, clock=clock, ttl_ms=100)
    with pytest.raises(BudgetExhausted) as raised:
        await run_with_retries(flaky, budget=budget, site="model:plan")

    assert raised.value.cap is CapKind.DEADLINE
    assert len(calls) == 1
    assert clock.slept_ms == [], "sleeping past the deadline would just waste the turn"


@pytest.mark.asyncio
async def test_a_write_attempt_exhausts_into_an_unknown_outcome(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    """The booking may have committed, so the run must not invite a retry-with-new-key."""

    async def flaky(_: object) -> str:
        raise TimeoutError

    budget = TurnBudget(caps, clock=clock, ttl_ms=200)
    with pytest.raises(BudgetExhausted) as raised:
        await run_with_retries(
            flaky,
            budget=budget,
            site="create_booking",
            spend=CapKind.TOOL_CALLS,
            write_attempt=True,
        )

    assert raised.value.after_write_attempt is True
    result = raised.value.to_result()
    assert result.a2a_state.value == "failed"
    assert result.requires_reconciliation is True


@pytest.mark.asyncio
async def test_the_lease_timeout_is_what_the_call_is_given(
    clock: FakeClock, caps: BudgetCaps
) -> None:
    seen: list[int] = []

    async def call(lease: CallLease) -> str:
        seen.append(lease.timeout_ms)
        return "ok"

    budget = _budget(clock, caps)
    clock.advance(29_400)

    await run_with_retries(call, budget=budget, site="model:plan")

    assert seen == [600], "the last attempt of a turn is short, and is told so"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError(), True),
        (httpx.ConnectError("no route"), True),
        (UpstreamTransient("429", status_code=429), True),
        (UpstreamTransient("503", status_code=503), True),
        (UpstreamTransient("unknown"), True),
        (UpstreamRejected("422", status_code=422), False),
        (UpstreamTransient("400", status_code=400), False),
        (ValueError("a bug, not a blip"), False),
        (BudgetExhausted(CapKind.ATTEMPTS, limit=3, used=3), False),
    ],
)
def test_only_transient_failures_are_retried(error: BaseException, expected: bool) -> None:
    assert is_transient(error) is expected


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(1, 150), (2, 300), (3, 500), (4, 500), (9, 500)],
)
def test_the_backoff_curve_is_bounded(attempt: int, expected: int) -> None:
    assert backoff_delay_ms(attempt, caps=BudgetCaps.from_env(SPEC_ENV)) == expected


def test_an_upstream_retry_after_is_not_clamped_to_the_local_curve() -> None:
    caps = BudgetCaps.from_env(SPEC_ENV)
    assert backoff_delay_ms(2, caps=caps, retry_after_s=5.0) == 5000


def test_retry_after_parses_both_of_its_forms() -> None:
    now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)

    assert parse_retry_after("2") == 2.0
    assert parse_retry_after(" 3.5 ") == 3.5
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("Wed, 14 Oct 2026 12:00:30 GMT", now=now) == 30.0
    assert parse_retry_after("-4") == 0.0
    assert parse_retry_after("Wed, 14 Oct 2026 11:00:00 GMT", now=now) == 0.0


def test_only_our_own_transient_error_carries_a_retry_after() -> None:
    assert retry_after_of(UpstreamTransient("429", status_code=429, retry_after_s=1.0)) == 1.0
    assert retry_after_of(TimeoutError()) is None
    assert retry_after_of(UpstreamRejected("422", status_code=422)) is None
