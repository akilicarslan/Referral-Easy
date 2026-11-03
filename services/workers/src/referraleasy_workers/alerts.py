"""§16's alert scheduler: query the telemetry indices on an interval, run the four rules, keep state.

    The scheduler queries OpenSearch and uses deduplication/cooldown to produce alert events and mock
    email outbox entries. The resolved event is also visible. A notification failure does not start
    a new infinite email loop.

`referraleasy_alerts` decides; this module measures, stores and runs on a clock. One tick:

1. **Measure** (:func:`measure`) — §15's queries over the windows each rule needs. A query that
   fails produces an ``unmeasured`` evaluation carrying the error, never an ``ok``.
2. **Decide** — the package's :class:`~referraleasy_alerts.Engine`, over durable state
   (:class:`PostgresAlertStorage`), so a restart does not forget what was firing.
3. **Record** — each transition becomes one ``alert_event`` row and one ``alert_message`` row
   (migration 017), each enforced unique, so a retried tick cannot compose a second email.
4. **Deliver** — to the log (:class:`LogSender`). Nothing is ever sent; §16 asks for a *mock* outbox.

**One rule is not evaluated, and the run says so.** ``circuit_open`` takes a fact — is the breaker
open — and no component publishes breaker state: the runtime moves a workflow to recovery after the
third transient failure, but emits no breaker event. Reporting the rule as ``ok`` would be inventing
the fact, so every run records it under ``skipped`` with that reason.

**One owner at a time.** A tick takes a transaction-scoped advisory lock and skips when another
scheduler holds it, so two replicas never open the same alert twice.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

from referraleasy_alerts import (
    AlertEvent,
    AlertState,
    CircuitBreakerOpen,
    Cooldown,
    DeliveryState,
    Engine,
    Evaluation,
    LatencyWindow,
    QualityDrop,
    QualityWindow,
    RateWindow,
    State,
    SustainedLatency,
    TerminalFailureRate,
    compose,
)
from referraleasy_telemetry import Sample
from referraleasy_telemetry.query import Bucket, Percentiles, TimeRange
from referraleasy_workers.db import fetch_all, fetch_one

__all__ = [
    "ALERT_LOCK_KEY",
    "CIRCUIT_SKIP_REASON",
    "AlertScheduler",
    "AlertSettings",
    "AlertStorage",
    "InMemoryAlertStorage",
    "LogSender",
    "MeasurementSource",
    "PostgresAlertStorage",
    "measure",
    "read_alerts",
]

_log = logging.getLogger("referraleasy.workers.alerts")

#: The advisory lock a tick holds. An arbitrary constant, named so every scheduler agrees on it.
ALERT_LOCK_KEY: Final = 0x7265_616C_6572  # "realer"

#: Why `circuit_open` is not evaluated — recorded on every run rather than once in a README.
CIRCUIT_SKIP_REASON: Final = (
    "no component publishes circuit-breaker state: the runtime moves a workflow to recovery after "
    "the third transient failure but emits no breaker event, so there is no fact to evaluate and "
    "reporting 'closed' would invent one"
)

#: `.invalid` is reserved (RFC 2606): the mock outbox's default recipient cannot be a real mailbox.
DEFAULT_RECIPIENT: Final = "on-call@referraleasy.invalid"
DEFAULT_TIMEZONE: Final = "America/Chicago"

#: The §15 outcome class that counts as a terminal failure for §16's first rule. `blocked` (a
#: guardrail refusal) and `success_empty` are outcomes the system meant; `error` is the one it did not.
FAILING_CLASS: Final = "error"


class MeasurementSource(Protocol):
    """The three §15 queries the rules need. `TelemetryQuery` implements it."""

    def outcome_breakdown(
        self, *, time_range: TimeRange, filters: Any = None
    ) -> dict[str, Sample]: ...

    def latency(
        self,
        *,
        time_range: TimeRange,
        kind: str = "attempt",
        filters: Any = None,
        include_errors: bool = True,
    ) -> Percentiles: ...

    def evaluation_scores(self, *, time_range: TimeRange, filters: Any = None) -> list[Bucket]: ...


@dataclass(frozen=True, slots=True)
class AlertSettings:
    """§16's settings, from the variables `.env.example` already declares."""

    window_seconds: float = 300.0
    failure_rate: float = 0.20
    min_invocations: int = 20
    cooldown_seconds: float = 900.0
    quality_window_hours: float = 24.0
    quality_baseline_days: float = 7.0
    recipient: str = DEFAULT_RECIPIENT
    timezone: str = DEFAULT_TIMEZONE
    interval_seconds: float = 60.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AlertSettings:
        source = os.environ if env is None else env

        def number(name: str, default: float) -> float:
            raw = source.get(name, "").strip()
            return float(raw) if raw else default

        return cls(
            window_seconds=number("ALERT_WINDOW_SECONDS", 300.0),
            failure_rate=number("ALERT_FAILURE_RATE_THRESHOLD", 0.20),
            min_invocations=int(number("ALERT_MIN_INVOCATIONS", 20)),
            cooldown_seconds=number("ALERT_COOLDOWN_SECONDS", 900.0),
            interval_seconds=number("ALERT_INTERVAL_SECONDS", 60.0),
            recipient=source.get("ALERT_RECIPIENT", "").strip() or DEFAULT_RECIPIENT,
            timezone=source.get("EVAL_TIMEZONE", "").strip() or DEFAULT_TIMEZONE,
        )


