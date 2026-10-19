"""Dedup and cooldown: a rule fires once, and its resolution is an event of its own.

    The scheduler queries OpenSearch and uses deduplication/cooldown to produce alert events and
    mock email outbox entries.
    The resolved event is also visible. A notification failure does not start a new infinite email
    loop.

The scheduler runs on an interval and evaluates the same rules against overlapping windows, so the
naive result is an alert every tick for a condition that has not changed. Three rules keep that from
becoming a mailbox:

**A transition is an event, a state is not.** Only a change from not-firing to firing opens an alert,
and only the reverse resolves it. A rule that stays firing is silent; that is what "dedup" means
here, and it is why the engine reasons about *previous* state rather than about the evaluations
alone.

**A resolution is delivered too.** §16 asks for it explicitly, and an alerting system that only
announces openings leaves every reader to guess when it ended — the same failure as a chart that
draws only failures.

**Cooldown bounds a re-open, and is not a mute.** A rule that resolves and fires again within the
cooldown does not produce a second notification, but the *state* is still firing and that is still
what the page shows. A cooldown that suppressed the state would make the dashboard and the mailbox
disagree.

The store is a Protocol with an in-memory implementation here. The durable one is Postgres, and it is
not in this package: what a rule decided and what an operator was told are different records, and the
second belongs with the outbox that delivers it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from referraleasy_alerts.models import AlertEvent, Evaluation, RuleName, Severity, State

__all__ = [
    "SEVERITIES",
    "AlertState",
    "Cooldown",
    "Decision",
    "Engine",
    "TransitionStore",
]

#: What each §16 rule is worth when it fires. A threshold set, like the rules' own: an operator tuning
#: the rules is the same person deciding how loudly to be told.
SEVERITIES: dict[RuleName, Severity] = {
    RuleName.CIRCUIT_OPEN: Severity.PAGE,
    RuleName.TERMINAL_FAILURE_RATE: Severity.PAGE,
    RuleName.SUSTAINED_LATENCY: Severity.WARN,
    RuleName.QUALITY_DROP: Severity.WARN,
}


@dataclass
class AlertState:
    """What is known about one `(rule, series)` pair right now."""

    firing: bool = False
    since: datetime | None = None
    #: When this pair last produced a *notification*. Separate from `since` because a re-open inside
    #: the cooldown updates the state and not the mailbox.
    notified_at: datetime | None = None
    last_detail: str = ""


@dataclass(frozen=True, slots=True)
class Cooldown:
    """How long a re-open stays quiet.

    Fifteen minutes by default, and the number matters less than the direction: too long and a
    second, genuinely different incident is swallowed; too short and a flapping dependency fills an
    inbox. §16 does not say, so this is a setting with a stated default rather than a rule.
    """

    minutes: float = 15

    def allows(self, last_notified: datetime | None, now: datetime) -> bool:
        if last_notified is None:
            return True
        return now - last_notified >= timedelta(minutes=self.minutes)


@dataclass(frozen=True, slots=True)
class Decision:
    """One evaluation's outcome: the state it leaves behind, and whether anyone is told."""

    evaluation: Evaluation
    series: str
    #: The event to record, or `None` when nothing changed — the common case, every tick.
    event: AlertEvent | None
    #: Whether the event (or the still-firing state) is quiet because of the cooldown.
    suppressed_by_cooldown: bool

    @property
    def key(self) -> str:
        return f"{self.evaluation.rule}:{self.series}" if self.series else str(self.evaluation.rule)


class TransitionStore(Protocol):
    """Where the previous state lives. A Protocol so the engine can be tested without a database.

    `items` is part of the port rather than an optional extra: "what is firing right now" is what the
    admin page asks, and a store that could not answer it would leave the engine with no way to
    report state except by replaying every event it had ever emitted.
    """

    def get(self, key: str) -> AlertState: ...

    def put(self, key: str, state: AlertState) -> None: ...

    def items(self) -> Iterable[tuple[str, AlertState]]: ...


