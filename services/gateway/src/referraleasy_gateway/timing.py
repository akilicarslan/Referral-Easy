"""Two clocks, two latencies, never one number standing in for both (§12).

§12 asks for progress latency to be measured **separately** from final-response latency. The reason
is that they answer different questions: progress latency is "how long until the patient saw
something happen", final latency is "how long until the patient had an outcome". A pipeline optimised
against a single blended number can look fast while showing the patient nothing for twenty seconds,
and a surface that reports one latency for both is reporting a number that is true of neither.

So every turn gets a `TurnClock`: `progress_ms()` is read when a non-terminal event is emitted,
`final_ms()` when the terminal one is, and both are recorded on the turn. The contract already
carries the split — `StreamEvent.progress_elapsed_ms` is documented as "null on a terminal event,
which carries the final timing instead" — and this module is the only place that reads either clock.

`Clock` is injectable because a test that has to sleep to observe a latency is a test that is slow,
flaky, or both. With a `ManualClock` the assertions are exact: emit a progress event at +120 ms, the
terminal at +250 ms, and both numbers are checkable rather than approximate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Final, Protocol

__all__ = [
    "Clock",
    "LatencyLedger",
    "ManualClock",
    "SystemClock",
    "TurnClock",
]


class Clock(Protocol):
    """Wall time for timestamps, monotonic time for durations.

    Split because they are not interchangeable: `created_at` must be a real UTC instant a reader can
    compare with a log line, and a duration must not move when the host's clock is corrected.
    """

    def now(self) -> datetime: ...

    def monotonic_ms(self) -> float: ...


class SystemClock:
    """The real thing. Monotonic for durations, UTC-aware for timestamps."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ms(self) -> float:
        return time.monotonic() * 1000.0


@dataclass
class ManualClock:
    """A clock the test moves by hand. Only ever constructed by tests and by `conftest`.

    `read_cost_ms` is the second seam: each read of `monotonic_ms` advances the clock by that much,
    which stands in for "the work between two readings took this long". Without it a test could only
    observe latencies by calling `advance_ms` between two statements it controls — impossible when
    the two readings happen inside one request. With it, a request that reads the clock four times
    reports a progress latency smaller than its final latency, which is the property §12 asks for,
    and the unit tests for `TurnClock` still pin exact numbers by calling `advance_ms` directly.
    Defaults to 0, so nothing moves unless a test asks it to.
    """

    _wall: datetime = field(default_factory=lambda: datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC))
    _monotonic_ms: float = 0.0
    read_cost_ms: float = 0.0

    def now(self) -> datetime:
        return self._wall

    def monotonic_ms(self) -> float:
        self._monotonic_ms += self.read_cost_ms
        return self._monotonic_ms

    def advance_ms(self, milliseconds: float) -> None:
        """Move both clocks forward together, so timestamps and durations stay coherent."""
        self._monotonic_ms += float(milliseconds)
        self._wall = datetime.fromtimestamp(self._wall.timestamp() + milliseconds / 1000.0, tz=UTC)


@dataclass(frozen=True, slots=True)
class LatencySample:
    """One turn's two latencies, kept as two fields rather than one and a flag.

    `progress_ms` is None for a turn that produced no progress event. That is not the same as zero:
    "never showed the patient anything" and "showed them something instantly" are different facts,
    and an average that conflates them is wrong in the direction that flatters us.
    """

    turn_id: str
    workflow_id: str
    progress_ms: int | None
    final_ms: int
    synthetic: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "workflow_id": self.workflow_id,
            "progress_elapsed_ms": self.progress_ms,
            "final_elapsed_ms": self.final_ms,
            "synthetic": self.synthetic,
        }


class LatencyLedger:
    """Both latencies per turn, and the two roll-ups kept apart on purpose.

    `progress_mean_ms` and `final_mean_ms` exist as separate properties so that no admin view can
    accidentally print one as "the latency". §12: "no fabricated demo charts" applies here too —
    these are measurements, and they are labelled `synthetic` like every other artefact.
    """

    def __init__(self, *, limit: int = 1000) -> None:
        self._samples: list[LatencySample] = []
        self._limit = limit
        self._lock = Lock()

    def record(self, sample: LatencySample) -> None:
        with self._lock:
            self._samples.append(sample)
            if len(self._samples) > self._limit:
                del self._samples[: len(self._samples) - self._limit]

    @property
    def samples(self) -> tuple[LatencySample, ...]:
        with self._lock:
            return tuple(self._samples)

    def _mean(self, values: list[int]) -> float | None:
        return None if not values else sum(values) / len(values)

    @property
    def progress_mean_ms(self) -> float | None:
        """Mean progress latency over turns that had one. None when there were none."""
        return self._mean([s.progress_ms for s in self._samples if s.progress_ms is not None])

    @property
    def final_mean_ms(self) -> float | None:
        return self._mean([s.final_ms for s in self.samples])

    def report(self) -> dict[str, object]:
        samples = self.samples
        return {
            "turns": len(samples),
            "progress_elapsed_ms_mean": self.progress_mean_ms,
            "final_elapsed_ms_mean": self.final_mean_ms,
            "progress_latency_measured_separately": True,
            "samples": [sample.as_dict() for sample in samples],
        }


class TurnClock:
    """One turn's timing: one start mark, two readings, both retained.

    A turn is the unit §12 measures — a command from the patient (or an agent step) that produces
    progress events and then an outcome. `progress_ms()` may be called repeatedly (one call per
    progress event) and each call returns the elapsed time at that moment; `final_ms()` is called
    once, when the turn ends.
    """

    def __init__(self, *, turn_id: str, workflow_id: str, clock: Clock, synthetic: bool) -> None:
        self.turn_id = turn_id
        self.workflow_id = workflow_id
        self._clock = clock
        self._started_ms: Final[float] = clock.monotonic_ms()
        self._synthetic = synthetic
        self._progress_ms: int | None = None
        self._final_ms: int | None = None

    def progress_ms(self) -> int:
        """Elapsed milliseconds so far, for a progress event.

        The **first** call also fixes the sample's progress reading, and later calls do not overwrite
        it. §12 measures "how long until the patient saw something happen", which is a fact about the
        first thing they saw — a turn that emitted five progress events and took twelve seconds to
        emit the first has a twelve-second progress latency, not a two-second one.
        """
        elapsed = int(self._clock.monotonic_ms() - self._started_ms)
        if self._progress_ms is None:
            self._progress_ms = elapsed
        return elapsed

    def final_ms(self) -> int:
        """Elapsed milliseconds at the end of the turn. Called once; a second call is a bug."""
        if self._final_ms is not None:  # pragma: no cover - guarded for clarity
            raise RuntimeError(f"turn {self.turn_id} already has a final latency")
        self._final_ms = int(self._clock.monotonic_ms() - self._started_ms)
        return self._final_ms

    @property
    def has_final(self) -> bool:
        return self._final_ms is not None

    def sample(self) -> LatencySample:
        """The turn's two latencies, for the ledger. Refuses to report an unfinished turn."""
        if self._final_ms is None:
            raise RuntimeError(f"turn {self.turn_id} has no final latency yet")
        return LatencySample(
            turn_id=self.turn_id,
            workflow_id=self.workflow_id,
            progress_ms=self._progress_ms,
            final_ms=self._final_ms,
            synthetic=self._synthetic,
        )