def _failed(rule: Any, error: Exception) -> Evaluation:
    """A query that could not run is unmeasured, with the error, and is never `ok`."""
    return Evaluation(
        rule=rule.name,
        state=State.UNMEASURED,
        detail=f"the telemetry query failed ({type(error).__name__}: {error}); nothing was measured",
        observed="query failed",
        threshold=rule.threshold,
        sample=Sample(0, 0),
    )


def measure(
    source: MeasurementSource, *, now: datetime, settings: AlertSettings
) -> tuple[list[tuple[Evaluation, str]], list[dict[str, str]]]:
    """One tick's evaluations as `(evaluation, series)`, and the rules that could not be evaluated."""
    window = timedelta(seconds=settings.window_seconds)

    def span(end: datetime, length: timedelta) -> TimeRange:
        return TimeRange.custom(end - length, end, timezone=settings.timezone)

    evaluations: list[tuple[Evaluation, str]] = []

    failure = TerminalFailureRate(
        min_invocations=settings.min_invocations,
        max_rate=settings.failure_rate,
        window_seconds=settings.window_seconds,
    )
    try:
        classes = source.outcome_breakdown(time_range=span(now, window))
        total = Sample(
            sum(sample.count for sample in classes.values()),
            sum(sample.documents for sample in classes.values()),
        )
        failing = classes.get(FAILING_CLASS, Sample(0, 0))
        evaluations.append(
            (
                failure.evaluate(
                    RateWindow(total=total, failing=failing, window_seconds=settings.window_seconds)
                ),
                "",
            )
        )
    except Exception as error:
        evaluations.append((_failed(failure, error), ""))

    latency = SustainedLatency()
    try:
        windows = []
        for back in range(latency.consecutive - 1, -1, -1):
            end = now - window * back
            percentiles = source.latency(time_range=span(end, window), kind="attempt")
            windows.append(
                LatencyWindow(
                    p95_ms=percentiles.p95, sample=percentiles.sample, label=end.isoformat()
                )
            )
        evaluations.append((latency.evaluate(tuple(windows)), ""))
    except Exception as error:
        evaluations.append((_failed(latency, error), ""))

    quality = QualityDrop()
    try:
        recent_length = timedelta(hours=settings.quality_window_hours)
        recent = _scored(source.evaluation_scores(time_range=span(now, recent_length)))
        baseline = _scored(
            source.evaluation_scores(
                time_range=span(now - recent_length, timedelta(days=settings.quality_baseline_days))
            )
        )
        series = sorted(set(recent) | set(baseline))
        if not series:
            evaluations.append(
                (
                    quality.evaluate(QualityWindow(score=None, baseline=None, sample=Sample(0, 0))),
                    "",
                )
            )
        for key in series:
            candidate, base = recent.get(key), baseline.get(key)
            sample = Sample(
                min(candidate.sample.count if candidate else 0, base.sample.count if base else 0),
                min(
                    candidate.sample.documents if candidate else 0,
                    base.sample.documents if base else 0,
                ),
            )
            evaluations.append(
                (
                    quality.evaluate(
                        QualityWindow(
                            score=candidate.value if candidate else None,
                            baseline=base.value if base else None,
                            sample=sample,
                        )
                    ),
                    key,
                )
            )
    except Exception as error:
        evaluations.append((_failed(quality, error), ""))

    skipped = [{"rule": str(CircuitBreakerOpen.name), "reason": CIRCUIT_SKIP_REASON}]
    return evaluations, skipped


