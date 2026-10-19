"""The one layer that owns retries.

A retry is a *multiplier on the budget*. If the HTTP transport retries once, the
A2A client retries once and the model SDK retries once, a single logical attempt
can consume the turn's whole quota while the caller believes it made one call —
the caps in `budget.py` would still read "3 attempts used" while nine requests
were sent. So the rule is a single owner:

* httpx is constructed with ``retries=0`` (:func:`referraleasy_agent_runtime.http.build_http_client`);
* the A2A client is handed *that* httpx client, so it has no separate retry policy
  (:func:`referraleasy_agent_runtime.http.a2a_client_config`);
* the model SDK is never asked to retry — the loop below is the only place an
  attempt is repeated.

Every retry in the system therefore passes through :func:`run_with_retries`, and
each attempt is visible as one lease against ``AGENT_MAX_ATTEMPTS`` (3, including
the first) and one charge against the per-turn model/tool counters.

Two behaviours that are easy to get wrong and are pinned by tests:

* **A valid empty result is not an error.** ``no_match`` is a *completed* call
  whose answer is "nothing matched"; retrying it would burn the turn to ask the
  same question again.
* **``Retry-After`` is honoured.** When an upstream tells us how long to wait, the
  backoff curve yields to it — but never past the turn deadline, where we fail
  with a typed outcome instead of sleeping into the void.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final

import httpx

from referraleasy_agent_runtime.budget import (
    BudgetCaps,
    BudgetExhausted,
    CallLease,
    CapKind,
    TurnBudget,
)
from referraleasy_agent_runtime.errors import UpstreamTransient
from referraleasy_contracts import AgentTaskResult, project_outcome
from referraleasy_contracts.generated.common import ResultOutcome

__all__ = [
    "NO_RETRIES",
    "CallOutcome",
    "backoff_delay_ms",
    "is_transient",
    "parse_retry_after",
    "retry_after_of",
    "run_with_retries",
]

#: The only acceptable number of transport-level retries anywhere in the system.
NO_RETRIES: Final = 0

#: Attempts are counted from 1, so the exponent for attempt 3 is 2 → a 4x base delay.
_BACKOFF_EXPONENT_OFFSET: Final = 1


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header into a non-negative number of seconds.

    Both forms in RFC 9110 are accepted — delta-seconds (``"2"``) and an
    HTTP-date (``"Wed, 21 Oct 2026 07:28:00 GMT"``). An unparseable value returns
    ``None`` (fall back to the local backoff curve) and a date already in the
    past returns ``0.0`` (retry now), which are different answers on purpose.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        pass
    else:
        return max(0.0, seconds)

    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    reference = now if now is not None else datetime.now(UTC)
    return max(0.0, (when - reference).total_seconds())


def retry_after_of(error: BaseException) -> float | None:
    """Extract the upstream's requested delay from an error, when it carried one."""
    if isinstance(error, UpstreamTransient):
        return error.retry_after_s
    return None


def is_transient(error: BaseException) -> bool:
    """Whether an error is worth another attempt.

    Narrow on purpose. A timeout, a dropped connection, an HTTP 429/5xx and our
    own :class:`UpstreamTransient` are transient. A 4xx that is not 429 is a
    well-formed refusal — retrying it just spends the budget to receive the same
    refusal — and anything else the runtime raises (a policy refusal, a
    configuration error) must never be retried.
    """
    if isinstance(error, UpstreamTransient):
        status = error.status_code
        return status is None or status == 429 or status >= 500
    return isinstance(error, (TimeoutError, httpx.TransportError))


def backoff_delay_ms(
    retry: int,
    *,
    caps: BudgetCaps,
    retry_after_s: float | None = None,
) -> int:
    """Milliseconds to wait before the ``retry``-th retry (1 = the first retry).

    Exponential from ``RETRY_BACKOFF_BASE_MS`` — the first retry waits the base
    delay, the second twice that, and so on — and never longer than
    ``RETRY_BACKOFF_MAX_MS``. A ``Retry-After`` from the upstream takes precedence
    over the local curve and is **not** clamped to it: the upstream knows its own
    recovery time, and silently shortening it would just produce another refusal.

    No jitter: a call site is single-flight, so the classic thundering-herd reason
    for jitter does not apply, and a deterministic delay is testable.
    """
    if retry_after_s is not None:
        return max(0, round(retry_after_s * 1000))
    exponent = max(0, retry - _BACKOFF_EXPONENT_OFFSET)
    # A left shift rather than ``base * 2**exponent``: ``int.__pow__`` is typed as
    # returning ``Any`` (a negative exponent yields a float), and doubling an integer
    # is exact with a shift anyway. The exponent is clamped to ``>= 0`` above.
    scaled = caps.retry_backoff_base_ms << exponent
    return min(scaled, caps.retry_backoff_max_ms)