@dataclass
class InMemoryTransitions:
    """The obvious implementation. Not durable, and says so: a restart forgets what was firing, which
    means one re-notification per alert after a restart rather than a missed one — the safe direction
    to be wrong in."""

    states: dict[str, AlertState] = field(default_factory=dict)

    def get(self, key: str) -> AlertState:
        return self.states.get(key, AlertState())

    def put(self, key: str, state: AlertState) -> None:
        self.states[key] = state

    def items(self) -> Iterable[tuple[str, AlertState]]:
        return tuple(self.states.items())


class Engine:
    """Turns a stream of evaluations into transitions, cooldown-aware.

    Stateless itself — the previous state is in the store — so two schedulers sharing a store would
    each see the other's transitions rather than each opening the same alert. Two schedulers sharing
    a store is still one scheduler too many; `SKIP LOCKED` and the single-owner rule live with the
    durable store, not here.
    """

    def __init__(self, store: TransitionStore, *, cooldown: Cooldown | None = None) -> None:
        self._store = store
        self._cooldown = cooldown or Cooldown()

    def decide(self, evaluation: Evaluation, *, now: datetime, series: str = "") -> Decision:
        """Apply one evaluation to the state, and say what should happen next.

        `UNMEASURED` leaves the previous state alone. That is the consequential choice in this method:
        a rule that cannot decide has not resolved anything, and treating a gap as a resolution would
        close an alert because the telemetry stopped arriving — the exact moment an operator most
        needs it to stay open.
        """
        key = f"{evaluation.rule}:{series}" if series else str(evaluation.rule)
        previous = self._store.get(key)

        if evaluation.state is State.UNMEASURED:
            previous.last_detail = evaluation.detail
            self._store.put(key, previous)
            return Decision(
                evaluation=evaluation,
                series=series,
                event=None,
                suppressed_by_cooldown=False,
            )

        firing = evaluation.state is State.FIRING
        if firing == previous.firing:
            previous.last_detail = evaluation.detail
            self._store.put(key, previous)
            return Decision(
                evaluation=evaluation,
                series=series,
                event=None,
                suppressed_by_cooldown=False,
            )

        allowed = self._cooldown.allows(previous.notified_at, now)
        # **The cooldown bounds a re-open, not a resolution.** Suppressing the resolve would tell an
        # operator that something fired and never tell them it stopped — leaving them holding an
        # incident that is over, which is the opposite of what §16 asks for when it says the resolved
        # event must be visible. A flapping rule is quieted by suppressing its *re-openings*; the
        # close always goes out.
        notifiable = allowed or not firing
        event: AlertEvent | None = None
        if notifiable:
            event = AlertEvent(
                rule=evaluation.rule,
                severity=SEVERITIES.get(evaluation.rule, Severity.INFO),
                opened=firing,
                at=now.isoformat(),
                detail=evaluation.detail,
                evaluation=evaluation,
            )
            previous.notified_at = now

        previous.firing = firing
        previous.since = now
        previous.last_detail = evaluation.detail
        self._store.put(key, previous)

        return Decision(
            evaluation=evaluation,
            series=series,
            event=event,
            suppressed_by_cooldown=not notifiable,
        )

    def decide_all(
        self, evaluations: Iterable[tuple[Evaluation, str]], *, now: datetime
    ) -> list[Decision]:
        """One tick: every rule that was evaluated, in the order the caller listed them."""
        return [
            self.decide(evaluation, now=now, series=series) for evaluation, series in evaluations
        ]

    def firing(self) -> dict[str, AlertState]:
        """What is firing right now, for the admin page. Read from the store, not from a log.

        Built from state rather than from the events the engine produced: a cooldown suppresses a
        *notification*, not a transition, so an alert that re-opened quietly is still firing here.
        Deriving this from the notification log would make the page and the mailbox disagree about
        the same system.
        """
        return {key: state for key, state in self._store.items() if state.firing}
