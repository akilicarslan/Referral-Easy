"""Time and quota for one turn — the only authority on either.

The numbers (3000 ms per attempt, at most 3 attempts *including the first*, a 30 s
turn, 8 delegations / 12 model calls / 16 tool calls per turn) are the spec's, and
they are global, not per agent. That is what makes a shared runtime necessary: if
`provider_matching` kept its own clock and `scheduling` kept its own counter, an
8-way delegation tree would multiply the turn budget by its branching factor.

How the budget is shared downward
---------------------------------
A child budget is created by :meth:`TurnBudget.delegate`, and it:

* keeps the **parent's absolute deadline** — a sub-agent receives what is left of
  the turn, and cannot mint a new 30 s;
* keeps the **parent's counters object** — delegations, model calls and tool
  calls are turn-scoped, so a sub-agent's calls are the same calls;
* is clamped by the parent's remaining time on every lease, so a 3 s attempt
  timeout becomes a 40 ms attempt timeout near the end of a turn.

Consequences that are load-bearing elsewhere: no sub-agent can outlive its
parent, no sub-agent can spend quota its parent did not have, and the tree
collapses with the root.

Exhaustion is a typed outcome
-----------------------------
Running out is :class:`BudgetExhausted`, which carries *which* cap ran out, how
much was used, and an application outcome. That distinction matters most for a
booking write: a deadline that expires while `create_booking` is in flight does
not mean "no booking exists", so the exhaustion projects to ``outcome_unknown``
and the caller is sent to reconciliation instead of being invited to retry.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol

from referraleasy_agent_runtime.errors import AgentRuntimeError, ConfigurationError
from referraleasy_contracts.generated.common import ResultOutcome

if TYPE_CHECKING:
    from referraleasy_agent_runtime.roles import AgentRole

__all__ = [
    "ATTEMPT_TIMEOUT_MS_ENV",
    "BACKOFF_BASE_MS_ENV",
    "BACKOFF_MAX_MS_ENV",
    "DEFAULT_ATTEMPT_TIMEOUT_MS",
    "DEFAULT_BACKOFF_BASE_MS",
    "DEFAULT_BACKOFF_MAX_MS",
    "DEFAULT_FALLBACK_TIMEOUT_MS",
    "DEFAULT_HTTP_TOOL_TIMEOUT_MS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_DELEGATIONS_PER_TURN",
    "DEFAULT_MAX_MODEL_CALLS_PER_TURN",
    "DEFAULT_MAX_TOOL_CALLS_PER_TURN",
    "DEFAULT_TURN_DEADLINE_MS",
    "FALLBACK_TIMEOUT_MS_ENV",
    "HTTP_TOOL_TIMEOUT_MS_ENV",
    "MAX_ATTEMPTS_ENV",
    "MAX_DELEGATIONS_ENV",
    "MAX_MODEL_CALLS_ENV",
    "MAX_TOOL_CALLS_ENV",
    "TURN_DEADLINE_MS_ENV",
    "BudgetCaps",
    "BudgetExhausted",
    "BudgetSnapshot",
    "CallLease",
    "CapKind",
    "Clock",
    "SystemClock",
    "TurnBudget",
    "TurnCounters",
]

# ── Environment variable names ───────────────────────────────────────────────────────────────
ATTEMPT_TIMEOUT_MS_ENV: Final = "AGENT_ATTEMPT_TIMEOUT_MS"
MAX_ATTEMPTS_ENV: Final = "AGENT_MAX_ATTEMPTS"
TURN_DEADLINE_MS_ENV: Final = "TURN_DEADLINE_MS"
MAX_DELEGATIONS_ENV: Final = "MAX_DELEGATIONS_PER_TURN"
MAX_MODEL_CALLS_ENV: Final = "MAX_MODEL_CALLS_PER_TURN"
MAX_TOOL_CALLS_ENV: Final = "MAX_TOOL_CALLS_PER_TURN"
HTTP_TOOL_TIMEOUT_MS_ENV: Final = "HTTP_TOOL_TIMEOUT_MS"
FALLBACK_TIMEOUT_MS_ENV: Final = "FALLBACK_TIMEOUT_MS"
BACKOFF_BASE_MS_ENV: Final = "RETRY_BACKOFF_BASE_MS"
BACKOFF_MAX_MS_ENV: Final = "RETRY_BACKOFF_MAX_MS"

# ── Spec defaults (identical to .env.example; the package runs without an env file) ──────────
DEFAULT_ATTEMPT_TIMEOUT_MS: Final = 3000
DEFAULT_MAX_ATTEMPTS: Final = 3
DEFAULT_TURN_DEADLINE_MS: Final = 30_000
DEFAULT_MAX_DELEGATIONS_PER_TURN: Final = 8
DEFAULT_MAX_MODEL_CALLS_PER_TURN: Final = 12
DEFAULT_MAX_TOOL_CALLS_PER_TURN: Final = 16
DEFAULT_HTTP_TOOL_TIMEOUT_MS: Final = 1500
DEFAULT_FALLBACK_TIMEOUT_MS: Final = 3000
DEFAULT_BACKOFF_BASE_MS: Final = 150
DEFAULT_BACKOFF_MAX_MS: Final = 500


class CapKind(StrEnum):
    """Which ceiling was reached. Reported so callers can act, not just fail."""

    ATTEMPTS = "attempts"
    DELEGATIONS = "delegations"
    MODEL_CALLS = "model_calls"
    TOOL_CALLS = "tool_calls"
    DEADLINE = "deadline"


def _int_env(env: Mapping[str, str], name: str, default: int, *, minimum: int = 1) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(
            f"{name} must be an integer number of milliseconds, got {raw!r}"
        ) from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class BudgetCaps:
    """The ceilings for one turn, as data.

    Built once at startup from the environment and threaded through every call,
    so no agent decides its own limits and no test needs a running service.
    """

    attempt_timeout_ms: int = DEFAULT_ATTEMPT_TIMEOUT_MS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    turn_deadline_ms: int = DEFAULT_TURN_DEADLINE_MS
    max_delegations: int = DEFAULT_MAX_DELEGATIONS_PER_TURN
    max_model_calls: int = DEFAULT_MAX_MODEL_CALLS_PER_TURN
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS_PER_TURN
    http_tool_timeout_ms: int = DEFAULT_HTTP_TOOL_TIMEOUT_MS
    fallback_timeout_ms: int = DEFAULT_FALLBACK_TIMEOUT_MS
    retry_backoff_base_ms: int = DEFAULT_BACKOFF_BASE_MS
    retry_backoff_max_ms: int = DEFAULT_BACKOFF_MAX_MS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> BudgetCaps:
        """Resolve caps from ``env`` (defaults to ``os.environ``).

        Every value is validated at construction: a caps object that cannot be
        enforced would otherwise fail at the worst possible moment, mid-turn.
        """
        resolved: Mapping[str, str] = env if env is not None else os.environ
        return cls(
            attempt_timeout_ms=_int_env(
                resolved, ATTEMPT_TIMEOUT_MS_ENV, DEFAULT_ATTEMPT_TIMEOUT_MS
            ),
            max_attempts=_int_env(resolved, MAX_ATTEMPTS_ENV, DEFAULT_MAX_ATTEMPTS),
            turn_deadline_ms=_int_env(resolved, TURN_DEADLINE_MS_ENV, DEFAULT_TURN_DEADLINE_MS),
            max_delegations=_int_env(
                resolved, MAX_DELEGATIONS_ENV, DEFAULT_MAX_DELEGATIONS_PER_TURN
            ),
            max_model_calls=_int_env(
                resolved, MAX_MODEL_CALLS_ENV, DEFAULT_MAX_MODEL_CALLS_PER_TURN
            ),
            max_tool_calls=_int_env(resolved, MAX_TOOL_CALLS_ENV, DEFAULT_MAX_TOOL_CALLS_PER_TURN),
            http_tool_timeout_ms=_int_env(
                resolved, HTTP_TOOL_TIMEOUT_MS_ENV, DEFAULT_HTTP_TOOL_TIMEOUT_MS
            ),
            fallback_timeout_ms=_int_env(
                resolved, FALLBACK_TIMEOUT_MS_ENV, DEFAULT_FALLBACK_TIMEOUT_MS
            ),
            retry_backoff_base_ms=_int_env(resolved, BACKOFF_BASE_MS_ENV, DEFAULT_BACKOFF_BASE_MS),
            retry_backoff_max_ms=_int_env(resolved, BACKOFF_MAX_MS_ENV, DEFAULT_BACKOFF_MAX_MS),
        )

    def limit_for(self, cap: CapKind) -> int:
        """The numeric ceiling behind ``cap``."""
        return {
            CapKind.ATTEMPTS: self.max_attempts,
            CapKind.DELEGATIONS: self.max_delegations,
            CapKind.MODEL_CALLS: self.max_model_calls,
            CapKind.TOOL_CALLS: self.max_tool_calls,
            # The deadline is expressed in milliseconds, and "used" is elapsed time.
            CapKind.DEADLINE: self.turn_deadline_ms,
        }[cap]


class Clock(Protocol):
    """The time source the budget is measured against.

    Injected so the tests can advance time by 29 999 ms in a microsecond and
    assert the deadline behaviour exactly, instead of sleeping for it.
    """

    def monotonic_ms(self) -> int:
        """Milliseconds from an arbitrary origin; only differences are meaningful."""
        ...

    async def sleep_ms(self, milliseconds: int) -> None:
        """Sleep for at most ``milliseconds``; a backoff must never overrun the deadline."""
        ...


class SystemClock:
    """The real clock: ``time.monotonic`` for measurement, ``asyncio.sleep`` for pacing."""

    def monotonic_ms(self) -> int:
        return int(time.monotonic() * 1000)

    async def sleep_ms(self, milliseconds: int) -> None:
        if milliseconds > 0:
            await asyncio.sleep(milliseconds / 1000)


@dataclass(slots=True)
class TurnCounters:
    """Mutable, turn-scoped usage. Shared by reference with every child budget."""

    delegations: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    #: Attempts are counted per call site, not per turn: three attempts for
    #: `find_candidates` must not consume the attempts of the next call.
    attempts_by_site: dict[str, int] = field(default_factory=dict)

    def used(self, cap: CapKind) -> int:
        return {
            CapKind.DELEGATIONS: self.delegations,
            CapKind.MODEL_CALLS: self.model_calls,
            CapKind.TOOL_CALLS: self.tool_calls,
            CapKind.ATTEMPTS: 0,
            CapKind.DEADLINE: 0,
        }[cap]


@dataclass(frozen=True, slots=True)
class CallLease:
    """A permitted call, with the timeout it is allowed to consume.

    ``timeout_ms`` is the *minimum* of the call kind's nominal timeout and what
    remains of the turn: the last attempt of a turn is short, and being told how
    short is what lets the caller cancel cleanly instead of hitting a wall.
    """

    cap: CapKind
    attempt: int
    timeout_ms: int
    remaining_ms: int


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """An immutable reading of a budget, for traces and tests."""

    role: str | None
    depth: int
    remaining_ms: int
    delegations: int
    model_calls: int
    tool_calls: int

    def as_trace_entry(self) -> dict[str, object]:
        return {
            "role": self.role,
            "depth": self.depth,
            "remaining_ms": self.remaining_ms,
            "delegations": self.delegations,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
        }


class BudgetExhausted(AgentRuntimeError):
    """A ceiling was reached. A typed, projectable outcome — never a bare crash.

    The projection is deliberately conservative for write calls: if the cap ran
    out while a booking write may have been in flight, the outcome is
    ``outcome_unknown`` (reconciliation), not ``retryable_error``, because
    "we do not know" and "nothing happened" are different and only one of them is
    safe to retry.
    """

    def __init__(
        self,
        cap: CapKind,
        *,
        limit: int,
        used: int,
        agent_role: str | None = None,
        site: str | None = None,
        after_write_attempt: bool = False,
        cause: BaseException | None = None,
    ) -> None:
        detail = f" while {site}" if site else ""
        super().__init__(
            f"{cap.value} budget exhausted{detail}: used {used} of {limit}"
            + (f" (agent {agent_role})" if agent_role else "")
            + (f"; last error: {cause!r}" if cause is not None else "")
        )
        self.cap = cap
        self.limit = limit
        self.used = used
        self.agent_role = agent_role
        self.site = site
        self.after_write_attempt = after_write_attempt
        self.cause = cause

    @property
    def outcome(self) -> ResultOutcome | None:
        """``outcome_unknown`` after a write attempt, ``retryable_error`` otherwise.

        Caps are per turn, so "the turn ran out" is recoverable by starting a
        fresh turn — unless a write was in flight, in which case the booking's
        fate is unknown and only reconciliation (with the *same* idempotency key)
        may proceed.
        """
        if self.after_write_attempt:
            return ResultOutcome.outcome_unknown
        return ResultOutcome.retryable_error


class TurnBudget:
    """One turn's remaining time and quota, shared by everything the turn delegates to.

    Construct one per inbound task with the caps resolved at startup, hand the
    same object to the model calls, the tool calls and the child budgets, and the
    ceilings hold by construction.
    """

    __slots__ = ("_clock", "_counters", "_deadline_ms", "caps", "depth", "role")

    @classmethod
    def inherited(
        cls, caps: BudgetCaps, declaration: Mapping[str, Any], *, role: Any
    ) -> TurnBudget:
        """Apply the caller's absolute deadline and remaining quotas."""
        from referraleasy_contracts.generated.task import TaskEnvelope

        envelope = TaskEnvelope.model_validate(declaration)
        wire = envelope.model_dump(mode="json")
        declared = wire["call_budget"]
        deadline = min(
            datetime.fromisoformat(wire["deadline"].replace("Z", "+00:00")),
            datetime.fromisoformat(declared["deadline_at"].replace("Z", "+00:00")),
        )
        ttl = max(0, int((deadline - datetime.now(UTC)).total_seconds() * 1000))
        narrowed = replace(
            caps,
            max_model_calls=min(caps.max_model_calls, declared["remaining_model_calls"]),
            max_tool_calls=min(caps.max_tool_calls, declared["remaining_tool_calls"]),
            max_delegations=min(caps.max_delegations, declared["remaining_delegations"]),
        )
        return cls(narrowed, role=role, ttl_ms=min(ttl, caps.turn_deadline_ms))

    def account_remote(self, usage: Mapping[str, Any]) -> None:
        """Charge peer usage to this turn. Reject invalid or excessive counters."""
        for field_name, kind in (
            ("model_calls", CapKind.MODEL_CALLS),
            ("tool_calls", CapKind.TOOL_CALLS),
            ("delegations", CapKind.DELEGATIONS),
        ):
            value = usage.get(field_name)
            if type(value) is not int or value < 0:
                raise ConfigurationError("The peer returned invalid budget usage.")
            used = getattr(self._counters, field_name) + value
            setattr(self._counters, field_name, used)
            if used > self.caps.limit_for(kind):
                raise BudgetExhausted(kind, limit=self.caps.limit_for(kind), used=used)

    def __init__(
        self,
        caps: BudgetCaps,
        *,
        clock: Clock | None = None,
        role: AgentRole | str | None = None,
        counters: TurnCounters | None = None,
        ttl_ms: int | None = None,
        deadline_at_ms: int | None = None,
        depth: int = 0,
    ) -> None:
        """Build a budget for one turn.

        ``ttl_ms`` and ``deadline_at_ms`` are two different things and are named
        apart on purpose: the first is a *duration* ("this turn gets 30 s"), the
        second is an *instant* on the clock ("this turn ends when the root's ends").
        A child budget is constructed with the parent's instant — conflating the two
        would hand every sub-agent a fresh deadline of its own, which is precisely
        the budget-multiplication this class exists to prevent.
        """
        if ttl_ms is not None and deadline_at_ms is not None:
            raise ConfigurationError(
                "a turn budget takes either a ttl_ms (a duration) or a deadline_at_ms "
                "(an instant), never both"
            )
        self.caps = caps
        self.depth = depth
        self.role = str(role) if role is not None else None
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._counters = counters if counters is not None else TurnCounters()
        now = self._clock.monotonic_ms()
        self._deadline_ms = (
            deadline_at_ms
            if deadline_at_ms is not None
            else now + (caps.turn_deadline_ms if ttl_ms is None else ttl_ms)
        )

    # ── time ────────────────────────────────────────────────────────────────────────────────
    @property
    def clock(self) -> Clock:
        """The injected time source, so callers pace themselves on the same clock."""
        return self._clock

    def now_ms(self) -> int:
        """The current instant on this budget's clock."""
        return self._clock.monotonic_ms()

    @property
    def deadline_ms(self) -> int:
        """Absolute (clock-relative) instant this turn must be over by."""
        return self._deadline_ms

    def remaining_ms(self) -> int:
        """Milliseconds left, floored at zero. Never negative: no caller may read "extra" time."""
        return max(0, self._deadline_ms - self._clock.monotonic_ms())

    def expired(self) -> bool:
        return self.remaining_ms() <= 0

    def require_time(self, *, after_write_attempt: bool = False) -> int:
        """Return the remaining time, or raise the typed deadline outcome."""
        remaining = self.remaining_ms()
        if remaining <= 0:
            raise BudgetExhausted(
                CapKind.DEADLINE,
                limit=self.caps.turn_deadline_ms,
                used=self.caps.turn_deadline_ms,
                agent_role=self.role,
                after_write_attempt=after_write_attempt,
            )
        return remaining

    # ── quota ───────────────────────────────────────────────────────────────────────────────
    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            role=self.role,
            depth=self.depth,
            remaining_ms=self.remaining_ms(),
            delegations=self._counters.delegations,
            model_calls=self._counters.model_calls,
            tool_calls=self._counters.tool_calls,
        )

    def _spend(self, cap: CapKind, *, site: str | None = None) -> int:
        counters = self._counters
        used = counters.used(cap)
        limit = self.caps.limit_for(cap)
        if used >= limit:
            raise BudgetExhausted(cap, limit=limit, used=used, agent_role=self.role, site=site)
        if cap is CapKind.DELEGATIONS:
            counters.delegations += 1
        elif cap is CapKind.MODEL_CALLS:
            counters.model_calls += 1
        elif cap is CapKind.TOOL_CALLS:
            counters.tool_calls += 1
        return used + 1

    def next_attempt(self, site: str) -> int:
        """Claim the next attempt for ``site``, or raise ``attempts`` exhaustion.

        The first attempt is attempt 1; with ``AGENT_MAX_ATTEMPTS=3`` the third
        attempt is the last, and asking for a fourth raises rather than silently
        performing it. "At most 3 attempts **including the first**" is enforced
        here, once, for every call site.
        """
        used = self._counters.attempts_by_site.get(site, 0)
        if used >= self.caps.max_attempts:
            raise BudgetExhausted(
                CapKind.ATTEMPTS,
                limit=self.caps.max_attempts,
                used=used,
                agent_role=self.role,
                site=site,
            )
        self._counters.attempts_by_site[site] = used + 1
        return used + 1

    def attempts_used(self, site: str) -> int:
        return self._counters.attempts_by_site.get(site, 0)

    def lease(
        self,
        cap: CapKind,
        *,
        site: str | None = None,
        timeout_ms: int | None = None,
        attempt: int | None = None,
        after_write_attempt: bool = False,
    ) -> CallLease:
        """Spend one unit of ``cap`` and return the timeout the call may consume.

        The lease refuses when the turn has expired *before* spending, so an
        expired turn cannot be charged for a call it will not make.

        ``attempt`` is the number already claimed with :meth:`next_attempt` by the
        retry owner. It is passed in rather than derived here, because *one logical
        call* is what the three-attempt cap counts: four independent model calls are
        four calls, not four attempts at one.
        """
        remaining = self.remaining_ms()
        if remaining <= 0:
            raise BudgetExhausted(
                CapKind.DEADLINE,
                limit=self.caps.turn_deadline_ms,
                used=self.caps.turn_deadline_ms,
                agent_role=self.role,
                site=site,
                after_write_attempt=after_write_attempt,
            )
        if cap is CapKind.DEADLINE:
            raise ConfigurationError("a lease must be taken against a spendable cap")
        self._spend(cap, site=site)
        nominal = self._nominal_timeout(cap) if timeout_ms is None else timeout_ms
        return CallLease(
            cap=cap,
            attempt=attempt if attempt is not None else 1,
            timeout_ms=max(1, min(nominal, remaining)),
            remaining_ms=remaining,
        )

    def _nominal_timeout(self, cap: CapKind) -> int:
        if cap is CapKind.TOOL_CALLS:
            return self.caps.http_tool_timeout_ms
        return self.caps.attempt_timeout_ms

    @contextmanager
    def model_call(self, *, site: str, after_write_attempt: bool = False) -> Iterator[CallLease]:
        """Spend one model call against the turn cap, or raise the typed outcome.

        A single model call is not an *attempt*: the attempt counter belongs to a
        logical call that is being retried, and is claimed by
        ``retries.run_with_retries``.
        """
        yield self.lease(CapKind.MODEL_CALLS, site=site, after_write_attempt=after_write_attempt)

    @contextmanager
    def tool_call(
        self,
        tool: str,
        *,
        timeout_ms: int | None = None,
        after_write_attempt: bool = False,
    ) -> Iterator[CallLease]:
        """Spend one tool call against the turn cap, or raise the typed outcome."""
        yield self.lease(
            CapKind.TOOL_CALLS,
            site=tool,
            timeout_ms=timeout_ms,
            after_write_attempt=after_write_attempt,
        )

    @contextmanager
    def fallback_call(self, tool: str) -> Iterator[CallLease]:
        """Spend one tool call on a degraded path, with the longer fallback timeout.

        A fallback (a stale cache, a second directory) is allowed
        ``FALLBACK_TIMEOUT_MS`` rather than the ordinary
        ``HTTP_TOOL_TIMEOUT_MS``, but it is still one tool call against the turn
        cap — a fallback is not a free retry.
        """
        yield self.lease(CapKind.TOOL_CALLS, site=tool, timeout_ms=self.caps.fallback_timeout_ms)

    # ── delegation ──────────────────────────────────────────────────────────────────────────
    def delegate(self, role: AgentRole | str | None = None) -> TurnBudget:
        """Return the child budget for a delegated sub-agent.

        The child shares this turn's absolute deadline and counters. It cannot
        extend the deadline, cannot reset the counters, and cannot be constructed
        from the environment — a sub-agent receives what is left, nothing more.
        """
        return TurnBudget(
            self.caps,
            clock=self._clock,
            role=role,
            counters=self._counters,
            deadline_at_ms=self._deadline_ms,
            depth=self.depth + 1,
        )

    @contextmanager
    def delegation(self, role: AgentRole | str | None = None) -> Iterator[TurnBudget]:
        """Spend one delegation and yield the sub-agent's budget.

        Both ceilings apply: the delegation count is checked here, and the child
        inherits the parent's deadline so the subtree cannot outlive the turn.
        """
        self.require_time()
        self._spend(CapKind.DELEGATIONS, site=DELEGATION_SITE)
        yield self.delegate(role)


#: Call-site label used for delegation accounting.
DELEGATION_SITE: Final = "delegate_task"