def _scored(buckets: Iterable[Bucket]) -> dict[str, Bucket]:
    """`metric|judge|rubric` → its *scored* bucket. N/A and eval_error never enter a comparison."""
    found: dict[str, Bucket] = {}
    for bucket in buckets:
        *series, status = bucket.key.split("|")
        if status == "scored" and bucket.value is not None:
            found["|".join(series)] = bucket
    return found


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class AlertStorage(Protocol):
    """Durable state for one tick, plus the three append-only records it writes."""

    def get(self, key: str) -> AlertState: ...

    def put(self, key: str, state: AlertState) -> None: ...

    def items(self) -> Iterable[tuple[str, AlertState]]: ...

    def record_evaluation(
        self, key: str, rule: str, series: str, evaluation: Evaluation, now: datetime
    ) -> None: ...

    def record_event(self, key: str, series: str, event: AlertEvent) -> int | None:
        """Insert the transition; `None` when it was already recorded (a retried tick)."""

    def add_message(
        self, event_id: int, *, key: str, event: AlertEvent, recipient: str
    ) -> None: ...

    def pending_messages(self) -> list[dict[str, Any]]: ...

    def mark_message(
        self, message_id: int, *, delivered: bool, error: str | None, now: datetime
    ) -> None: ...

    def record_run(self, run: Mapping[str, Any]) -> None: ...


def _split(key: str) -> tuple[str, str]:
    rule, _, series = key.partition(":")
    return rule, series


