"""Resolving an unknown booking outcome under the key that produced it (§7, §17).

When a booking attempt ends without a definite answer — a timeout, a malformed response, a rate
limit, an outcome the facility itself has not settled — the workflow goes to `BOOKING_UNVERIFIED`,
and the *only* way out is `lookup_request` against the same idempotency key. This module is that
loop, and it exists to make three wrong answers hard to give:

**"It probably did not go through, let us try again."** Re-sending the booking with a fresh key is the
double-booking bug. A second booking is never created here; there is no code path in this module that
builds a key, and :func:`~referraleasy_workers.errors.ensure_same_key` refuses one if it appears. A
key that comes back different is an *escalation*, not an invitation to adopt it — the facility
answering under a key we did not mint means something is wrong that a worker must not paper over.

**"The lookup failed, so the booking failed."** A lookup that fails or times out answers nothing. It
is retried against the same key with the runtime's backoff curve (honouring `Retry-After` when the
provider names one) until the budget is spent, and then it **escalates** — it never degrades into
`resolved_not_booked`, because that verdict is what authorises a new booking.

**"The budget ran out, so the answer is no."** Exhaustion is `escalated`, and escalation means a
human: `prolonged_unknown_booking` is one of §11's handoff reason classes precisely because this
situation is expected to be handed over rather than guessed at.

The loop is the *only* retry owner for the lookups. It is deliberately not wrapped in
`run_with_retries`: a retry loop inside a retry loop multiplies the budget (three attempts around
three attempts is nine lookups against a system that is already unwell), and it would make the
`lookup_attempts` this module reports a fiction.

The `pending` status is reachable and means something: the caller's deadline arrived before the
lookup budget did. That is not a failure and not a resolution — the workflow stays in
`BOOKING_UNVERIFIED` and the reconciliation is re-run later, with the same key.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Protocol

from referraleasy_agent_runtime import BudgetCaps, backoff_delay_ms
from referraleasy_contracts.generated.booking import ReconciliationState, ReconciliationStatus
from referraleasy_workers.booking import BookingApi, LookupOutcome, LookupView
from referraleasy_workers.errors import ensure_same_key

__all__ = [
    "BookingReconciler",
    "EscalationReason",
    "ReconciliationAction",
    "ReconciliationOutcome",
    "WallClock",
    "needs_confirmation",
]

#: The lookup budget when the caller (and the settings) name none. Five is the mock facility's own
#: escalation threshold, which makes our giving up and theirs the same event rather than two.
DEFAULT_LOOKUP_BUDGET: Final[int] = 5


class WallClock(Protocol):
    """The clock this module reads: wall time, for timestamps and for a caller's deadline.

    Deliberately **not** the agent runtime's `Clock`, which is a monotonic budget clock with
    `monotonic_ms`/`sleep_ms` and no `now()`. Annotating this parameter with that protocol promised a
    method it does not have, so a caller who passed a `SystemClock` got an `AttributeError` instead of
    a reconciliation — the mismatch `tests/workers/test_reconciliation.py` now pins from both sides.
    The two clocks answer different questions (how much budget is left, versus what time it is) and
    both are real, so this module names the one it needs.
    """

    def now(self) -> datetime: ...


class EscalationReason(StrEnum):
    """Why a reconciliation stopped without an answer. These go into `escalation_reason`."""

    #: The lookup budget ran out with nothing resolved.
    BUDGET_EXHAUSTED = "lookup_budget_exhausted"
    #: Two lookups that both claimed to resolve contradicted each other.
    LOOKUPS_DISAGREE = "lookups_disagree"
    #: The facility answered under a key this workflow did not mint.
    DIFFERENT_KEY = "booking_found_under_a_different_key"
    #: The facility reports *its own* reconciliation as exhausted.
    FACILITY_GAVE_UP = "facility_reconciliation_escalated"


class ReconciliationAction(StrEnum):
    """What the caller should do with the outcome. One enum, so no caller re-derives it."""

    #: An appointment exists. Do not book again; verify what it is and tell the patient.
    CONFIRM_BOOKING = "confirm_booking"
    #: No appointment exists under the key. A booking attempt may be started — under a NEW workflow
    #: position, and with the workflow's own consent, never as a silent second try.
    BOOK = "book"
    #: Nothing resolved yet, and the budget for this pass is still intact. Run it again later.
    RETRY_LATER = "retry_later"
    #: A human must decide (§11, `prolonged_unknown_booking`).
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    """The conclusion of a reconciliation pass, with the evidence that produced it."""

    status: ReconciliationStatus
    action: ReconciliationAction
    #: The one key. Carried on the outcome so a caller can assert it never changed rather than
    #: trusting that it did not.
    key: str
    lookup_attempts: int
    started_at: datetime
    finished_at: datetime
    lookups: tuple[LookupView, ...] = ()
    booking_request_id: str | None = None
    booking_state: str | None = None
    escalation_reason: EscalationReason | None = None
    detail: str = ""

    @property
    def is_resolved(self) -> bool:
        return self.status in (
            ReconciliationStatus.resolved_booked,
            ReconciliationStatus.resolved_not_booked,
        )

    @property
    def is_escalated(self) -> bool:
        return self.status is ReconciliationStatus.escalated

    @property
    def is_pending(self) -> bool:
        return self.status is ReconciliationStatus.pending

    @property
    def booked(self) -> bool:
        return self.status is ReconciliationStatus.resolved_booked

    @property
    def not_booked(self) -> bool:
        return self.status is ReconciliationStatus.resolved_not_booked

    @property
    def key_never_changed(self) -> bool:
        """Every lookup was issued for :attr:`key` — checked, not assumed."""
        return all(lookup.idempotency_key == self.key for lookup in self.lookups)

    @property
    def last_lookup_at(self) -> datetime | None:
        return self.finished_at if self.lookups else None

    @property
    def patient_message(self) -> str:
        """What can be said about an unresolved outcome without asserting an answer we do not have."""
        if self.booked:
            return "Your appointment with the hospital is confirmed."
        if self.not_booked:
            return "The hospital has confirmed there is no appointment booked for you yet."
        if self.is_pending:
            return (
                "We are still checking with the hospital whether your appointment went through. "
                "We will tell you as soon as we know."
            )
        return (
            "We have not been able to confirm with the hospital whether your appointment went "
            "through. A person on our team is picking this up and will contact you."
        )

    def state(self) -> ReconciliationState:
        """The contract-shaped summary, for storing on the workflow that owns this reconciliation."""
        return ReconciliationState(
            status=self.status,
            started_at=self.started_at,
            lookup_attempts=self.lookup_attempts,
            last_lookup_at=self.last_lookup_at,
            escalation_reason=None
            if self.escalation_reason is None
            else self.escalation_reason.value,
        )

    def attempts_summary(self) -> str:
        """One line per lookup, for an audit trail or a ticket's evidence list."""
        return (
            "no lookups were issued"
            if not self.lookups
            else "; ".join(
                f"#{index} {lookup.outcome.value} ({lookup.http_status})"
                for index, lookup in enumerate(self.lookups, start=1)
            )
        )


