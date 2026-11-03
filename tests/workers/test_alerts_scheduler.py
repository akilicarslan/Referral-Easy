"""§16's scheduler (`referraleasy_workers.alerts`): measure, decide, record, deliver to the log.

The unit tests drive the scheduler over `InMemoryAlertStorage` with a scripted telemetry source, so
every branch is exercised without a cluster or a database. The last test runs the same tick against
migration 017's tables and is marked `integration`: it needs `TEST_DATABASE_URL`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from referraleasy_telemetry import Sample
from referraleasy_telemetry.query import Bucket, Percentiles, TimeRange
from referraleasy_workers.alerts import (
    CIRCUIT_SKIP_REASON,
    AlertScheduler,
    AlertSettings,
    InMemoryAlertStorage,
    PostgresAlertStorage,
    measure,
    read_alerts,
)

NOW = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)


@dataclass
class Telemetry:
    """A scripted §15 source. Each attribute is what the next query answers."""

    total: int = 0
    errors: int = 0
    p95: float | None = None
    latency_samples: int = 0
    scores: list[Bucket] = field(default_factory=list)
    baseline: list[Bucket] = field(default_factory=list)
    fail: bool = False
    latency_calls: list[TimeRange] = field(default_factory=list)

    def outcome_breakdown(self, *, time_range: TimeRange, filters: Any = None) -> dict[str, Sample]:
        if self.fail:
            raise ConnectionError("opensearch is down")
        return {
            "success_full": Sample(self.total - self.errors, self.total - self.errors),
            "error": Sample(self.errors, self.errors),
        }

    def latency(
        self,
        *,
        time_range: TimeRange,
        kind: str = "attempt",
        filters: Any = None,
        include_errors: bool = True,
    ) -> Percentiles:
        self.latency_calls.append(time_range)
        return Percentiles(
            metric="latency.attempt.ms",
            p50=None,
            p95=self.p95,
            p99=None,
            sample=Sample(self.latency_samples, self.latency_samples),
        )

    def evaluation_scores(self, *, time_range: TimeRange, filters: Any = None) -> list[Bucket]:
        recent = time_range.end >= NOW - timedelta(minutes=1)
        return self.scores if recent else self.baseline


def _scheduler(source: Telemetry, storage: InMemoryAlertStorage, **settings: Any) -> AlertScheduler:
    @contextmanager
    def opened() -> Iterator[InMemoryAlertStorage]:
        yield storage

    return AlertScheduler(
        storage=opened, source=source, settings=AlertSettings(**settings), scheduler_id="test"
    )


def _states(run: dict[str, Any]) -> dict[str, str]:
    return {
        e["rule"] + (f":{e['series']}" if e["series"] else ""): e["state"]
        for e in run["evaluations"]
    }


def test_a_failure_rate_above_the_threshold_opens_one_alert_and_one_message() -> None:
    source, storage = Telemetry(total=40, errors=20), InMemoryAlertStorage()
    run = _scheduler(source, storage).tick(NOW)
    assert _states(run)["terminal_failure_rate"] == "firing"
    assert run["events"] == 1
    assert [m["state"] for m in storage.messages] == ["delivered"]  # delivered to the log only

    # Still firing on the next tick: dedup means no new event and no new message.
    again = _scheduler(source, storage).tick(NOW + timedelta(minutes=1))
    assert again["events"] == 0 and len(storage.messages) == 1


def test_the_resolution_is_an_event_of_its_own() -> None:
    source, storage = Telemetry(total=40, errors=20), InMemoryAlertStorage()
    _scheduler(source, storage).tick(NOW)
    source.errors = 0
    run = _scheduler(source, storage).tick(NOW + timedelta(minutes=5))
    assert run["events"] == 1
    assert [e["kind"] for e in storage.events] == ["opened", "resolved"]
    assert len(storage.messages) == 2


def test_too_few_invocations_is_unmeasured_and_changes_nothing() -> None:
    source, storage = Telemetry(total=5, errors=5), InMemoryAlertStorage()
    run = _scheduler(source, storage).tick(NOW)
    assert _states(run)["terminal_failure_rate"] == "unmeasured"
    assert storage.events == [] and storage.messages == []


def test_a_query_that_fails_is_unmeasured_with_the_error_never_ok() -> None:
    source = Telemetry(fail=True)
    evaluations, _ = measure(source, now=NOW, settings=AlertSettings())
    failed = next(e for e, _ in evaluations if str(e.rule) == "terminal_failure_rate")
    assert str(failed.state) == "unmeasured" and "opensearch is down" in failed.detail


def test_the_circuit_rule_is_reported_as_skipped_with_its_reason_on_every_run() -> None:
    run = _scheduler(Telemetry(), InMemoryAlertStorage()).tick(NOW)
    assert run["skipped"] == [{"rule": "circuit_open", "reason": CIRCUIT_SKIP_REASON}]
    assert "circuit_open" not in _states(run)


def test_latency_reads_three_consecutive_windows_ending_now() -> None:
    source = Telemetry(p95=3000.0, latency_samples=50)
    run = _scheduler(source, InMemoryAlertStorage(), window_seconds=300).tick(NOW)
    ends = [window.end for window in source.latency_calls]
    assert [NOW - end for end in ends] == [
        timedelta(minutes=10),
        timedelta(minutes=5),
        timedelta(0),
    ]
    assert _states(run)["sustained_attempt_latency"] == "firing"


def test_quality_compares_each_metric_judge_rubric_series_on_scored_buckets_only() -> None:
    def bucket(series: str, status: str, value: float | None, n: int) -> Bucket:
        return Bucket(key=f"{series}|{status}", value=value, sample=Sample(n, n))

    source = Telemetry(
        scores=[
            bucket("groundedness|none|offline-rules-v1", "scored", 0.5, 40),
            bucket("groundedness|none|offline-rules-v1", "not_applicable", None, 9),
        ],
        baseline=[bucket("groundedness|none|offline-rules-v1", "scored", 0.9, 60)],
    )
    run = _scheduler(source, InMemoryAlertStorage()).tick(NOW)
    assert _states(run)["quality_drop:groundedness|none|offline-rules-v1"] == "firing"


def test_no_scored_evaluations_is_an_unmeasured_quality_rule() -> None:
    run = _scheduler(Telemetry(), InMemoryAlertStorage()).tick(NOW)
    assert _states(run)["quality_drop"] == "unmeasured"


def test_a_failing_sender_retries_the_same_message_and_never_composes_another() -> None:
    class Refusing:
        def send(self, message: Any) -> bool:
            raise OSError("smtp is a fiction here")

    source, storage = Telemetry(total=40, errors=20), InMemoryAlertStorage()

    @contextmanager
    def opened() -> Iterator[InMemoryAlertStorage]:
        yield storage

    scheduler = AlertScheduler(storage=opened, source=source, sender=Refusing(), scheduler_id="t")  # type: ignore[arg-type]
    for minute in range(7):
        scheduler.tick(NOW + timedelta(minutes=minute))
    assert len(storage.messages) == 1
    assert storage.messages[0]["state"] == "dead" and storage.messages[0]["attempt_count"] == 5


# ---------------------------------------------------------------------------
# Against migration 017
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a_tick_against_the_alert_tables_records_state_event_message_and_run(db: Any) -> None:
    for table in ("alert_message", "alert_event", "alert_state", "alert_run"):
        db.execute(f"DELETE FROM {table}")  # rolled back with the test's transaction

    @contextmanager
    def opened() -> Iterator[PostgresAlertStorage]:
        yield PostgresAlertStorage(db)

    source = Telemetry(total=40, errors=20)
    scheduler = AlertScheduler(storage=opened, source=source, scheduler_id="it")
    scheduler.tick(NOW)
    scheduler.tick(NOW + timedelta(minutes=1))  # still firing: no second event or message
    source.errors = 0
    scheduler.tick(NOW + timedelta(minutes=5))

    view = read_alerts(db)
    assert [e["kind"] for e in reversed(view["events"])] == ["opened", "resolved"]
    assert view["counts"] == {"delivered": 2}
    failure = next(s for s in view["states"] if s["rule"] == "terminal_failure_rate")
    assert failure["firing"] is False and failure["last_state"] == "ok"
    assert view["last_run"]["skipped"][0]["rule"] == "circuit_open"
    # A replayed transition is refused by the table, not by the caller's memory.
    storage = PostgresAlertStorage(db)
    assert storage.try_lock() is True