class PostgresAlertStorage:
    """`AlertStorage` over migration 017's tables, on one caller-owned connection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def try_lock(self) -> bool:
        row = fetch_one(
            self._conn, "SELECT pg_try_advisory_xact_lock(%s) AS held", (ALERT_LOCK_KEY,)
        )
        return bool(row and row["held"])

    def get(self, key: str) -> AlertState:
        row = fetch_one(self._conn, "SELECT * FROM alert_state WHERE key = %s", (key,))
        if row is None:
            return AlertState()
        return AlertState(
            firing=bool(row["firing"]),
            since=row["since"],
            notified_at=row["notified_at"],
            last_detail=str(row["last_detail"]),
        )

    def put(self, key: str, state: AlertState) -> None:
        rule, series = _split(key)
        fetch_one(
            self._conn,
            """
            INSERT INTO alert_state (key, rule, series, firing, since, notified_at, last_state,
                                     last_detail, evaluated_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'unmeasured', %s, now())
            ON CONFLICT (key) DO UPDATE SET
                firing = EXCLUDED.firing, since = EXCLUDED.since,
                notified_at = EXCLUDED.notified_at, last_detail = EXCLUDED.last_detail
            RETURNING key
            """,
            (key, rule, series, state.firing, state.since, state.notified_at, state.last_detail),
        )

    def items(self) -> Iterable[tuple[str, AlertState]]:
        rows = fetch_all(self._conn, "SELECT * FROM alert_state ORDER BY key")
        return [
            (
                str(row["key"]),
                AlertState(
                    firing=bool(row["firing"]),
                    since=row["since"],
                    notified_at=row["notified_at"],
                    last_detail=str(row["last_detail"]),
                ),
            )
            for row in rows
        ]

    def record_evaluation(
        self, key: str, rule: str, series: str, evaluation: Evaluation, now: datetime
    ) -> None:
        fetch_one(
            self._conn,
            """
            UPDATE alert_state SET last_state = %s, last_evaluation = %s::jsonb, evaluated_at = %s
            WHERE key = %s RETURNING key
            """,
            (str(evaluation.state), json.dumps(evaluation.as_dict()), now, key),
        )

    def record_event(self, key: str, series: str, event: AlertEvent) -> int | None:
        row = fetch_one(
            self._conn,
            """
            INSERT INTO alert_event (key, rule, series, severity, kind, at, detail, evaluation)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (key, kind, at) DO NOTHING
            RETURNING id
            """,
            (
                key,
                str(event.rule),
                series,
                str(event.severity),
                event.kind,
                event.at,
                event.detail,
                json.dumps(event.evaluation.as_dict()),
            ),
        )
        return int(row["id"]) if row else None

    def add_message(self, event_id: int, *, key: str, event: AlertEvent, recipient: str) -> None:
        email = compose(event)
        fetch_one(
            self._conn,
            """
            INSERT INTO alert_message (event_id, dedupe_key, recipient, subject, body)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING id
            """,
            (event_id, f"{key}:{event.kind}:{event.at}", recipient, email.subject, email.body),
        )

    def pending_messages(self) -> list[dict[str, Any]]:
        return fetch_all(
            self._conn,
            "SELECT * FROM alert_message WHERE state IN ('pending', 'failed') "
            "AND attempt_count < max_attempts ORDER BY id FOR UPDATE SKIP LOCKED",
        )

    def mark_message(
        self, message_id: int, *, delivered: bool, error: str | None, now: datetime
    ) -> None:
        fetch_one(
            self._conn,
            """
            UPDATE alert_message SET
                attempt_count = attempt_count + 1,
                state = CASE WHEN %s THEN 'delivered'
                             WHEN attempt_count + 1 >= max_attempts THEN 'dead'
                             ELSE 'failed' END,
                delivered_at = CASE WHEN %s THEN %s ELSE NULL END,
                last_error = %s
            WHERE id = %s RETURNING id
            """,
            (delivered, delivered, now, error, message_id),
        )

    def record_run(self, run: Mapping[str, Any]) -> None:
        fetch_one(
            self._conn,
            """
            INSERT INTO alert_run (started_at, finished_at, scheduler, evaluations, skipped, events,
                                   error)
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s) RETURNING id
            """,
            (
                run["started_at"],
                run["finished_at"],
                run["scheduler"],
                json.dumps(run["evaluations"]),
                json.dumps(run["skipped"]),
                run["events"],
                run.get("error"),
            ),
        )


@dataclass
class InMemoryAlertStorage:
    """The same surface in one process, for the unit tests and nothing else."""

    states: dict[str, AlertState] = field(default_factory=dict)
    evaluations: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)

    def try_lock(self) -> bool:
        return True

    def get(self, key: str) -> AlertState:
        state = self.states.get(key)
        return AlertState(**vars(state)) if state else AlertState()

    def put(self, key: str, state: AlertState) -> None:
        self.states[key] = AlertState(**vars(state))

    def items(self) -> Iterable[tuple[str, AlertState]]:
        return tuple(self.states.items())

    def record_evaluation(
        self, key: str, rule: str, series: str, evaluation: Evaluation, now: datetime
    ) -> None:
        self.evaluations[key] = {"state": str(evaluation.state), "at": now}

    def record_event(self, key: str, series: str, event: AlertEvent) -> int | None:
        if any(
            e["key"] == key and e["kind"] == event.kind and e["at"] == event.at for e in self.events
        ):
            return None
        self.events.append(
            {"id": len(self.events) + 1, "key": key, "kind": event.kind, "at": event.at}
        )
        return len(self.events)

    def add_message(self, event_id: int, *, key: str, event: AlertEvent, recipient: str) -> None:
        if any(m["event_id"] == event_id for m in self.messages):
            return
        self.messages.append(
            {
                "id": len(self.messages) + 1,
                "event_id": event_id,
                "subject": compose(event).subject,
                "recipient": recipient,
                "state": str(DeliveryState.PENDING),
                "attempt_count": 0,
            }
        )

    def pending_messages(self) -> list[dict[str, Any]]:
        return [
            m
            for m in self.messages
            if m["state"] in ("pending", "failed") and m["attempt_count"] < 5
        ]

    def mark_message(
        self, message_id: int, *, delivered: bool, error: str | None, now: datetime
    ) -> None:
        message = next(m for m in self.messages if m["id"] == message_id)
        message["attempt_count"] += 1
        message["state"] = (
            "delivered" if delivered else "dead" if message["attempt_count"] >= 5 else "failed"
        )

    def record_run(self, run: Mapping[str, Any]) -> None:
        self.runs.append(dict(run))


class _StorageTransitions:
    """The engine's `TransitionStore`, over whichever `AlertStorage` the tick holds."""

    def __init__(self, storage: AlertStorage) -> None:
        self._storage = storage

    def get(self, key: str) -> AlertState:
        return self._storage.get(key)

    def put(self, key: str, state: AlertState) -> None:
        self._storage.put(key, state)

    def items(self) -> Iterable[tuple[str, AlertState]]:
        return self._storage.items()