@dataclass(frozen=True, slots=True)
class CallOutcome[T]:
    """What one retried call actually did.

    Carries the attempt count and the application outcome, so the caller can
    record "this took 3 attempts and returned nothing" rather than a bare value.
    """

    value: T
    attempts: int
    outcome: ResultOutcome
    retry_after_s: float | None = None
    elapsed_ms: int = 0

    @property
    def is_empty(self) -> bool:
        """True for a valid ``no_match``: a successful call whose answer is "nothing matched"."""
        return self.outcome is ResultOutcome.no_match

    def to_result(self) -> AgentTaskResult:
        """Project onto the A2A protocol via the contracts adapter (never a local table)."""
        return project_outcome(self.outcome)

    def as_trace_entry(self) -> dict[str, object]:
        return {
            "attempts": self.attempts,
            "outcome": self.outcome.value,
            "retry_after_s": self.retry_after_s,
            "elapsed_ms": self.elapsed_ms,
            "empty": self.is_empty,
        }


async def run_with_retries[T](
    call: Callable[[CallLease], Awaitable[T]],
    *,
    budget: TurnBudget,
    site: str,
    spend: CapKind = CapKind.MODEL_CALLS,
    timeout_ms: int | None = None,
    valid_empty: Callable[[T], bool] | None = None,
    retryable: Callable[[BaseException], bool] = is_transient,
    retry_after: Callable[[BaseException], float | None] = retry_after_of,
    write_attempt: bool = False,
) -> CallOutcome[T]:
    """Run ``call`` under the turn budget, retrying only what is worth retrying.

    ``call`` receives the :class:`CallLease` so it can size its own timeout (and
    pass it to the transport). ``valid_empty`` marks the "ran fine, matched
    nothing" case: the result is returned immediately, as a ``no_match``
    outcome, with ``attempts == 1``.

    Raises :class:`BudgetExhausted` — the typed outcome — when the attempts,
    deadline, model-call or tool-call caps are reached. ``CancelledError`` is
    never caught: cancellation must abort the turn, not start another attempt.
    """
    started_at = budget.now_ms()
    last_failure: BaseException | None = None
    retry_after_s: float | None = None

    while True:
        try:
            # Time first (an expired turn must not be charged an attempt), then the
            # attempt claim, then the quota and the timeout.
            budget.require_time(after_write_attempt=write_attempt)
            attempt = budget.next_attempt(site)
            lease = budget.lease(
                spend,
                site=site,
                attempt=attempt,
                timeout_ms=timeout_ms,
                after_write_attempt=write_attempt,
            )
        except BudgetExhausted as exhausted:
            if last_failure is None:
                raise
            raise _exhausted_with_cause(exhausted, last_failure, site=site) from last_failure

        try:
            async with asyncio.timeout(lease.timeout_ms / 1000):
                value = await call(lease)
        except Exception as error:  # classification decides whether it is retried
            if not retryable(error):
                raise
            last_failure = error
            retry_after_s = retry_after(error)
        else:
            empty = valid_empty is not None and valid_empty(value)
            return CallOutcome(
                value=value,
                attempts=lease.attempt,
                outcome=ResultOutcome.no_match if empty else ResultOutcome.completed,
                retry_after_s=None,
                elapsed_ms=budget.now_ms() - started_at,
            )

        if lease.attempt >= budget.caps.max_attempts:
            # The third attempt is the last one, so there is nothing to wait for:
            # sleeping here would only delay a failure we already know about.
            raise BudgetExhausted(
                CapKind.ATTEMPTS,
                limit=budget.caps.max_attempts,
                used=lease.attempt,
                agent_role=budget.role,
                site=site,
                after_write_attempt=write_attempt,
                cause=last_failure,
            ) from last_failure

        delay_ms = backoff_delay_ms(lease.attempt, caps=budget.caps, retry_after_s=retry_after_s)
        remaining_ms = budget.remaining_ms()
        if delay_ms >= remaining_ms:
            # The wait alone would overrun the turn, so there is no point starting
            # another attempt: fail with the deadline outcome instead of sleeping
            # past the deadline and reporting a timeout.
            raise BudgetExhausted(
                CapKind.DEADLINE,
                limit=budget.caps.turn_deadline_ms,
                used=budget.caps.turn_deadline_ms - remaining_ms,
                agent_role=budget.role,
                site=site,
                after_write_attempt=write_attempt,
                cause=last_failure,
            ) from last_failure
        await budget.clock.sleep_ms(delay_ms)


def _exhausted_with_cause(
    exhausted: BudgetExhausted, cause: BaseException, *, site: str
) -> BudgetExhausted:
    """Re-raise a cap exhaustion that knows *why* the last attempt failed.

    A bare "3 attempts used" is unactionable; carrying the last transport error
    puts the real cause in the trace while keeping the failure typed.
    """
    return BudgetExhausted(
        exhausted.cap,
        limit=exhausted.limit,
        used=exhausted.used,
        agent_role=exhausted.agent_role,
        site=exhausted.site or site,
        after_write_attempt=exhausted.after_write_attempt,
        cause=cause,
    )
