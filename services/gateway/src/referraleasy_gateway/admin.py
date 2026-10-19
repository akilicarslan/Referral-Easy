# ruff: noqa: RUF002  # the §12 rule is quoted in Turkish, verbatim from the spec
"""The admin read API — §12's nine pages, read from the telemetry indices (ADR-0039).

§12 asks for nine pages and §15 draws the data path as OpenSearch → *Admin API*. That node did not
exist; this module is it, mounted under the gateway's existing `/v1/admin/*` because the admin
session, the error envelope and the same-origin proxy are all already there (ADR-0039 records why,
and what the alternative cost).

Three rules shape everything below, and each of them is a way this could have been written wrong:

**A page with no source says so; it does not render a zero.** §12 forbids a fabricated chart
("sahte sabit başarı grafikleri kullanılmaz") and §14 has the same rule for scores. A route whose
source is absent, unreachable or not yet built answers :data:`unavailable` — an explicit shape with a
reason — rather than an empty series that a chart would draw as a flat, successful line. That is the
difference between "nothing happened" and "we did not look", and only one of them is true.

**Every number carries how many things it was computed from.** §12 requires a sample count on every
chart, and :class:`~referraleasy_telemetry.Sample` is a field of every result rather than an optional
companion precisely so one cannot be forgotten. These handlers pass it through untouched: a view
cannot invent a sample it was not given, which is why the requirement is enforced here and not in the
page.

**The window and the filters are the library's, not this module's.** `TimeRange` resolves "day" in
the requested timezone and `Filters` knows which §15 attribute each dimension lives on. Re-deriving
either here would produce a second answer to "where does this field live", and the two would
eventually disagree — quietly, in a chart.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from referraleasy_alerts import RULES as ALERT_RULES
from referraleasy_alerts import SEVERITIES, DeliveryState, Severity
from referraleasy_telemetry import (
    ATTR_AGENT_ROLE,
    TRACE_INDEX_PATTERN,
    Filters,
    LatencyKind,
    MetricName,
    TelemetryQuery,
    TelemetryQueryError,
    TimeRange,
)

# Reached through `conventions` because the package's curated surface exports `ATTR_AGENT_ROLE` and
# not its three siblings — a line drawn by whoever needed each name rather than a rule. All four are
# one vocabulary (§15's), so the alternative is three names added to a public surface to satisfy one
# caller's list.
from referraleasy_telemetry.conventions import (
    ATTR_AGENT_RELEASE,
    ATTR_AGENT_VERSION,
    ATTR_MODEL_NAME,
)

from .config import GatewaySettings
from .errors import ContractViolationError

__all__ = [
    "RANGES",
    "SCENARIO_MANIFEST",
    "TelemetryReads",
    "Window",
    "build_reads",
    "load_alert_rules",
    "load_scenarios",
    "unavailable",
]

logger = logging.getLogger(__name__)

#: §12's ranges. ``custom`` carries its own `from`/`to`.
RANGES: Final[tuple[str, ...]] = ("day", "month", "custom")

#: The zone §12's "day" and "month" are cut in when a request names none. Stated here rather than
#: read from the library because `TimeRange` spells it as a default argument, and this constant has
#: to exist before a `TimeRange` does. `test_admin.py` asserts the two agree, so the copy cannot
#: drift into a gateway that reports one zone and windows by another.
DEFAULT_TIMEZONE: Final = "America/Chicago"

#: A dashboard query slower than this is a dashboard nobody waits for. Shorter than the library's own
#: default and deliberately not an environment setting: there is no deployment whose correct value
#: differs from this one, and a knob nobody turns is a knob nobody has tested.
TIMEOUT_SECONDS: Final = 5.0

#: §15's three latency distributions, in display order. Derived from the enum rather than listed, so
#: a fourth kind would appear here instead of being silently unreported. §9 forbids averaging
#: progress and final latency into one number, which is why they are three entries and not one.
LATENCY_KINDS: Final[tuple[str, ...]] = tuple(kind.value for kind in LatencyKind)

#: How many distinct values a filter list asks for.
DIMENSION_SIZE: Final = 25

#: The §15 dimensions §12's Agents & Versions page lists. Named here rather than on the route so the
#: page and any future filter dropdown read the same list — and so a dimension that is not a
#: dimension (an id) has no way into either.
AGENT_DIMENSIONS: Final[tuple[str, ...]] = (
    ATTR_AGENT_ROLE,
    ATTR_AGENT_VERSION,
    ATTR_AGENT_RELEASE,
    ATTR_MODEL_NAME,
)


def load_alert_rules() -> dict[str, Any]:
    """§16's four alert rules and the thresholds they publish, for the admin page.

    **Static, and that is the honest content of the page today.** No evaluation has run: the
    scheduler that queries OpenSearch on an interval is the rest of Phase 7.3 (`compose.yaml`
    declares it and its `scheduler` arm still refuses to start). So the page shows what each rule
    watches and what it would take to fire, and says plainly that nothing has been evaluated — which
    is a different statement from "nothing is wrong", and the one an alerting page must not blur.

    Read from the package rather than restated here, because §16's thresholds are the package's and a
    second copy would eventually be a second answer to what a rule fires on.
    """
    rules = [
        {
            "rule": str(rule().name),
            "threshold": rule().threshold,
            "severity": str(SEVERITIES.get(rule().name, Severity.INFO)),
            "unmeasured_is_not_ok": True,
        }
        for rule in ALERT_RULES
    ]
    return {
        "available": True,
        "rules": rules,
        "evaluation": {
            "ran": False,
            "reason": (
                "No rule has been evaluated. The scheduler that queries the telemetry indices on an "
                "interval is the remaining work in Phase 7.3, so nothing here is a finding — and "
                "none of these rules reports 'ok' until it can measure something."
            ),
        },
        "outbox": {
            "messages": [],
            "counts": {str(state): 0 for state in DeliveryState},
            "sends": False,
            "note": (
                "The mock outbox composes and records; it never sends. Nothing has been composed yet "
                "because nothing has fired."
            ),
        },
    }


def unavailable(reason: str) -> dict[str, Any]:
    """The answer for a page whose source is absent, unreachable or not built yet.

    A function rather than a literal so every "no source" state has the same shape, and so the
    `available` flag is not something a handler can forget to set on the success path — the two
    shapes are produced in one place each and a test asserts they are distinguishable.
    """
    return {"available": False, "reason": reason}


#: The repository root, resolved the same way `referraleasy_guardrails.corpus` and
#: `referraleasy_eval.dataset` resolve it: four levels up from a module under `<package>/src/<pkg>/`.
#: That lands on the checkout when running from source and on `/app/.venv` in the image, which is
#: where the gateway Dockerfile puts `fixtures/` — so the same expression is correct in both, and
#: this is the third copy of it rather than a coincidence worth inventing a package for.
REPO_ROOT: Final = Path(__file__).resolve().parents[4]

#: §20's eight portfolio scenarios, as a machine-readable index. Fixture metadata, not telemetry: it
#: is the same file the evaluation dataset validates its `scenario_id`s against, so the admin page and
#: the offline evaluation cannot disagree about what a scenario is.
SCENARIO_MANIFEST: Final = REPO_ROOT / "fixtures" / "scenarios.json"


def load_scenarios(path: Path = SCENARIO_MANIFEST) -> dict[str, Any]:
    """§12's Fault Scenarios page: the eight scenarios and what each one is expected to prove.

    **Read from the fixtures, not from a run.** §12 requires this page to link a scenario to the real
    trace and metrics a run produced, and no run has produced any yet — the Scenario Runner is Phase
    7.4. So the page shows the *definition*: the fault step injected, the outcomes that count as
    correct, and the assertions that make the scenario pass or fail. Those are the fixtures' own
    words, quoted rather than summarised, because a summary of an assertion is not an assertion.

    A missing or malformed file is :func:`unavailable` with the path, not an empty list: a scenarios
    page with nothing on it and a scenarios page whose file is missing are different problems, and
    only one of them is about the scenarios.
    """
    if not path.is_file():
        return unavailable(
            f"no scenario manifest at {path}. The eight §20 scenarios are indexed there; without it "
            "there is nothing to describe, and an empty list would look like a project with no "
            "scenarios rather than a missing file."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as problem:
        return unavailable(f"{path} could not be read: {problem}")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        return unavailable(f"{path} carries no scenarios list")
    return {
        "available": True,
        "source": str(path),
        "scenarios": scenarios,
        # Said here rather than inferred by the page: the link §12 asks for is the one thing this
        # read cannot supply, and a page that has to decide whether a missing link means "not run
        # yet" or "we forgot" will eventually decide the wrong one.
        "runs": {
            "linked": False,
            "reason": (
                "No run has been linked to these scenarios. §12 asks for a real trace and metric "
                "link per scenario; the Scenario Runner that produces one is Phase 7.4, so this page "
                "shows the definitions and not results."
            ),
        },
    }


@dataclass(frozen=True, slots=True)
class Window:
    """§12's time filter: a named range or an explicit one, resolved in a named timezone."""

    range: str = "day"
    timezone: str = DEFAULT_TIMEZONE
    start: datetime | None = None
    end: datetime | None = None

    @classmethod
    def parse(
        cls,
        *,
        range_: str | None,
        timezone: str | None,
        start: str | None,
        end: str | None,
    ) -> Window:
        """Read the query parameters, refusing anything that would silently become a different window.

        The refusals are the point. An unknown `range` that fell back to "day" would answer a
        question nobody asked, and a `custom` with one bound missing would be resolved to something
        plausible — so both are 422s naming what was wrong.
        """
        name = (range_ or "day").strip().lower()
        if name not in RANGES:
            raise ContractViolationError(
                f"range={name!r} is not one of {', '.join(RANGES)}. A default here would answer a "
                "window nobody asked for and label it as the one they did."
            )
        zone = (timezone or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE

        if name != "custom":
            if start or end:
                raise ContractViolationError(
                    f"range={name!r} was given with from/to. The named ranges are computed from the "
                    "clock; accepting bounds here would silently ignore them."
                )
            return cls(range=name, timezone=zone)

        if not start or not end:
            raise ContractViolationError(
                "range=custom needs both from and to. One bound has no defensible default — the "
                "other end of the window would be whatever the clock said at the time."
            )
        return cls(
            range=name, timezone=zone, start=_instant(start, "from"), end=_instant(end, "to")
        )

    def resolve(self, *, now: datetime | None = None) -> TimeRange:
        """The `TimeRange` this window names. Raises :class:`TelemetryQueryError` on a bad zone."""
        if self.range == "day":
            return TimeRange.day(now=now, timezone=self.timezone)
        if self.range == "month":
            return TimeRange.month(now=now, timezone=self.timezone)
        assert self.start is not None and self.end is not None  # guaranteed by `parse`
        return TimeRange.custom(self.start, self.end, timezone=self.timezone)

    def as_dict(self, resolved: TimeRange) -> dict[str, Any]:
        """What the reader was told, with the resolved window — not the request that produced it."""
        return {
            "range": self.range,
            "timezone": resolved.timezone,
            "start": resolved.start.isoformat(),
            "end": resolved.end.isoformat(),
            "interval": resolved.calendar_interval(),
        }


def _instant(raw: str, name: str) -> datetime:
    """Parse one bound. Naive is read as local to the window's zone, by `TimeRange.custom`."""
    text = raw.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ContractViolationError(
            f"{name}={raw!r} is not an ISO-8601 instant. A date without a time has no defensible "
            "reading here: midnight in which zone?"
        ) from exc


def filters_from(
    *,
    agents: Sequence[str] = (),
    agent_versions: Sequence[str] = (),
    releases: Sequence[str] = (),
    models: Sequence[str] = (),
    synthetic: str | None = None,
) -> Filters:
    """§12's filter set, from repeated query parameters.

    `synthetic` is a three-state on purpose — `synthetic`, `live_demo`, or absent meaning *both*.
    Absent is not a third label: §15 makes the label an enum so "unlabelled" is not a silent state,
    and a filter that read absent as "synthetic only" would hide every live-demo record without
    saying it had.
    """
    return Filters(
        agents=tuple(agents),
        agent_versions=tuple(agent_versions),
        releases=tuple(releases),
        models=tuple(models),
        synthetic=synthetic.strip() if synthetic and synthetic.strip() else None,
    )


class TelemetryReads:
    """The read side of the admin API, over the telemetry indices.

    One instance per gateway process, built from `OPENSEARCH_URL`. Every method returns a
    JSON-serialisable mapping that carries `available`; nothing here raises for a query that fails,
    because a dashboard whose cluster is briefly unreachable should draw its other panels rather than
    answer 500 for the whole page.
    """

    def __init__(self, query: TelemetryQuery, traces: TelemetryQuery | None = None) -> None:
        self._query = query
        # The trace family is a second index pattern, so it is a second query object. §15 keeps the
        # families apart on purpose and `TelemetryQuery` refuses an index outside its allow-list, so
        # "read the spans" is a different client rather than a different argument.
        self._traces = traces if traces is not None else query

    @property
    def base_url(self) -> str:
        return self._query.base_url

    # -- shared ----------------------------------------------------------------------------------

    def _guarded(self, what: str, work: Any) -> dict[str, Any]:
        """Run one panel's query, turning any transport failure into that panel's "no source".

        Catches `Exception` deliberately and logs it: the caller is a chart, and the alternative —
        letting an OpenSearch hiccup become a 500 — replaces eight working panels with one error
        page. The reason string carries the exception type, so a reader can tell a refused connection
        from a query the cluster rejected.
        """
        try:
            return dict(work())
        except TelemetryQueryError as problem:
            return unavailable(f"the {what} query was refused: {problem}")
        except Exception as problem:  # see the docstring
            logger.warning("admin read %r failed: %s", what, problem)
            return unavailable(f"the {what} query did not complete: {type(problem).__name__}")

    # -- Overview (§12) --------------------------------------------------------------------------

    def overview(self, window: TimeRange, filters: Filters) -> dict[str, Any]:
        """Counts, outcomes, the three latencies, API failures, the series and index health.

        Everything §15 lists for a reading of the system, and nothing that is a rate: a rate without
        its denominator is a number a reader cannot check, so the counts travel and the page divides
        them if it wants to.
        """

        def read() -> dict[str, Any]:
            return {
                "available": True,
                "events": self._query.event_count(time_range=window, filters=filters).as_dict(),
                "agent_triggers": self._query.agent_triggers(
                    time_range=window, filters=filters
                ).as_dict(),
                "outcomes": {
                    name: sample.as_dict()
                    for name, sample in self._query.outcome_breakdown(
                        time_range=window, filters=filters
                    ).items()
                },
                "latency": {
                    kind: self._query.latency(
                        time_range=window, kind=kind, filters=filters
                    ).as_dict()
                    for kind in LATENCY_KINDS
                },
                "api_failures": [
                    bucket.as_dict()
                    for bucket in self._query.api_failures(
                        time_range=window, filters=filters, size=DIMENSION_SIZE
                    )
                ],
                "series": self._query.time_series(time_range=window, filters=filters).as_dict(),
                "index_health": self._query.index_health(),
            }

        return self._guarded("overview", read)

    # -- Agents & Versions (§12, §13) ------------------------------------------------------------

    def agents(self, window: TimeRange, filters: Filters) -> dict[str, Any]:
        """The dimensions §12 asks for: which agent, which version, which release, which model.

        Four aggregations rather than one grouped query, because the dimensions are independent —
        an agent version is not a pair with a model, and a multi-dimensional terms query would
        return only the combinations that occurred.

        This is not the whole page. §12 also requires the Agent Card, the tool/skill/sub-agent
        versions and a sample input/output pair, and those live in the registry and the A2A cards
        rather than in the telemetry indices. They are absent here rather than approximated: the
        page says which half it has.
        """

        def read() -> dict[str, Any]:
            dimensions: dict[str, list[dict[str, Any]]] = {}
            for dimension in AGENT_DIMENSIONS:
                buckets = self._query.dimension_values(
                    dimension, time_range=window, filters=filters, size=DIMENSION_SIZE
                )
                dimensions[dimension] = [bucket.as_dict() for bucket in buckets]
            return {"available": True, "dimensions": dimensions}

        return self._guarded("agent dimension", read)

    # -- Trace Explorer (§12, §15) ---------------------------------------------------------------

    def traces(self, window: TimeRange, *, synthetic: bool | None = None) -> dict[str, Any]:
        """Recent traces, newest first.

        `synthetic` is the trace document's own boolean. §15 labels synthetic traffic everywhere it
        is recorded, and a trace list that mixed fixture runs with a live demo run without saying so
        would make a reconstruction of a demo's behaviour a reconstruction of both.
        """

        def read() -> dict[str, Any]:
            return {
                "available": True,
                "traces": [
                    summary.as_dict()
                    for summary in self._traces.recent_traces(
                        time_range=window, synthetic=synthetic, size=DIMENSION_SIZE
                    )
                ],
            }

        return self._guarded("trace", read)

    def trace(self, trace_id: str) -> dict[str, Any]:
        """One trace's spans, oldest first, for the waterfall.

        A trace that is not found is an empty waterfall and not a 404: the retention policy is seven
        days, so "this trace has aged out" and "this trace never existed" are the same answer from
        here — and the page can say so more usefully than a status code can.
        """

        def read() -> dict[str, Any]:
            spans = self._traces.trace_spans(trace_id)
            return {
                "available": True,
                "trace_id": trace_id,
                "spans": [span.as_dict() for span in spans],
                "span_count": len(spans),
            }

        return self._guarded("trace detail", read)

    # -- Guardrails (§12) ------------------------------------------------------------------------

    def guardrails(self, window: TimeRange, filters: Filters) -> dict[str, Any]:
        """§9's per-layer decisions, counted. A layer that did not run has no bucket, not a zero."""

        def read() -> dict[str, Any]:
            return {
                "available": True,
                "decisions": [
                    bucket.as_dict()
                    for bucket in self._query.guardrail_decisions(
                        time_range=window, filters=filters
                    )
                ],
            }

        return self._guarded("guardrail", read)

    # -- Feedback (§12) --------------------------------------------------------------------------

    def feedback(self, window: TimeRange, filters: Filters) -> dict[str, Any]:
        """§12's three numbers, kept apart: raw count, like rate, participation rate.

        The denominator is **turns**, and the payload says so rather than leaving a reader to infer
        it. `feedback()` takes `total_responses` from its caller because it cannot be derived from
        the votes — the responses nobody rated are exactly the ones not in the index. In this stack a
        patient rates a *turn*, and §15 measures every turn (`latency.turn.ms`), so that metric's
        event count is the population a vote could have come from.
        """

        def read() -> dict[str, Any]:
            turns = self._query.event_count(
                time_range=window, filters=filters, metric=MetricName.LATENCY_TURN_MS
            )
            summary = self._query.feedback(
                time_range=window, filters=filters, total_responses=turns.count
            )
            return {
                "available": True,
                "summary": summary.as_dict(),
                "denominator": {
                    "label": "turns",
                    "metric": str(MetricName.LATENCY_TURN_MS),
                    "sample": turns.as_dict(),
                },
            }

        return self._guarded("feedback", read)

    # -- Evaluations (§12, §14) ------------------------------------------------------------------

    def evaluations(self, window: TimeRange, filters: Filters) -> dict[str, Any]:
        """§14's scores, per `eval.run_id` and metric.

        The buckets are returned as the library produced them. §14 forbids merging two judges or two
        rubrics into one series, and the grouping the library chose (`eval.run_id` then metric) is
        what keeps them apart — so this method must not reshape them into a flat list of numbers.
        """

        def read() -> dict[str, Any]:
            return {
                "available": True,
                "scores": [
                    bucket.as_dict()
                    for bucket in self._query.evaluation_scores(time_range=window, filters=filters)
                ],
            }

        return self._guarded("evaluation", read)


def build_reads(settings: GatewaySettings) -> TelemetryReads | None:
    """Build the read side from the environment, or `None` when there is no cluster to read.

    `None` is a supported state, not a misconfiguration: §18's `core` profile runs without
    OpenSearch, and the gateway that serves the patient surface must start there. ADR-0039 decision 2
    records why this is optional rather than a startup requirement — a stack without the telemetry
    cluster has admin charts with no data, which is not a reason to refuse every booking.
    """
    url = settings.opensearch_url.strip()
    if not url:
        logger.info(
            "OPENSEARCH_URL is unset, so the admin read API will answer 'no source' for every "
            "panel. The gateway serves its own surface either way."
        )
        return None
    return TelemetryReads(
        TelemetryQuery(url, timeout=TIMEOUT_SECONDS),
        # A second client for the spans: §15's trace family is a different index pattern, and
        # `TelemetryQuery` validates its index against the allow-list at construction rather than
        # letting a route choose one.
        TelemetryQuery(url, index=TRACE_INDEX_PATTERN, timeout=TIMEOUT_SECONDS),
    )