class LogSender:
    """Delivery for the mock outbox: a log line. It holds no address book and no network client."""

    def send(self, message: Mapping[str, Any]) -> bool:
        _log.warning(
            "ALERT (mock email to %s, not sent): %s",
            message.get("recipient"),
            message.get("subject"),
        )
        return True


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class AlertScheduler:
    """Runs one tick at a time over a storage the caller opens per tick."""

    def __init__(
        self,
        *,
        storage: Callable[[], AbstractContextManager[AlertStorage]],
        source: MeasurementSource,
        settings: AlertSettings | None = None,
        sender: LogSender | None = None,
        scheduler_id: str | None = None,
    ) -> None:
        self._storage = storage
        self._source = source
        self._settings = settings or AlertSettings()
        self._sender = sender or LogSender()
        self._id = scheduler_id or f"{socket.gethostname()}:{os.getpid()}"

    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        """Measure, decide, record, deliver. Returns what the run recorded."""
        started = now or datetime.now(UTC)
        with self._storage() as storage:
            if not getattr(storage, "try_lock", lambda: True)():
                return {"skipped_tick": "another scheduler holds the alert lock"}
            evaluations, skipped = measure(self._source, now=started, settings=self._settings)
            engine = Engine(
                _StorageTransitions(storage),
                cooldown=Cooldown(minutes=self._settings.cooldown_seconds / 60),
            )
            events = 0
            for decision in engine.decide_all(evaluations, now=started):
                key = decision.key
                storage.record_evaluation(
                    key,
                    str(decision.evaluation.rule),
                    decision.series,
                    decision.evaluation,
                    started,
                )
                if decision.event is None:
                    continue
                event_id = storage.record_event(key, decision.series, decision.event)
                if event_id is None:
                    continue
                events += 1
                storage.add_message(
                    event_id, key=key, event=decision.event, recipient=self._settings.recipient
                )
            for message in storage.pending_messages():
                try:
                    delivered, error = self._sender.send(message), None
                except (
                    Exception
                ) as failure:  # a failed delivery is recorded, never retried in a loop
                    delivered, error = False, f"{type(failure).__name__}: {failure}"
                storage.mark_message(
                    int(message["id"]), delivered=delivered, error=error, now=started
                )
            run = {
                "started_at": started,
                "finished_at": datetime.now(UTC) if now is None else started,
                "scheduler": self._id,
                "evaluations": [
                    {"series": series, **evaluation.as_dict()} for evaluation, series in evaluations
                ],
                "skipped": skipped,
                "events": events,
            }
            storage.record_run(run)
        return run

    def run_forever(self, *, stop: Callable[[], bool] = lambda: False) -> None:
        """Tick on the configured interval. A failing tick is logged and the loop goes on."""
        while not stop():
            try:
                run = self.tick()
                _log.info("alert tick: %s event(s)", run.get("events", 0))
            except Exception:
                _log.exception("the alert tick failed; the next one runs on schedule")
            time.sleep(self._settings.interval_seconds)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def rows_as_json(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rows with their timestamps as ISO strings, for a JSON answer."""
    return [{key: _iso(value) for key, value in row.items()} for row in rows]


def read_alerts(conn: Any, *, events: int = 50) -> dict[str, Any]:
    """What the admin Alerts page shows: state per rule, recent transitions, the outbox, last run."""
    last_run = fetch_one(conn, "SELECT * FROM alert_run ORDER BY started_at DESC LIMIT 1")
    return {
        "states": rows_as_json(
            fetch_all(
                conn,
                "SELECT key, rule, series, firing, since, notified_at, last_state, last_detail, "
                "last_evaluation, evaluated_at FROM alert_state ORDER BY rule, series",
            )
        ),
        "events": rows_as_json(
            fetch_all(
                conn,
                "SELECT id, rule, series, severity, kind, at, detail FROM alert_event "
                "ORDER BY at DESC LIMIT %s",
                (events,),
            )
        ),
        "messages": rows_as_json(
            fetch_all(
                conn,
                "SELECT id, event_id, recipient, subject, state, attempt_count, last_error, "
                "created_at, delivered_at FROM alert_message ORDER BY id DESC LIMIT %s",
                (events,),
            )
        ),
        "counts": {
            row["state"]: int(row["n"])
            for row in fetch_all(
                conn, "SELECT state, count(*) AS n FROM alert_message GROUP BY state"
            )
        },
        "last_run": {key: _iso(value) for key, value in last_run.items()} if last_run else None,
    }
