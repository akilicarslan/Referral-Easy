# §16 is quoted in Turkish, verbatim from the spec
"""§16's alerting: what a rule decided, and the three states it can be in.

The whole module turns on one distinction. An alerting system has three answers, not two:

* **firing** — the condition holds, with enough data to say so;
* **ok** — the condition does not hold, with enough data to say so;
* **unmeasured** — there was not enough data to decide.

Collapsing the third into the second is how an alerting system goes quiet at exactly the moment it
matters: a service that stopped emitting has no failures *and no evidence*, and §16 asks for its
thresholds to apply *"yeterli örnekle"* — with enough samples. So `unmeasured` is a first-class state
here, it carries the reason, and nothing in this package can produce `ok` from a sample below the
rule's own minimum.

Every input below is a §15 measurement, so every one of them arrives with a `Sample`. A rule that
took a bare `float` could be called with a number nobody measured, and there would be no way to tell
from the outside whether it had been.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from referraleasy_telemetry import Sample

__all__ = [
    "AlertEvent",
    "Evaluation",
    "LatencyWindow",
    "QualityWindow",
    "RateWindow",
    "RuleName",
    "Severity",
    "State",
]


class RuleName(StrEnum):
    """§16's starting set. The spec calls them "örnek kurallar" — examples, and the settings."""

    TERMINAL_FAILURE_RATE = "terminal_failure_rate"
    SUSTAINED_LATENCY = "sustained_attempt_latency"
    CIRCUIT_OPEN = "circuit_open"
    QUALITY_DROP = "quality_drop"


class Severity(StrEnum):
    """What an event is worth. A severity is a claim about consequences, so there are three."""

    PAGE = "page"
    WARN = "warn"
    INFO = "info"


class State(StrEnum):
    """The three answers. `UNMEASURED` is not a soft `OK`."""

    FIRING = "firing"
    OK = "ok"
    UNMEASURED = "unmeasured"


@dataclass(frozen=True, slots=True)
class RateWindow:
    """A rate's two counts over one window, each with its own sample.

    Two samples rather than one because the numerator and the denominator are counted by different
    queries and §15's dedup applies to each: an invocation that was written twice must not inflate
    the denominator, and a failure written twice must not inflate the numerator. Taking one sample
    for both would hide whichever of the two was duplicated.
    """

    total: Sample
    failing: Sample
    window_seconds: float

    @property
    def rate(self) -> float | None:
        """The failure rate, or `None` when there is no denominator.

        `None` and not `0.0`: a window in which nothing was invoked has no failure rate, and calling
        it zero is the same error as calling an empty index a healthy one.
        """
        if self.total.count <= 0:
            return None
        return self.failing.count / self.total.count


@dataclass(frozen=True, slots=True)
class LatencyWindow:
    """One window's p95 for one latency distribution.

    `p95_ms` is nullable because §15 returns `None` for a percentile with no observations — a zero
    would plot an impossibly fast system, and a rule reading that zero would see a healthy one.
    """

    p95_ms: float | None
    sample: Sample
    label: str = ""


@dataclass(frozen=True, slots=True)
class QualityWindow:
    """A quality score against the baseline it is being compared with.

    Both nullable and both required: a drop needs two numbers, and a rule given one of them is
    unmeasured rather than fine. §14 forbids merging runs scored under different rubrics, so the
    caller passes the two it means to compare.
    """

    score: float | None
    baseline: float | None
    sample: Sample


@dataclass(frozen=True, slots=True)
class Evaluation:
    """One rule's answer, with what it was shown and what it was asked.

    `observed` and `threshold` are strings rather than numbers because a rule's condition is not
    always a comparison of scalars — "three consecutive windows" has no single threshold — and a rule
    forced to express itself as one number would either lie or be unable to say why it fired.
    """

    rule: RuleName
    state: State
    detail: str
    observed: str
    threshold: str
    sample: Sample

    @property
    def firing(self) -> bool:
        """True only for `FIRING`. Not `not ok` — unmeasured is not firing either."""
        return self.state is State.FIRING

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": str(self.rule),
            "state": str(self.state),
            "detail": self.detail,
            "observed": self.observed,
            "threshold": self.threshold,
            "sample": self.sample.as_dict(),
        }


#: How long an unmeasured evaluation's reason may be before it is cut. Long enough for a threshold
#: and what was seen, short enough for a table cell.
MAX_DETAIL: Final = 400


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """A state change worth telling someone about: an alert that started, or one that resolved.

    **Both directions are events.** §16 asks for the resolved case to be visible, and an alerting
    system that only emits openings leaves the reader to infer the close — which is the same problem
    as a chart that only draws failures.
    """

    rule: RuleName
    severity: Severity
    opened: bool
    at: str
    detail: str
    evaluation: Evaluation

    @property
    def kind(self) -> str:
        return "opened" if self.opened else "resolved"

    def dedupe_key(self, series: str = "") -> str:
        """The key a cooldown collapses on: a rule, and optionally the series it is about.

        A rule that fires for one agent and then another is two things worth saying; a rule that
        fires for the same agent every minute is one. `series` is how a caller says which it has, and
        an empty series means the rule is about the system rather than a subject.
        """
        return f"{self.rule}:{series}" if series else str(self.rule)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": str(self.rule),
            "severity": str(self.severity),
            "kind": self.kind,
            "at": self.at,
            "detail": self.detail,
            "evaluation": self.evaluation.as_dict(),
        }