def needs_confirmation(lookup: LookupView) -> bool:
    """Whether a "found" answer should be read a second time before it is trusted.

    Only when the facility reports its own reconciliation as `pending`: it is telling us it has not
    settled the question either, so the record we just read may still change. Everywhere else a
    found answer is final, and reading it twice would spend budget that this workflow may need later.
    """
    return (
        lookup.outcome is LookupOutcome.BOOKING_FOUND_UNDER_SAME_KEY
        and lookup.facility_reconciliation == "pending"
    )


class BookingReconciler:
    """Resolves `outcome_unknown` under the original key, or escalates it to a human."""

    def __init__(
        self,
        *,
        api: BookingApi,
        caps: BudgetCaps,
        lookup_budget: int = DEFAULT_LOOKUP_BUDGET,
        clock: WallClock | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if lookup_budget < 1:
            raise ValueError(
                "lookup_budget must be at least 1; a reconciliation that never asks is not one"
            )
        self._api = api
        self._caps = caps
        self._lookup_budget = lookup_budget
        self._clock = clock
        self._sleep = sleep or asyncio.sleep

    async def reconcile(
        self,
        *,
        key: str,
        deadline: datetime | None = None,
        candidate_key: str | None = None,
    ) -> ReconciliationOutcome:
        """Look up ``key`` until it resolves, escalates, or the caller's deadline arrives.

        ``candidate_key`` exists only to be refused. A caller that has somehow produced a second key
        gets :class:`~referraleasy_workers.errors.SecondKeyRefusedError` instead of a lookup under it,
        which turns "never generate a second key" from a rule into a wall.
        """
        if candidate_key is not None:
            ensure_same_key(key, candidate_key)

        started_at = self._now()
        history: list[LookupView] = []
        attempts = 0

        while attempts < self._lookup_budget:
            if deadline is not None and self._now() >= deadline:
                return self._pending(
                    key, history, started_at, "deadline reached before the lookup budget"
                )
            attempts += 1
            lookup = await self._api.lookup_request(key=key)
            # The answer must be for the key we asked about. An adapter that answered for another key
            # would otherwise be indistinguishable from one that answered correctly.
            ensure_same_key(key, lookup.idempotency_key)
            history.append(lookup)

            if lookup.outcome is LookupOutcome.BOOKING_FOUND_UNDER_A_DIFFERENT_KEY:
                return self._escalate(
                    key,
                    history,
                    started_at,
                    EscalationReason.DIFFERENT_KEY,
                    f"the facility answered under key {lookup.other_key!r}; a worker must not adopt it",
                )
            if not lookup.is_answer and lookup.facility_gave_up:
                return self._escalate(
                    key,
                    history,
                    started_at,
                    EscalationReason.FACILITY_GAVE_UP,
                    "the facility reports its own reconciliation as escalated",
                )

            decided = self._decide(key, history, started_at)
            if decided is not None:
                return decided
            if attempts >= self._lookup_budget:
                break
            await self._sleep(self._delay_seconds(attempts, lookup))

        return self._escalate(
            key,
            history,
            started_at,
            EscalationReason.BUDGET_EXHAUSTED,
            f"no answer after {len(history)} lookups under the same key",
        )

    def _decide(
        self, key: str, history: list[LookupView], started_at: datetime
    ) -> ReconciliationOutcome | None:
        """Turn the answers so far into an outcome, or ``None`` when the question is still open.

        Disagreement is checked first and deliberately: a pass that saw both "it is there" and "it is
        not there" must not return whichever arrived last, and it must not average them into a
        probability. Two sources disagreeing is a human's call (§11 `conflicting_sources`).
        """
        found = [
            lookup
            for lookup in history
            if lookup.outcome is LookupOutcome.BOOKING_FOUND_UNDER_SAME_KEY
        ]
        absent = [
            lookup
            for lookup in history
            if lookup.outcome is LookupOutcome.DEFINITIVELY_ABSENT_FOR_SAME_KEY
        ]

        if found and absent:
            return self._escalate(
                key,
                history,
                started_at,
                EscalationReason.LOOKUPS_DISAGREE,
                "one lookup reported the booking exists and another that it does not",
            )
        if found:
            answer = found[0]
            if len(found) == 1 and needs_confirmation(answer):
                # The facility says the booking exists *and* that its own reconciliation is still
                # pending. Read it once more before acting, because a record that is still being
                # reconciled can still change.
                return None
            return ReconciliationOutcome(
                status=ReconciliationStatus.resolved_booked,
                action=ReconciliationAction.CONFIRM_BOOKING,
                key=key,
                lookup_attempts=len(history),
                started_at=started_at,
                finished_at=self._now(),
                lookups=tuple(history),
                booking_request_id=answer.booking_request_id,
                booking_state=answer.booking_state,
                detail="the facility reports the booking under the same key",
            )
        if absent:
            answer = absent[0]
            return ReconciliationOutcome(
                status=ReconciliationStatus.resolved_not_booked,
                action=ReconciliationAction.BOOK,
                key=key,
                lookup_attempts=len(history),
                started_at=started_at,
                finished_at=self._now(),
                lookups=tuple(history),
                booking_request_id=answer.booking_request_id,
                detail="the facility reports no booking under the same key",
            )
        return None

    def _pending(
        self, key: str, history: list[LookupView], started_at: datetime, detail: str
    ) -> ReconciliationOutcome:
        """Nothing resolved and nothing refused: the next pass continues under the same key."""
        return ReconciliationOutcome(
            status=ReconciliationStatus.pending,
            action=ReconciliationAction.RETRY_LATER,
            key=key,
            lookup_attempts=len(history),
            started_at=started_at,
            finished_at=self._now(),
            lookups=tuple(history),
            detail=detail,
        )

    def _escalate(
        self,
        key: str,
        history: list[LookupView],
        started_at: datetime,
        reason: EscalationReason,
        detail: str,
    ) -> ReconciliationOutcome:
        return ReconciliationOutcome(
            status=ReconciliationStatus.escalated,
            action=ReconciliationAction.ESCALATE,
            key=key,
            lookup_attempts=len(history),
            started_at=started_at,
            finished_at=self._now(),
            lookups=tuple(history),
            escalation_reason=reason,
            detail=detail,
        )

    def _delay_seconds(self, attempt: int, lookup: LookupView) -> float:
        """The pause before the next lookup, from the runtime's curve and the provider's own advice."""
        return backoff_delay_ms(attempt, caps=self._caps, retry_after_s=lookup.retry_after_s) / 1000

    def _now(self) -> datetime:
        return self._clock.now() if self._clock is not None else datetime.now(UTC)
