"""§16's four starting rules, each a pure decision about a measurement.

    Example rules: terminal failure >20% with at least 20 invocations in 5 minutes; p95 attempt
    latency >2.5 seconds across three windows; an open circuit breaker; quality degradation with
    enough samples. These are initial settings.

Three properties hold for all four, and each is a way an alerting system goes wrong:

**Nothing fires below its own minimum.** §16's first rule says *"en az 20 invocation varken"* — the
count is part of the condition, not a footnote. A rate rule over four invocations will fire on one
failure and stay silent on the same four-invocation day that went well, which is noise in one
direction and false confidence in the other.

**Unmeasurable is `UNMEASURED`, never `OK`.** A window with no invocations has no failure rate; a
distribution with no observations has no p95. Both are reported as a gap with its reason.

**The threshold is quoted with the observation.** Every evaluation carries what it was shown and what
it was asked, so an alert can be argued with. An alert that says only "latency high" makes the reader
go and look up the number it used.

The thresholds are dataclass fields rather than module constants, because §16 calls them settings —
a deployment tunes them, and a rule that had them baked in would need a code change to quiet down.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from referraleasy_alerts.models import (
    MAX_DETAIL,
    Evaluation,
    LatencyWindow,
    QualityWindow,
    RateWindow,
    RuleName,
    State,
)
from referraleasy_telemetry import Sample

__all__ = [
    "RULES",
    "CircuitBreakerOpen",
    "QualityDrop",
    "Rule",
    "SustainedLatency",
    "TerminalFailureRate",
]


def _unmeasured(rule: RuleName, detail: str, observed: str, threshold: str) -> Evaluation:
    """A gap, with its reason. The one constructor every "cannot decide" path goes through."""
    return Evaluation(
        rule=rule,
        state=State.UNMEASURED,
        detail=detail[:MAX_DETAIL],
        observed=observed,
        threshold=threshold,
        sample=Sample(count=0, documents=0),
    )


@runtime_checkable
class Rule(Protocol):
    """What a rule is: a name, a threshold it publishes, and a decision."""

    @property
    def name(self) -> RuleName: ...

    @property
    def threshold(self) -> str:
        """The condition, in words, so a page can show what would have to change."""
        ...


@dataclass(frozen=True, slots=True)
class TerminalFailureRate:
    """§16's first rule: terminal failure over 20% in a window with at least 20 invocations.

    "Terminal" is the caller's set — §15's outcome classes are the vocabulary and the four terminal
    ones are `error`, `blocked`, `fallback` and `partial`; `empty` is *not* terminal, because §15 says
    an empty result is a valid outcome. This rule takes a count rather than the classes themselves so
    the definition of terminal stays in one place instead of being re-derived here.
    """

    min_invocations: int = 20
    max_rate: float = 0.20
    window_seconds: float = 300

    name = RuleName.TERMINAL_FAILURE_RATE

    @property
    def threshold(self) -> str:
        return f"terminal failure > {self.max_rate:.0%} with at least {self.min_invocations} invocations in {self.window_seconds:.0f}s"

    def evaluate(self, window: RateWindow) -> Evaluation:
        rate = window.rate
        if rate is None:
            return _unmeasured(
                self.name,
                "no invocations in the window, so there is no failure rate to compare. A quiet "
                "window is not a healthy one",
                "0 invocations",
                self.threshold,
            )
        if window.total.count < self.min_invocations:
            return Evaluation(
                rule=self.name,
                state=State.UNMEASURED,
                detail=(
                    f"{window.total.count} invocations is below the {self.min_invocations} this rule "
                    f"requires, so the {rate:.1%} measured is not enough to fire on — and not enough "
                    "to clear either"
                ),
                observed=f"{window.failing.count}/{window.total.count} = {rate:.1%}",
                threshold=self.threshold,
                sample=window.total,
            )
        firing = rate > self.max_rate
        return Evaluation(
            rule=self.name,
            state=State.FIRING if firing else State.OK,
            detail=(
                f"{rate:.1%} of {window.total.count} invocations failed terminally"
                if firing
                else f"{rate:.1%} of {window.total.count} invocations failed terminally, at or below the threshold"
            ),
            observed=f"{window.failing.count}/{window.total.count} = {rate:.1%}",
            threshold=self.threshold,
            sample=window.total,
        )


@dataclass(frozen=True, slots=True)
class SustainedLatency:
    """§16's second rule: p95 attempt latency above the ceiling in three consecutive windows.

    **Three windows, not one.** A single slow window is a spike; a rule that fired on it would be the
    one that gets muted. The windows must be consecutive and in order — the caller passes the most
    recent last — because three above the ceiling with a quiet one between them is a different fact
    about the system, and "any three of the last five" is a different rule that this one does not
    implement.
    """

    max_p95_ms: float = 2500.0
    consecutive: int = 3

    name = RuleName.SUSTAINED_LATENCY

    @property
    def threshold(self) -> str:
        return f"p95 attempt latency > {self.max_p95_ms:.0f} ms in {self.consecutive} consecutive windows"

    def evaluate(self, windows: tuple[LatencyWindow, ...]) -> Evaluation:
        if len(windows) < self.consecutive:
            return _unmeasured(
                self.name,
                f"{len(windows)} window(s) seen, and this rule needs {self.consecutive} consecutive "
                "ones. Fewer is not a pass",
                f"{len(windows)} window(s)",
                self.threshold,
            )
        recent = windows[-self.consecutive :]
        missing = [
            window.label or f"#{index}"
            for index, window in enumerate(recent)
            if window.p95_ms is None
        ]
        if missing:
            return _unmeasured(
                self.name,
                "no p95 in window(s) "
                + ", ".join(missing)
                + ", and a window with no observations cannot be counted as below the ceiling",
                f"{len(recent) - len(missing)} of {self.consecutive} windows measured",
                self.threshold,
            )
        values = [window.p95_ms for window in recent if window.p95_ms is not None]
        firing = all(value > self.max_p95_ms for value in values)
        observed = ", ".join(f"{value:.0f} ms" for value in values)
        return Evaluation(
            rule=self.name,
            state=State.FIRING if firing else State.OK,
            detail=(
                f"p95 above {self.max_p95_ms:.0f} ms in all {self.consecutive} consecutive windows"
                if firing
                else "at least one of the consecutive windows was at or below the ceiling"
            ),
            observed=observed,
            threshold=self.threshold,
            sample=recent[-1].sample,
        )


@dataclass(frozen=True, slots=True)
class CircuitBreakerOpen:
    """§16's third rule: the circuit breaker is open.

    The simplest of the four, and the only one whose input is a fact rather than a measurement — so
    it does not take a `Sample` and does not have an `UNMEASURED` state. That is deliberate: a
    breaker is a boolean the runtime knows, and a rule that reported "not enough data" about a value
    it was handed would be inventing a reason to be unsure.
    """

    name = RuleName.CIRCUIT_OPEN

    @property
    def threshold(self) -> str:
        return "the circuit breaker is open"

    def evaluate(
        self, *, open_: bool, endpoint: str, opened_for_seconds: float | None = None
    ) -> Evaluation:
        if not open_:
            return Evaluation(
                rule=self.name,
                state=State.OK,
                detail=f"the breaker for {endpoint} is closed",
                observed=f"{endpoint}: closed",
                threshold=self.threshold,
                sample=Sample(count=1, documents=1),
            )
        held = (
            f", open for {opened_for_seconds:.0f}s"
            if opened_for_seconds is not None
            else ", open for an unknown time"
        )
        return Evaluation(
            rule=self.name,
            state=State.FIRING,
            detail=f"the breaker for {endpoint} is open{held}, so calls are being refused without being tried",
            observed=f"{endpoint}: open{held}",
            threshold=self.threshold,
            sample=Sample(count=1, documents=1),
        )


@dataclass(frozen=True, slots=True)
class QualityDrop:
    """§16's fourth rule: a quality drop, with enough samples to mean it.

    *"with enough samples"* is the whole of this rule. §14 already refuses to score a case it cannot
    measure, so a comparison between two scores is only as good as the smaller of their two samples —
    and this rule requires **both** to clear the minimum, because a confident baseline compared with
    a thin candidate is exactly the comparison that produces a fake regression.

    The drop is absolute rather than relative: a fall from 0.90 to 0.70 is twenty points whatever the
    baseline, and a relative rule would fire twice as readily on a small score as on a large one.
    """

    min_sample: int = 30
    max_drop: float = 0.10

    name = RuleName.QUALITY_DROP

    @property
    def threshold(self) -> str:
        return f"score more than {self.max_drop:.2f} below baseline, with at least {self.min_sample} scored cases on both sides"

    def evaluate(self, window: QualityWindow) -> Evaluation:
        if window.score is None or window.baseline is None:
            return _unmeasured(
                self.name,
                "a comparison needs both a candidate score and a baseline; one of them is missing, "
                "which is not a drop of zero",
                f"score={window.score}, baseline={window.baseline}",
                self.threshold,
            )
        if window.sample.count < self.min_sample:
            return Evaluation(
                rule=self.name,
                state=State.UNMEASURED,
                detail=(
                    f"{window.sample.count} scored case(s) is below the {self.min_sample} this rule "
                    "requires, so a difference of "
                    f"{window.baseline - window.score:+.3f} cannot be told from noise"
                ),
                observed=f"score {window.score:.3f} vs baseline {window.baseline:.3f} over {window.sample.count} cases",
                threshold=self.threshold,
                sample=window.sample,
            )
        drop = window.baseline - window.score
        firing = drop > self.max_drop
        return Evaluation(
            rule=self.name,
            state=State.FIRING if firing else State.OK,
            detail=(
                f"score fell {drop:.3f} from the baseline over {window.sample.count} cases"
                if firing
                else f"score moved {drop:+.3f} against the baseline, within the allowed drop"
            ),
            observed=f"score {window.score:.3f} vs baseline {window.baseline:.3f} over {window.sample.count} cases",
            threshold=self.threshold,
            sample=window.sample,
        )


#: §16's starting set, in the order the spec lists them. A tuple rather than a registry keyed by name:
#: the rules have different input shapes, so a lookup table would have to erase that to be uniform.
RULES = (TerminalFailureRate, SustainedLatency, CircuitBreakerOpen, QualityDrop)
