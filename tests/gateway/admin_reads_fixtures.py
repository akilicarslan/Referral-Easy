"""A stand-in `TelemetryQuery`, so the admin reads can be tested without a cluster.

Named `admin_reads_fixtures` rather than `fixtures` because a `conftest` module is reachable under a
name six directories share and two test modules named alike stop collection (see
`tests/test_test_layout.py`).

**Why a fake query rather than a fake `TelemetryReads`.** The thing under test is the composition —
which aggregation a page calls, with which window and which filters, and whether the sample counts
survive the trip — and a fake at the `TelemetryReads` boundary would replace exactly the code the
tests are about. The fake sits one layer lower, where the library's own contract is already
covered by `tests/telemetry/test_query.py`, so what remains is this repository's use of it.

The canned values are deliberately *unround*: a percentile of 250.0 could be a default that leaked in,
where 137.5 could not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from referraleasy_gateway.admin import TelemetryReads
from referraleasy_telemetry import (
    FeedbackSummary,
    Percentiles,
    Sample,
    TelemetryQueryError,
    TimeRange,
    TimeSeries,
)

# `Bucket` is in `query.__all__` and not re-exported at the package top level, which curates its
# surface. Reached through the module rather than added to that surface for a test's convenience.
from referraleasy_telemetry.query import Bucket

#: Two events in the window, and one of the two written twice. `documents` above `count` is the
#: dedup signal §15 requires and the reason the fake bothers to be inexact.
OCCURRED = Sample(count=2, documents=3)

#: What every fake aggregation returns by default.
SAMPLE = OCCURRED


@dataclass
class FakeQuery:
    """Records what it was asked and answers with fixed objects.

    `raises` makes every call throw, which is how the "a panel fails, the page still draws" path is
    exercised — one panel must degrade to its own "no source" instead of taking the response down.
    """

    calls: list[tuple[str, TimeRange, Any]] = field(default_factory=list)
    raises: Exception | None = None
    feedback_total: int | None = None

    def _record(self, name: str, time_range: TimeRange, filters: Any) -> None:
        self.calls.append((name, time_range, filters))
        if self.raises is not None:
            raise self.raises

    def _names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    # -- the surface `TelemetryReads` uses -------------------------------------------------------

    def event_count(self, *, time_range, filters=None, metric=None, **_kw) -> Sample:
        self._record(f"event_count:{metric}" if metric else "event_count", time_range, filters)
        return SAMPLE

    def agent_triggers(self, *, time_range, filters=None, **_kw) -> Sample:
        self._record("agent_triggers", time_range, filters)
        return SAMPLE

    def outcome_breakdown(self, *, time_range, filters=None, **_kw) -> dict[str, Sample]:
        self._record("outcome_breakdown", time_range, filters)
        return {"success_full": SAMPLE, "empty": Sample(count=1, documents=1)}

    def latency(self, *, time_range, kind="attempt", filters=None, **_kw) -> Percentiles:
        self._record(f"latency:{kind}", time_range, filters)
        return Percentiles(
            metric=f"latency.{kind}.ms", p50=137.5, p95=411.25, p99=980.75, sample=SAMPLE
        )

    def api_failures(self, *, time_range, filters=None, size=25, **_kw) -> list[Bucket]:
        self._record("api_failures", time_range, filters)
        return [Bucket(key="/slots 503", sample=SAMPLE, value=3.0)]

    def time_series(self, *, time_range, filters=None, metric=None, **_kw) -> TimeSeries:
        self._record("time_series", time_range, filters)
        return TimeSeries(
            interval=time_range.calendar_interval(),
            timezone=time_range.timezone,
            points=((datetime(2026, 10, 15, 6, 0, tzinfo=UTC), SAMPLE),),
            sample=SAMPLE,
        )

    def index_health(self) -> dict[str, Any]:
        self.calls.append(("index_health", TimeRange.day(), None))
        return {"indices": [{"index": "otel-events-2026.10.15", "docs": 3}], "retention_days": 7}

    def guardrail_decisions(self, *, time_range, filters=None, **_kw) -> list[Bucket]:
        self._record("guardrail_decisions", time_range, filters)
        return [Bucket(key="layer=regex decision=block", sample=Sample(1, 1))]

    def feedback(self, *, time_range, total_responses, filters=None, size=1000, **_kw):
        self._record("feedback", time_range, filters)
        self.feedback_total = total_responses
        return FeedbackSummary(
            responses_with_feedback=2,
            likes=1,
            dislikes=1,
            total_responses=total_responses,
            like_rate=0.5,
            participation_rate=0.25 if total_responses else None,
        )

    def evaluation_scores(self, *, time_range, filters=None, index=None, **_kw) -> list[Bucket]:
        self._record("evaluation_scores", time_range, filters)
        return [Bucket(key="eval-run-1|outcome_correctness", sample=SAMPLE, value=0.875)]


def reads_for(query: FakeQuery) -> TelemetryReads:
    """A real `TelemetryReads` over the fake transport.

    The real class on purpose: it holds the sample-count and "no source" rules, and a fake of it
    would let a test pass while those rules were gone.
    """
    return TelemetryReads(query)  # type: ignore[arg-type]


def exploded(message: str = "connection refused") -> FakeQuery:
    """A query whose every call fails, for the degraded path."""
    return FakeQuery(raises=TelemetryQueryError(message))
