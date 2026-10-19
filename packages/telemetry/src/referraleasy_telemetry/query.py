# ruff: noqa: RUF001, RUF002  # Turkish quoted from §15 of ReferralEasy_Architecture_v3.md is kept verbatim
"""The typed query layer the admin app reads §15 through.

Not a UI, and deliberately not an HTTP service either: this module turns the questions §12's pages
ask into OpenSearch request bodies and the answers back into typed objects. ``apps/web`` and
whatever serves it decide how to render them.

**Where the field names come from.** Two translations happen between an attribute in the code and a
field in an index, and both are easy to get wrong in a way that returns zero hits rather than an
error:

1. Data Prepper flattens ``metric.name`` to ``log.attributes.metric@name``
   (``conventions.field_name``).
2. Data Prepper does not create the index mapping, so a *string* attribute is dynamically mapped as
   ``text`` **with a ``keyword`` sub-field**, and aggregating on it requires the ``.keyword``
   suffix.

The second one is not a style choice. Both index templates set ``"dynamic": true`` and map only the
structural fields — ``traceId``, ``time``, ``body`` and so on — leaving the attribute fields to
OpenSearch's dynamic mapping. Adding explicit ``keyword`` mappings to the templates would fix that
only for indices created *afterwards*: every index that already exists keeps the mapping it was
created with, so the query layer would then have to know which of the two shapes it was talking to.
Relying on the dynamic mapping instead is consistent across old and new indices, and
``tests/telemetry/test_query_live.py`` asserts the aggregation actually runs against the live index
rather than trusting the reasoning above. What it costs is a redundant ``text`` field per string
attribute — real, and the price of not having two field shapes to reason about.

**Every aggregate carries its sample count.** §12: "sample counts on every chart". :class:`Sample`
is not optional and is not a separate call — a caller cannot get a p95 without also getting the
number of events it was computed from, because there is no method that returns one without the
other.

**Duplicates are counted once, but visible.** ``count`` is a ``cardinality`` aggregation over
``event.id``, which is the §15 dedup rule; ``documents`` is the raw hit count. When they differ,
records really were written twice, and a reader should be able to see that rather than have it
quietly corrected. Both numbers are on every :class:`Sample`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from referraleasy_telemetry.conventions import (
    ALLOWED_QUERY_PATTERNS,
    ATTR_AGENT_RELEASE,
    ATTR_AGENT_ROLE,
    ATTR_AGENT_VERSION,
    ATTR_API_ENDPOINT,
    ATTR_API_STATUS_CODE,
    ATTR_DEPLOYMENT_ANNOTATION,
    ATTR_EVAL_JUDGE,
    ATTR_EVAL_METRIC,
    ATTR_EVAL_RUBRIC_VERSION,
    ATTR_EVAL_STATUS,
    ATTR_EVENT_ID,
    ATTR_EVENT_NAME,
    ATTR_FEEDBACK_RESPONSE_ID,
    ATTR_FEEDBACK_VALUE,
    ATTR_METRIC_NAME,
    ATTR_METRIC_VALUE,
    ATTR_MODEL_NAME,
    ATTR_OUTCOME_CLASS,
    ATTR_SYNTHETIC,
    ATTR_WORKFLOW_STATE,
    EVENT_INDEX_PATTERN,
    TELEMETRY_RETENTION_DAYS,
    MetricName,
    OutcomeClass,
    Synthetic,
)
from referraleasy_telemetry.conventions import (
    DIMENSION_ATTRIBUTES as _DIMENSION_ATTRIBUTES,
)
from referraleasy_telemetry.events import (
    METRIC_UNITS,
    UnknownDimensionError,
    validate_dimensions,
)
from referraleasy_telemetry.feedback import FeedbackSummary, Vote, parse_votes, summarise_feedback

#: The suffix that reaches the ``keyword`` sub-field of a dynamically mapped string, which is what
#: an aggregation or a sort needs. See the module docstring for why this is not solved in the
#: index templates instead.
KEYWORD_SUFFIX: Final[str] = ".keyword"

#: How many buckets a terms aggregation may return before it is truncated. Every query that uses it
#: reports ``sum_other_doc_count`` so the truncation is visible — a silently truncated breakdown is
#: a wrong breakdown.
DEFAULT_TERMS_SIZE: Final[int] = 50

#: The time field per attribute family. Events carry ``time``; trace documents carry ``startTime``.
EVENT_TIME_FIELD: Final[str] = "time"
TRACE_TIME_FIELD: Final[str] = "startTime"

#: The span document's own fields, as the traces index template maps them.
#: ``durationInNanos`` is Data Prepper's unit — nanoseconds in an integer, which is why every
#: duration that leaves this module goes through :func:`_nanos_to_ms` rather than being divided at
#: each call site.
TRACE_ID_FIELD: Final[str] = "traceId"
SPAN_ID_FIELD: Final[str] = "spanId"
TRACE_PARENT_SPAN_FIELD: Final[str] = "parentSpanId"
TRACE_NAME_FIELD: Final[str] = "name"
TRACE_SERVICE_FIELD: Final[str] = "serviceName"
TRACE_KIND_FIELD: Final[str] = "kind"
TRACE_DURATION_FIELD: Final[str] = "durationInNanos"
TRACE_SYNTHETIC_FIELD: Final[str] = "synthetic"

#: §15 forbids the trace id as a metric label: a terms aggregation over it is one bucket per
#: request. Named here so the refusal in `dimension_values` reads as a rule rather than a string.
TRACE_ID_AS_DIMENSION: Final[str] = "trace_id"

#: What a span's :attr:`SpanRecord.attributes` excludes. The mapped fields are already named fields
#: on the record, and repeating them under `attributes` would leave a caller unsure which copy is
#: the one to read — and let a custom attribute shadow `name` or `kind` if one were ever emitted.
RESERVED_SPAN_FIELDS: Final[frozenset[str]] = frozenset(
    {
        TRACE_ID_FIELD,
        SPAN_ID_FIELD,
        TRACE_PARENT_SPAN_FIELD,
        TRACE_NAME_FIELD,
        TRACE_SERVICE_FIELD,
        TRACE_KIND_FIELD,
        TRACE_DURATION_FIELD,
        TRACE_SYNTHETIC_FIELD,
        TRACE_TIME_FIELD,
        "endTime",
        "traceGroup",
        "@timestamp",
    }
)

#: IANA zones accepted without a lookup, so a typo fails fast with a clear message instead of
#: ZoneInfoNotFoundError from deep inside a request builder.
_KNOWN_ZONES: Final[frozenset[str]] = frozenset(
    {"UTC", "America/Chicago", "America/New_York", "America/Los_Angeles", "Europe/Istanbul"}
)


class TelemetryQueryError(ValueError):
    """A query was refused before it was sent, or its answer was not a search response."""


class UnindexablePatternError(TelemetryQueryError):
    """A query named an index family that telemetry queries must not read (§15)."""


# ---------------------------------------------------------------------------
# Field naming
# ---------------------------------------------------------------------------


def keyword_field(attribute: str, family: str = "log") -> str:
    """The aggregatable field for a string attribute."""
    from referraleasy_telemetry.conventions import field_name

    return f"{field_name(attribute, family=family)}{KEYWORD_SUFFIX}"


def numeric_field(attribute: str, family: str = "log") -> str:
    """The field for a numeric attribute. Numbers are dynamically mapped without a sub-field."""
    from referraleasy_telemetry.conventions import field_name

    return field_name(attribute, family=family)


def source_field(attribute: str, family: str = "log") -> str:
    """The **``_source`` path** for an attribute — the name its value is stored under.

    A third translation, and unlike the other two it is not reachable from the mapping. Data Prepper
    writes the flattened attribute as a single ``_source`` key whose name *contains* the dots and the
    ``@``: the value of ``feedback.value`` is stored under the literal key
    ``"log.attributes.feedback@value"``. Neither of the searchable names is a source path:

    * ``log.attributes.feedback@value.keyword`` is a *multi-field*. OpenSearch's source filtering
      does not follow multi-fields, and it does not say so — it answers ``200`` and the path is
      simply absent from the hit.
    * ``log.attributes.feedback@value`` is the source path, and it does work.

    **Measured on the running stack, both directions.** A search with
    ``_source: ["log.attributes.feedback@value.keyword", "…@response_id.keyword", "time"]`` returned
    hits whose ``_source`` held only ``time``; the same search with the flat paths returned
    ``{"log.attributes.feedback@value": "down", "log.attributes.feedback@response_id": …}``. Passing
    the keyword name into code that reads ``_source`` is therefore a silent zero, which is exactly
    what :meth:`TelemetryQuery.feedback` did: the votes were in the index and the admin panel read
    zero.
    """
    from referraleasy_telemetry.conventions import field_name

    return field_name(attribute, family=family)


#: Pre-built field names for the hot paths, so a typo in an attribute constant cannot survive to
#: become an empty result.
EVENT_ID_FIELD: Final[str] = keyword_field(ATTR_EVENT_ID)
METRIC_NAME_FIELD: Final[str] = keyword_field(ATTR_METRIC_NAME)
METRIC_VALUE_FIELD: Final[str] = numeric_field(ATTR_METRIC_VALUE)
OUTCOME_FIELD: Final[str] = keyword_field(ATTR_OUTCOME_CLASS)
AGENT_ROLE_FIELD: Final[str] = keyword_field(ATTR_AGENT_ROLE)
FEEDBACK_RESPONSE_ID_FIELD: Final[str] = keyword_field(ATTR_FEEDBACK_RESPONSE_ID)
FEEDBACK_VALUE_FIELD: Final[str] = keyword_field(ATTR_FEEDBACK_VALUE)

#: The ``_source`` paths for the two feedback attributes, which are what a `top_hits` returns and
#: what :func:`feedback.parse_votes` reads. Distinct from the two constants above on purpose — see
#: :func:`source_field`.
FEEDBACK_RESPONSE_ID_SOURCE: Final[str] = source_field(ATTR_FEEDBACK_RESPONSE_ID)
FEEDBACK_VALUE_SOURCE: Final[str] = source_field(ATTR_FEEDBACK_VALUE)


# ---------------------------------------------------------------------------
# Request vocabulary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimeRange:
    """A window, in a named timezone. §12: "day, month, custom range, timezone".

    The timezone is not cosmetic. A "day" in ``America/Chicago`` starts at 05:00 or 06:00 UTC
    depending on the season, and a dashboard that buckets by UTC days while the operators are in
    Chicago shows a daily chart whose days do not line up with anyone's working day. The zone is
    passed to OpenSearch for both the range filter and the histogram, so the buckets and the window
    agree.
    """

    start: datetime
    end: datetime
    timezone: str = "America/Chicago"

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise TelemetryQueryError(
                f"time range ends before it starts ({self.start.isoformat()} -> "
                f"{self.end.isoformat()}); an inverted range returns zero hits rather than an "
                f"error, which reads as 'nothing happened'"
            )
        zone(self.timezone)

    @classmethod
    def day(cls, *, now: datetime | None = None, timezone: str = "America/Chicago") -> TimeRange:
        """Midnight-to-midnight today, in ``timezone``."""
        tz = zone(timezone)
        local = (now or datetime.now(UTC)).astimezone(tz)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return cls(start, start + timedelta(days=1), timezone)

    @classmethod
    def month(cls, *, now: datetime | None = None, timezone: str = "America/Chicago") -> TimeRange:
        """The first of the month to the first of the next, in ``timezone``."""
        tz = zone(timezone)
        local = (now or datetime.now(UTC)).astimezone(tz)
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (start + timedelta(days=32)).replace(day=1)
        return cls(start, next_month, timezone)

    @classmethod
    def custom(
        cls,
        start: datetime,
        end: datetime,
        *,
        timezone: str = "America/Chicago",
    ) -> TimeRange:
        """An explicit window. Naive datetimes are read as local to ``timezone``."""
        tz = zone(timezone)
        return cls(_localise(start, tz), _localise(end, tz), timezone)

    @property
    def window_days(self) -> float:
        """The window in **calendar** days, not elapsed ones.

        Python subtracts two aware datetimes that share a ``tzinfo`` by their wall-clock fields, so
        ``TimeRange.day()`` across the DST fall-back — two local midnights five and six hours behind
        UTC — reports ``1.0`` while the window is 25 hours from end to end (measured; the range
        filter is the absolute one, ``2026-11-01T05:00Z`` to ``2026-11-02T06:00Z``). That is the right
        unit for the only thing that reads this: :meth:`calendar_interval` asks how many *buckets* a
        chart wants, and a day is a day however many hours it had. It is the wrong unit for anything
        asking how much time passed, which is why it says so here instead of being called
        ``elapsed_days``.
        """
        return (self.end - self.start).total_seconds() / 86400

    def calendar_interval(self) -> str:
        """A histogram bucket width that gives a readable chart for this window.

        Chosen by ``calendar_interval`` rather than a fixed millisecond interval so that the buckets
        fall on calendar boundaries in the requested timezone — a daily chart whose buckets start at
        an arbitrary hour is a chart nobody can read.
        """
        days = self.window_days
        if days <= 2:
            return "hour"
        if days <= 62:
            return "day"
        if days <= 400:
            return "week"
        return "month"

    def to_filter(self, field_name_: str = EVENT_TIME_FIELD) -> dict[str, Any]:
        """The OpenSearch range clause, in the requested timezone."""
        return {
            "range": {
                field_name_: {
                    "gte": _iso(self.start),
                    "lte": _iso(self.end),
                    "time_zone": self.timezone,
                }
            }
        }


def zone(timezone: str) -> ZoneInfo:
    """Resolve an IANA zone, with a message naming the fix."""
    if not timezone:
        raise TelemetryQueryError("a timezone is required; an implicit UTC silently shifts the day")
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        hint = (
            "" if timezone in _KNOWN_ZONES else " (check the spelling; IANA names are Region/City)"
        )
        raise TelemetryQueryError(f"unknown timezone {timezone!r}{hint}") from exc


def _localise(value: datetime, tz: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class Filters:
    """The §12 filter set. Every field is optional; absent means "do not filter"."""

    agents: tuple[str, ...] = ()
    agent_versions: tuple[str, ...] = ()
    releases: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    event_names: tuple[str, ...] = ()
    outcomes: tuple[OutcomeClass | str, ...] = ()
    workflow_states: tuple[str, ...] = ()
    synthetic: Synthetic | str | None = None
    deployment_annotation: str | None = None
    dimensions: Mapping[str, Any] = field(default_factory=dict)

    def clauses(self) -> list[dict[str, Any]]:
        """The ``bool.filter`` clauses this filter set contributes.

        Filters go in ``filter``, never in ``must``: a scored clause would make the result set
        relevance-ordered, and every aggregation here is a counting question that relevance has no
        opinion about.
        """
        clauses: list[dict[str, Any]] = []
        for attribute, values in (
            (ATTR_AGENT_ROLE, self.agents),
            (ATTR_AGENT_VERSION, self.agent_versions),
            (ATTR_AGENT_RELEASE, self.releases),
            (ATTR_MODEL_NAME, self.models),
            (ATTR_EVENT_NAME, self.event_names),
            (ATTR_WORKFLOW_STATE, self.workflow_states),
        ):
            if values:
                clauses.append({"terms": {keyword_field(attribute): [str(v) for v in values]}})
        if self.outcomes:
            clauses.append(
                {"terms": {OUTCOME_FIELD: [str(OutcomeClass(str(o))) for o in self.outcomes]}}
            )
        if self.synthetic is not None:
            clauses.append({"term": {keyword_field(ATTR_SYNTHETIC): str(self.synthetic)}})
        if self.deployment_annotation is not None:
            clauses.append(
                {"term": {keyword_field(ATTR_DEPLOYMENT_ANNOTATION): self.deployment_annotation}}
            )
        if self.dimensions:
            # Same validation as emission, so a filter the app can build is a filter the emitter
            # could have produced. A trace_id filter is not a dimension and is refused with the
            # §15 reason rather than accepted and used to build a one-document query.
            validate_dimensions(self.dimensions)
            for key, value in self.dimensions.items():
                field_ = (
                    numeric_field(key) if isinstance(value, (int, float)) else keyword_field(key)
                )
                clauses.append({"term": {field_: value}})
        return clauses


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sample:
    """A number, and how many things it was computed from.

    §12 requires a sample count on every chart. Making it a field of every result rather than an
    optional companion is what stops one from being forgotten.

    ``count`` is the deduplicated event count (``cardinality(event.id)``) — the number a chart
    should show. ``documents`` is how many raw hits matched. When ``documents > count`` the same
    event was written more than once; the count is still right, and the difference is visible.
    """

    count: int
    documents: int
    truncated: bool = False
    other: int = 0

    @property
    def duplicated(self) -> int:
        """How many documents were duplicates of an event already counted."""
        return max(0, self.documents - self.count)

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "documents": self.documents,
            "duplicated": self.duplicated,
            "truncated": self.truncated,
            "other": self.other,
        }


@dataclass(frozen=True, slots=True)
class Percentiles:
    """p50/p95/p99 for one latency distribution, with its sample count.

    ``None`` for a percentile means no data, never zero milliseconds. A zero would plot as an
    impossibly fast system.
    """

    metric: str
    p50: float | None
    p95: float | None
    p99: float | None
    sample: Sample
    min_ms: float | None = None
    max_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "p50": self.p50,
            "p95": self.p95,
            "p99": self.p99,
            "min": self.min_ms,
            "max": self.max_ms,
            "sample": self.sample.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class Bucket:
    """One group in a breakdown."""

    key: str
    sample: Sample
    value: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "sample": self.sample.as_dict()}


@dataclass(frozen=True, slots=True)
class TimeSeries:
    """A ``date_histogram`` in the requested timezone, with the overall sample count."""

    interval: str
    timezone: str
    points: tuple[tuple[datetime, Sample], ...]
    sample: Sample

    def as_dict(self) -> dict[str, Any]:
        return {
            "interval": self.interval,
            "timezone": self.timezone,
            "points": [
                {"at": at.isoformat(), "sample": sample.as_dict()} for at, sample in self.points
            ],
            "sample": self.sample.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class TraceSummary:
    """One trace, as the list of traces shows it.

    ``duration_ms`` is the **slowest span**, not the end-to-end wall time. The two differ whenever a
    trace has concurrent spans, and reporting the slowest span as though it were the trace's duration
    is how a waterfall's own numbers stop matching the row it was opened from. It is named for what
    it is, and the waterfall shows the real start and end.
    """

    trace_id: str
    service_name: str
    entry_span: str
    started_at: datetime | None
    duration_ms: float | None
    span_count: int
    sample: Sample

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "service_name": self.service_name,
            "entry_span": self.entry_span,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "duration_ms": self.duration_ms,
            "span_count": self.span_count,
            "sample": self.sample.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class SpanRecord:
    """One span. ``attributes`` is everything the mapping did not already name a field for."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    service_name: str
    kind: str
    started_at: datetime | None
    duration_ms: float | None
    attributes: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "service_name": self.service_name,
            "kind": self.kind,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "duration_ms": self.duration_ms,
            "attributes": dict(self.attributes),
        }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _allowed_index(index: str) -> str:
    """The one place the index allow-list is enforced — construction *and* per-call override.

    §15 keeps the directory and the telemetry indices apart, and the check has to hold on every
    path that can name an index. ``search(index=...)`` reaches the same URL builder that
    construction does, so a check living only in ``__init__`` would be the same hole with a smaller
    door — and the family it would let through is the one §15 separates precisely because its
    contents are retrieved text rather than recorded telemetry.
    """
    if index not in ALLOWED_QUERY_PATTERNS:
        raise UnindexablePatternError(
            f"{index!r} is not a telemetry index family. §15 keeps the directory and the "
            f"telemetry indices apart, and a telemetry query may only read "
            f"{', '.join(ALLOWED_QUERY_PATTERNS)}."
        )
    return index


class TelemetryQuery:
    """Reads the telemetry indices. One instance per OpenSearch endpoint.

    The index is a constructor argument and is validated against
    :data:`conventions.ALLOWED_QUERY_PATTERNS` — a telemetry query cannot be pointed at the
    directory/vector family (§15: "Rehber ve telemetri indeksleri ayrılır"). That is not a
    formality: retrieved directory text is untrusted content, and a query that joined the two would
    let a document that was *retrieved* be read as a document that was *recorded*.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:9200",
        *,
        index: str = EVENT_INDEX_PATTERN,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.index = _allowed_index(index)
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout

    # -- transport -------------------------------------------------------------

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def search(self, body: dict[str, Any], *, index: str | None = None) -> dict[str, Any]:
        """Run one search and return the raw response.

        A response that is not a JSON *object* is refused rather than returned: every caller
        indexes into it (``hits``, ``aggregations``), so a JSON array or scalar would surface as a
        ``TypeError`` several frames away from the request that caused it. Checked with
        ``isinstance`` and not cast — the payload came off a socket.
        """
        target = self.index if index is None else _allowed_index(index)
        response = self._http().post(
            f"{self.base_url}/{target}/_search",
            json=body,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TelemetryQueryError(
                f"{self.base_url}/{target}/_search answered with a JSON "
                f"{type(payload).__name__}, not a search response object."
            )
        return payload

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self) -> TelemetryQuery:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- request building ------------------------------------------------------

    @staticmethod
    def body(
        *,
        time_range: TimeRange,
        filters: Filters | None = None,
        aggs: Mapping[str, Any] | None = None,
        time_field: str = EVENT_TIME_FIELD,
        size: int = 0,
    ) -> dict[str, Any]:
        """Assemble a search body: range + filters, and never any hits unless asked for."""
        must = [time_range.to_filter(time_field)]
        must.extend((filters or Filters()).clauses())
        body: dict[str, Any] = {
            "size": size,
            "query": {"bool": {"filter": must}},
            "track_total_hits": True,
        }
        if aggs:
            body["aggs"] = dict(aggs)
        return body

    # -- §15: counts -----------------------------------------------------------

    def event_count(
        self,
        *,
        time_range: TimeRange,
        filters: Filters | None = None,
        metric: MetricName | str | None = None,
        time_field: str = EVENT_TIME_FIELD,
    ) -> Sample:
        """Deduplicated event count. The `count` on every chart."""
        extra = dict(filters.dimensions) if filters else {}
        effective = filters or Filters()
        if metric is not None:
            effective = _with_dimension(effective, ATTR_METRIC_NAME, str(metric))
        del extra
        body = self.body(
            time_range=time_range,
            filters=effective,
            time_field=time_field,
            aggs={"sample": _sample_agg()},
        )
        response = self.search(body)
        return _sample_from(response)

    def agent_triggers(self, *, time_range: TimeRange, filters: Filters | None = None) -> Sample:
        """§15 "agent logical trigger count"."""
        return self.event_count(
            time_range=time_range,
            filters=filters,
            metric=None,
        )

    def outcome_breakdown(
        self, *, time_range: TimeRange, filters: Filters | None = None
    ) -> dict[str, Sample]:
        """The six outcome classes, each counted separately.

        ``success_empty`` is present as its own key and is not folded into ``error``. A caller that
        wants "failures" has to decide which classes it means — §15 does not provide that roll-up,
        and this method will not invent one.
        """
        body = self.body(
            time_range=time_range,
            filters=filters,
            aggs={"outcomes": _terms_agg(OUTCOME_FIELD, sub={"sample": _sample_agg()})},
        )
        response = self.search(body)
        buckets = response.get("aggregations", {}).get("outcomes", {})
        found = {str(bucket["key"]): _sample_from(bucket) for bucket in buckets.get("buckets", [])}
        # Every class is present, even at zero: a missing key would render as a missing bar and
        # read as "this never happens" without saying so.
        return {str(cls): found.get(str(cls), Sample(0, 0)) for cls in OutcomeClass}

    def api_failures(
        self,
        *,
        time_range: TimeRange,
        filters: Filters | None = None,
        size: int = DEFAULT_TERMS_SIZE,
    ) -> list[Bucket]:
        """§15 "API fail endpoint/status": one bucket per (endpoint, status) pair."""
        base_filters = _with_dimension(filters or Filters(), ATTR_EVENT_NAME, "api_failure")
        body = self.body(
            time_range=time_range,
            filters=base_filters,
            aggs={
                "by_endpoint": _terms_agg(
                    keyword_field(ATTR_API_ENDPOINT),
                    size=size,
                    sub={
                        "by_status": _terms_agg(
                            numeric_field(ATTR_API_STATUS_CODE),
                            size=size,
                            sub={"sample": _sample_agg()},
                        )
                    },
                )
            },
        )
        response = self.search(body)
        out: list[Bucket] = []
        for endpoint in response.get("aggregations", {}).get("by_endpoint", {}).get("buckets", []):
            for status in endpoint.get("by_status", {}).get("buckets", []):
                out.append(
                    Bucket(
                        key=f"{endpoint['key']} {status['key']}",
                        value=float(status["key"]) if _is_number(status["key"]) else None,
                        sample=_sample_from(status),
                    )
                )
        return out

    # -- §15: latency ----------------------------------------------------------

    def latency(
        self,
        *,
        time_range: TimeRange,
        kind: str = "attempt",
        filters: Filters | None = None,
        include_errors: bool = True,
    ) -> Percentiles:
        """p50/p95/p99 for one of the three latency distributions, **timeouts included**.

        ``include_errors`` defaults to ``True`` and exists only so a caller can *ask* for a
        success-only view while knowing what it is doing. §15 says timeouts are not removed from the
        distribution; a caller that sets this to ``False`` gets a different, rosier number, and the
        flag is named so that reading the call site makes that visible. The default is the honest
        one.
        """
        metric = {
            "attempt": MetricName.LATENCY_ATTEMPT_MS,
            "invocation": MetricName.LATENCY_INVOCATION_MS,
            "turn": MetricName.LATENCY_TURN_MS,
        }[kind]

        effective = _with_dimension(filters or Filters(), ATTR_METRIC_NAME, str(metric))
        if not include_errors:
            effective = Filters(
                **{
                    **_filters_kwargs(effective),
                    "outcomes": (OutcomeClass.SUCCESS_FULL, OutcomeClass.SUCCESS_PARTIAL),
                }
            )
        body = self.body(
            time_range=time_range,
            filters=effective,
            aggs={"latency": _percentiles_agg(METRIC_VALUE_FIELD), "sample": _sample_agg()},
        )
        response = self.search(body)
        values = response.get("aggregations", {}).get("latency", {}).get("values", {})
        return Percentiles(
            metric=str(metric),
            p50=_maybe_float(values.get("50.0")),
            p95=_maybe_float(values.get("95.0")),
            p99=_maybe_float(values.get("99.0")),
            min_ms=_maybe_float(values.get(MIN_PERCENT_KEY)),
            max_ms=_maybe_float(values.get(MAX_PERCENT_KEY)),
            sample=_sample_from(response),
        )

    def time_series(
        self, *, time_range: TimeRange, filters: Filters | None = None, metric: str | None = None
    ) -> TimeSeries:
        """Buckets in the requested timezone, each with its own sample count."""
        effective = filters or Filters()
        if metric is not None:
            effective = _with_dimension(effective, ATTR_METRIC_NAME, metric)
        interval = time_range.calendar_interval()
        body = self.body(
            time_range=time_range,
            filters=effective,
            aggs={
                "series": {
                    "date_histogram": {
                        "field": EVENT_TIME_FIELD,
                        "calendar_interval": interval,
                        "time_zone": time_range.timezone,
                        "min_doc_count": 0,
                    },
                    "aggs": {"sample": _sample_agg()},
                },
                "sample": _sample_agg(),
            },
        )
        response = self.search(body)
        buckets = response.get("aggregations", {}).get("series", {}).get("buckets", [])
        points: list[tuple[datetime, Sample]] = []
        for bucket in buckets:
            stamp = bucket.get("key_as_string") or bucket.get("key")
            parsed = _parse_time(stamp)
            if parsed is None:
                continue
            points.append((parsed, _sample_from(bucket)))
        return TimeSeries(
            interval=interval,
            timezone=time_range.timezone,
            points=tuple(points),
            sample=_sample_from(response),
        )

    # -- §15: cost and human work ---------------------------------------------

    def model_usage(
        self, *, time_range: TimeRange, filters: Filters | None = None
    ) -> dict[str, Percentiles]:
        """Input tokens, output tokens and cost, each with its own sample count."""
        out: dict[str, Percentiles] = {}
        for metric in (
            MetricName.MODEL_TOKENS_INPUT,
            MetricName.MODEL_TOKENS_OUTPUT,
            MetricName.MODEL_COST_USD,
        ):
            effective = _with_dimension(filters or Filters(), ATTR_METRIC_NAME, str(metric))
            body = self.body(
                time_range=time_range,
                filters=effective,
                aggs={
                    "m": _percentiles_agg(METRIC_VALUE_FIELD, percents=(50, 95)),
                    "sample": _sample_agg(),
                },
            )
            response = self.search(body)
            values = response.get("aggregations", {}).get("m", {}).get("values", {})
            out[str(metric)] = Percentiles(
                metric=str(metric),
                p50=_maybe_float(values.get("50.0")),
                p95=_maybe_float(values.get("95.0")),
                p99=None,
                min_ms=_maybe_float(values.get(MIN_PERCENT_KEY)),
                max_ms=_maybe_float(values.get(MAX_PERCENT_KEY)),
                sample=_sample_from(response),
            )
        return out

    def hitl(
        self, *, time_range: TimeRange, filters: Filters | None = None
    ) -> dict[str, Percentiles]:
        """§15 "HITL bekleme/çözüm" — waiting and resolution as two separate distributions."""
        out: dict[str, Percentiles] = {}
        for metric in (MetricName.HITL_WAIT_MS, MetricName.HITL_RESOLVE_MS):
            effective = _with_dimension(filters or Filters(), ATTR_METRIC_NAME, str(metric))
            body = self.body(
                time_range=time_range,
                filters=effective,
                aggs={
                    "m": _percentiles_agg(METRIC_VALUE_FIELD, percents=(50, 95)),
                    "sample": _sample_agg(),
                },
            )
            response = self.search(body)
            values = response.get("aggregations", {}).get("m", {}).get("values", {})
            out[str(metric)] = Percentiles(
                metric=str(metric),
                p50=_maybe_float(values.get("50.0")),
                p95=_maybe_float(values.get("95.0")),
                p99=None,
                sample=_sample_from(response),
            )
        return out

    def guardrail_decisions(
        self, *, time_range: TimeRange, filters: Filters | None = None
    ) -> list[Bucket]:
        """§15 "guardrail kararları", broken down by decision and stage."""
        body = self.body(
            time_range=time_range,
            filters=_with_dimension(filters or Filters(), ATTR_EVENT_NAME, "guardrail_decision"),
            aggs={
                "by_decision": _terms_agg(
                    keyword_field("guardrail.decision"),
                    sub={"sample": _sample_agg()},
                )
            },
        )
        response = self.search(body)
        return [
            Bucket(key=str(bucket["key"]), sample=_sample_from(bucket))
            for bucket in response.get("aggregations", {}).get("by_decision", {}).get("buckets", [])
        ]

    def evaluation_scores(
        self,
        *,
        time_range: TimeRange,
        filters: Filters | None = None,
        index: str = EVENT_INDEX_PATTERN,
    ) -> list[Bucket]:
        """§14/§15 evaluation scores, split by metric, judge and rubric.

        **This reads §15's event stream, not the evaluation index**, and the default says so. An
        `eval_score` is a telemetry event — `event.name = "eval_score"`, `metric.name =
        "eval.score"` — written by the Data Prepper event pipeline into `otel-events-*`, and every
        field aggregated below (`eval.metric`, `eval.judge`, `eval.rubric_version`, `eval.status`)
        is an attribute of that event.

        `referraleasy-evaluation-*` is a *different data class*, not a longer-lived copy of this
        one: §14's regression baseline of sampled turns, with its own schema (`turn_id`,
        `release_id`, `judge_model_id`, `score`, `passed`) and its own 30-day retention. It carries
        none of the fields this method aggregates on, so reading it here would answer zero buckets
        rather than the same answer from a warmer store. The writer §14 describes — the sampler
        that records turns at `EVAL_SAMPLE_RATE` — is not implemented, so that family is empty; its
        index template and ISM policy are in place for the day it is.

        The three are separate buckets rather than one average because §14 forbids merging
        different judges and rubrics into a single series, and an average across them is exactly
        that merge with the seams hidden.

        ``eval.status`` is a bucket key, and only ``scored`` records carry a number — ``N/A`` and
        ``eval_error`` records are emitted with ``metric.value = 0`` precisely so they cannot leak
        into an average through a missing value, and the caller filters on ``status`` to exclude
        them. The buckets make the exclusion a visible choice rather than an invisible one.
        """
        agg_name = "by_metric"
        body = self.body(
            time_range=time_range,
            filters=_with_dimension(filters or Filters(), ATTR_EVENT_NAME, "eval_score"),
            time_field=EVENT_TIME_FIELD,
            aggs={
                agg_name: _terms_agg(
                    keyword_field(ATTR_EVAL_METRIC),
                    sub={
                        "by_judge": _terms_agg(
                            keyword_field(ATTR_EVAL_JUDGE),
                            sub={
                                "by_rubric": _terms_agg(
                                    keyword_field(ATTR_EVAL_RUBRIC_VERSION),
                                    sub={
                                        "by_status": _terms_agg(
                                            keyword_field(ATTR_EVAL_STATUS),
                                            sub={
                                                "score": _percentiles_agg(
                                                    METRIC_VALUE_FIELD, percents=(50,)
                                                ),
                                                "sample": _sample_agg(),
                                            },
                                        )
                                    },
                                )
                            },
                        )
                    },
                )
            },
        )
        response = self.search(body, index=index)
        out: list[Bucket] = []
        for metric in response.get("aggregations", {}).get(agg_name, {}).get("buckets", []):
            for judge in metric.get("by_judge", {}).get("buckets", []):
                for rubric in judge.get("by_rubric", {}).get("buckets", []):
                    for status in rubric.get("by_status", {}).get("buckets", []):
                        scored = status.get("score", {}).get("values", {}).get("50.0")
                        out.append(
                            Bucket(
                                key=f"{metric['key']}|{judge['key']}|{rubric['key']}|{status['key']}",
                                value=_maybe_float(scored) if status["key"] == "scored" else None,
                                sample=_sample_from(status),
                            )
                        )
        return out

    # -- §15: feedback ---------------------------------------------------------

    def feedback(
        self,
        *,
        time_range: TimeRange,
        total_responses: int,
        filters: Filters | None = None,
        size: int = 1000,
    ) -> FeedbackSummary:
        """The three feedback numbers, with the last-active collapse applied in the query.

        Two aggregations do the work: a ``terms`` over ``response_id`` and a ``top_hits`` of size 1
        sorted by newest first inside it. ``top_hits`` is what makes "last active" a property of the
        *search* — the alternative, fetching every vote and collapsing in Python, is the same
        answer computed after moving every document over the network, and it stops working the
        moment the window is a month.

        ``total_responses`` is passed by the caller because it cannot be derived from the votes: the
        responses nobody rated are exactly the ones not present here.

        **The aggregation reads a ``keyword`` field and the hits read a ``_source`` path**, and
        getting that backwards is silent. ``by_response`` groups on
        ``log.attributes.feedback@response_id.keyword`` because a ``terms`` aggregation on the
        ``text`` field would bucket by token; the ``top_hits`` asks for ``log.attributes.feedback@value``
        because that is the name the value is stored under, and source filtering does not follow a
        multi-field — it just omits it. Measured both ways on the running stack; see
        :func:`source_field`. The first version of this method used the keyword names in both places
        and returned zero votes for a populated index.

        **Sort resolution is one millisecond.** ``time`` is mapped as
        ``strict_date_optional_time||epoch_millis`` in the events template, so the sort key is the
        epoch-millisecond value: the ``_source`` keeps microseconds, the ordering does not. Two votes
        on one response inside the same millisecond therefore tie and the tie is resolved by index
        order, not by the timestamps. A person thumbing up and then down cannot do that; two records
        written by one batch can. Recorded rather than worked around, because the alternative —
        changing the mapping — would give the indices that already exist a different second shape.
        """
        effective = _with_dimension(filters or Filters(), ATTR_EVENT_NAME, "feedback")
        body = self.body(
            time_range=time_range,
            filters=effective,
            aggs={
                "by_response": {
                    "terms": {"field": FEEDBACK_RESPONSE_ID_FIELD, "size": size},
                    "aggs": {
                        "latest": {
                            "top_hits": {
                                "size": 1,
                                # The timestamp, then insertion order. ``time`` ties at millisecond
                                # resolution, and ``_doc`` order follows the order documents were
                                # written for an append-only index, so the later write wins. That is a
                                # heuristic, stated as one: it is what makes a flip-flop written by
                                # one batch resolve to the later of the two rather than to whichever
                                # document Lucene happened to visit first. Measured — a pair 43 µs
                                # apart returned the later vote, with sort ``[<ms>, <doc id>]``.
                                "sort": [
                                    {EVENT_TIME_FIELD: {"order": "desc"}},
                                    {"_doc": {"order": "desc"}},
                                ],
                                "_source": [
                                    FEEDBACK_VALUE_SOURCE,
                                    FEEDBACK_RESPONSE_ID_SOURCE,
                                    EVENT_TIME_FIELD,
                                ],
                            }
                        }
                    },
                }
            },
        )
        response = self.search(body)
        agg = response.get("aggregations", {}).get("by_response", {})
        votes: list[Vote] = []
        for bucket in agg.get("buckets", []):
            hits = bucket.get("latest", {}).get("hits", {}).get("hits", [])
            if not hits:
                continue
            parsed = parse_votes(
                hits,
                value_field=FEEDBACK_VALUE_SOURCE,
                response_id_field=FEEDBACK_RESPONSE_ID_SOURCE,
                time_field=EVENT_TIME_FIELD,
            )
            votes.extend(parsed)

        other = int(agg.get("sum_other_doc_count", 0) or 0)
        return summarise_feedback(
            votes,
            total_responses=total_responses,
            truncated=other > 0,
            other_responses=other,
        )

    # -- §12: dimension values ---------------------------------------------------

    def dimension_values(
        self,
        dimension: str,
        *,
        time_range: TimeRange,
        filters: Filters | None = None,
        size: int = DEFAULT_TERMS_SIZE,
    ) -> list[Bucket]:
        """The distinct values one dimension took, and how many events carried each.

        This is what fills a filter list, and it is also the read behind §12's *"which versions
        produced this traffic"* — the same aggregation over ``agent.version``, ``agent.release`` or
        ``model.name``.

        **A dimension and not a field.** The argument goes through :func:`validate_dimensions`, the
        same validation emission uses, so a caller cannot ask for a value of ``trace_id`` — that is
        not a dimension and a terms aggregation over high-cardinality ids is a different query
        wearing this one's name. The refusal names §15 rather than returning a big list.

        ``sum_other_doc_count`` becomes ``Sample.other``: when the cardinality exceeds ``size`` the
        answer is a *truncated* list, and a filter list that silently omits values is one an operator
        cannot tell from a complete one.
        """
        # The *name* half of `validate_dimensions`, and not the whole of it: that function takes a
        # `{dimension: value}` mapping because emission has a value to check against a closed
        # vocabulary, while this method has none. Calling it with a placeholder would refuse a
        # legitimate dimension whose vocabulary the placeholder is not in — which is how this was
        # written first, and `agent.role="probe"` is exactly that mistake.
        if dimension == TRACE_ID_AS_DIMENSION:
            raise UnknownDimensionError(
                "'trace_id' is not a dimension (§15: trace ID metric label yapılmaz). A terms "
                "aggregation on it produces one bucket per request and answers nothing."
            )
        if dimension not in event_dimensions():
            raise UnknownDimensionError(
                f"{dimension!r} is not a dimension. Allowed dimensions are the bounded, "
                "closed-vocabulary attributes in conventions.DIMENSION_ATTRIBUTES; per-request "
                "identifiers belong in `extra_fields`."
            )
        field_ = keyword_field(dimension)
        body = self.body(
            time_range=time_range,
            filters=filters,
            aggs={
                "values": {
                    "terms": {"field": field_, "size": size},
                    "aggs": {"sample": _sample_agg()},
                }
            },
        )
        response = self.search(body)
        agg = response.get("aggregations", {}).get("values", {})
        other = int(agg.get("sum_other_doc_count", 0) or 0)
        buckets: list[Bucket] = []
        for bucket in agg.get("buckets", []):
            sample = _sample_from(bucket)
            buckets.append(
                Bucket(
                    key=str(bucket.get("key", "")),
                    sample=Sample(
                        count=sample.count,
                        documents=sample.documents,
                        other=other,
                    ),
                    value=float(bucket.get("doc_count", 0) or 0),
                )
            )
        return buckets

    # -- §12: traces -------------------------------------------------------------

    def recent_traces(
        self,
        *,
        time_range: TimeRange,
        synthetic: bool | None = None,
        size: int = DEFAULT_TERMS_SIZE,
    ) -> list[TraceSummary]:
        """One row per trace in the window, newest first.

        **`synthetic` here is the trace document's top-level boolean, not §15's attribute.** The two
        index families carry the label differently — events under ``log.attributes.synthetic`` and
        spans as a mapped field — so this method takes a bool rather than a `Filters`, which would
        have to pretend the same field path works for both. Trace queries therefore do not accept the
        event filter set at all, and that is a limitation stated rather than papered over.

        The ordering is by each trace's first span, and it comes from the `terms` aggregation's own
        `order` rather than from sorting afterwards in Python — which would sort only the page that
        was returned.
        """
        body = self.body(
            time_range=time_range,
            time_field=TRACE_TIME_FIELD,
            aggs={
                "traces": {
                    "terms": {
                        "field": TRACE_ID_FIELD,
                        "size": size,
                        "order": {"first": "desc"},
                    },
                    "aggs": {
                        "first": {"min": {"field": TRACE_TIME_FIELD}},
                        "spans": {
                            "cardinality": {"field": SPAN_ID_FIELD, "precision_threshold": 10_000}
                        },
                        "slowest": {"max": {"field": TRACE_DURATION_FIELD}},
                        "entry": {
                            "top_hits": {
                                "size": 1,
                                "sort": [{TRACE_TIME_FIELD: {"order": "asc"}}],
                                "_source": [TRACE_SERVICE_FIELD, TRACE_NAME_FIELD],
                            }
                        },
                    },
                }
            },
        )
        if synthetic is not None:
            body["query"]["bool"]["filter"].append({"term": {TRACE_SYNTHETIC_FIELD: synthetic}})
        response = self.search(body)
        agg = response.get("aggregations", {}).get("traces", {})
        other = int(agg.get("sum_other_doc_count", 0) or 0)
        summaries: list[TraceSummary] = []
        for bucket in agg.get("buckets", []):
            hits = bucket.get("entry", {}).get("hits", {}).get("hits", [])
            source = hits[0].get("_source", {}) if hits else {}
            summaries.append(
                TraceSummary(
                    trace_id=str(bucket.get("key", "")),
                    service_name=str(source.get(TRACE_SERVICE_FIELD, "")),
                    entry_span=str(source.get(TRACE_NAME_FIELD, "")),
                    started_at=_parse_time(bucket.get("first", {}).get("value")),
                    duration_ms=_nanos_to_ms(bucket.get("slowest", {}).get("value")),
                    span_count=int(bucket.get("spans", {}).get("value", 0) or 0),
                    sample=Sample(
                        count=int(bucket.get("doc_count", 0) or 0),
                        documents=int(bucket.get("doc_count", 0) or 0),
                        other=other,
                    ),
                )
            )
        return summaries

    def trace_spans(self, trace_id: str, *, size: int = 500) -> list[SpanRecord]:
        """Every span of one trace, oldest first.

        **No time range, and that is deliberate.** A trace is identified exactly, and windowing it
        would produce a *partial* waterfall — a trace whose root span began before midnight would
        render without it, and a waterfall missing its root looks like a trace that did not have
        one. The retention policy is what bounds this, not a range the caller passed.

        The span count is not capped silently: a trace longer than ``size`` returns the first ``size``
        spans and the caller can tell, because the returned list is exactly ``size`` long.
        """
        body: dict[str, Any] = {
            "size": size,
            "query": {"bool": {"filter": [{"term": {TRACE_ID_FIELD: trace_id}}]}},
            "sort": [{TRACE_TIME_FIELD: {"order": "asc"}}],
        }
        response = self.search(body)
        spans: list[SpanRecord] = []
        for hit in response.get("hits", {}).get("hits", []):
            source = hit.get("_source", {})
            if not isinstance(source, Mapping):
                continue
            spans.append(
                SpanRecord(
                    trace_id=str(source.get(TRACE_ID_FIELD, trace_id)),
                    span_id=str(source.get(SPAN_ID_FIELD, "")),
                    parent_span_id=(
                        str(source[TRACE_PARENT_SPAN_FIELD])
                        if source.get(TRACE_PARENT_SPAN_FIELD)
                        else None
                    ),
                    name=str(source.get(TRACE_NAME_FIELD, "")),
                    service_name=str(source.get(TRACE_SERVICE_FIELD, "")),
                    kind=str(source.get(TRACE_KIND_FIELD, "")),
                    started_at=_parse_time(source.get(TRACE_TIME_FIELD)),
                    duration_ms=_nanos_to_ms(source.get(TRACE_DURATION_FIELD)),
                    attributes={
                        str(key): value
                        for key, value in source.items()
                        if key not in RESERVED_SPAN_FIELDS
                    },
                )
            )
        return spans

    # -- index health ----------------------------------------------------------

    def index_health(self) -> dict[str, Any]:
        """Which indices exist, and the retention §15 promises for them."""
        response = self._http().get(f"{self.base_url}/_cat/indices/{self.index}?format=json")
        response.raise_for_status()
        indices = sorted(
            (
                {"index": row.get("index"), "docs": _maybe_int(row.get("docs.count"))}
                for row in response.json()
            ),
            key=lambda row: str(row["index"]),
        )
        return {
            "pattern": self.index,
            "indices": indices,
            "retention_days": TELEMETRY_RETENTION_DAYS,
        }


# ---------------------------------------------------------------------------
# Aggregation builders
# ---------------------------------------------------------------------------


def _sample_agg() -> dict[str, Any]:
    """The deduplicated count, plus the raw document count for the same scope.

    ``cardinality`` on ``event.id`` is the §15 dedup rule. ``precision_threshold`` is raised above
    the default 3000 for correctness on a demo-sized index; it is a *threshold* rather than a
    guarantee — the aggregation is exact below it and approximate above — so the raw
    ``doc_count`` is carried alongside and the difference is reported as duplication rather than
    asserted to be zero.

    **Returns the agg's body, not a named aggregation.** Every call site names it ``sample`` —
    ``aggs={"sample": _sample_agg()}`` or ``sub={"sample": _sample_agg()}`` — so the wire form is
    ``{"sample": {"cardinality": {...}}}`` in all of them.

    Two defects lived here, and they are worth telling apart because neither was visible to the
    mocked tests.

    This function returned ``{"distinct_events": {"cardinality": {...}}}`` first, which the callers
    that wrote ``{"sample": _sample_agg()}`` wrapped into ``{"sample": {"distinct_events": {...}}}``
    — and OpenSearch reads that as an aggregation *named* ``sample`` of *type* ``distinct_events``, a
    type that does not exist. Every query carrying a sample count was rejected before reaching a
    shard::

        {"error": {"root_cause": [{"type": "parsing_exception",
         "reason": "Unknown aggregation type [distinct_events]"}]}, "status": 400}

    The two call sites that wrote ``sub=_sample_agg()`` (the outcome breakdown and the API-failure
    breakdown) produced ``{"aggs": {"distinct_events": {"cardinality": ...}}}``, which was *valid* —
    so the file held two incompatible conventions for one helper and neither shape could serve both.
    A test would have had to assert the request path in both places to notice; none did.

    Both were found by running the query layer against the running cluster, which is what
    ``test_query_live.py`` is for. A stub that agrees with the builder cannot fail on the builder.
    """
    return {"cardinality": {"field": EVENT_ID_FIELD, "precision_threshold": 40000}}


def _percentiles_agg(field: str, percents: tuple[int, ...] = (50, 95, 99)) -> dict[str, Any]:
    """A percentiles aggregation that also reports the two bounds.

    **min and max have to be asked for as the 0th and 100th percentile.** Measured against the
    running node: a ``percentiles`` aggregation returns *only* the percents that were requested.
    With 23 documents in range the response was ``{"50.0": 1.0, "95.0": 2.0, "99.0": 1234.5}`` and
    no ``min``/``max`` keys at all, so reading ``values.get("min")`` returns ``None`` on every query
    — silently. The admin layer would have shown "min: no data" beside a perfectly good p95 forever.

    OpenSearch computes the 0th and 100th percentiles exactly, so asking for them is the same two
    numbers for two extra entries in one response that was already being sent.
    """
    return {
        "percentiles": {
            "field": field,
            "percents": sorted({*percents, 0, 100}),
            "keyed": True,
        }
    }


#: The response keys for the two bounds, which are percentiles rather than names. See
#: :func:`_percentiles_agg`.
MIN_PERCENT_KEY: Final[str] = "0.0"
MAX_PERCENT_KEY: Final[str] = "100.0"


def _terms_agg(
    field: str, *, size: int = DEFAULT_TERMS_SIZE, sub: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    agg: dict[str, Any] = {"terms": {"field": field, "size": size}}
    if sub:
        agg["aggs"] = dict(sub)
    return agg


def _nanos_to_ms(value: Any) -> float | None:
    """Data Prepper's nanosecond duration as milliseconds, or `None`.

    `None` rather than `0.0` for a missing or unparseable value, for the same reason every percentile
    is nullable: a span that reported no duration is not a span that took no time, and a waterfall
    drawing it at zero would show it as instantaneous.
    """
    number = _maybe_float(value)
    return None if number is None else number / 1_000_000


def _sample_from(node: Mapping[str, Any]) -> Sample:
    """Build a :class:`Sample` from a search response or from one aggregation bucket.

    Two callers, two shapes, and the difference is the ``doc_count``: a search response carries its
    total under ``hits.total.value`` while a bucket carries it as ``doc_count``. The sample
    aggregation itself is ``{"sample": {"value": n}}`` in both — that is the cardinality agg named
    ``sample`` and the key under it, which is now the only shape accepted. The earlier version also
    unwrapped a ``{"sample": {"distinct_events": {"value": n}}}`` nesting, which was the shape
    :func:`_sample_agg` produced and the shape OpenSearch rejects before running anything; accepting
    it here is exactly what let the wrong builder look right, because the stubs agreed with it.
    """
    aggregations = node.get("aggregations")
    if isinstance(aggregations, Mapping):
        container = aggregations
        doc_count = int(node.get("hits", {}).get("total", {}).get("value", 0) or 0)
    else:
        container = node
        doc_count = int(node.get("doc_count", 0) or 0)

    sample_agg = container.get("sample")
    count = int(sample_agg.get("value", 0) or 0) if isinstance(sample_agg, Mapping) else 0
    return Sample(count=count, documents=doc_count)


def _with_dimension(filters: Filters, key: str, value: Any) -> Filters:
    """A copy of ``filters`` with one more dimension term."""
    dimensions = dict(filters.dimensions)
    dimensions[key] = value
    return Filters(**_filters_kwargs(filters, dimensions=dimensions))


def _filters_kwargs(filters: Filters, **overrides: Any) -> dict[str, Any]:
    return {
        "agents": filters.agents,
        "agent_versions": filters.agent_versions,
        "releases": filters.releases,
        "models": filters.models,
        "event_names": filters.event_names,
        "outcomes": filters.outcomes,
        "workflow_states": filters.workflow_states,
        "synthetic": filters.synthetic,
        "deployment_annotation": filters.deployment_annotation,
        "dimensions": filters.dimensions,
        **overrides,
    }


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # OpenSearch reports an unbounded percentile as +/-Infinity or NaN; neither is a reading, and
    # charting Infinity as a latency is worse than charting nothing.
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _maybe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def event_dimensions() -> Sequence[str]:
    """The dimensions a caller may group by. Re-exported so callers do not import conventions."""
    return _DIMENSION_ATTRIBUTES


#: Every metric, with its unit. Exposed so an admin form can enumerate what is chartable rather
#: than hard-coding the list a second time.
METRIC_UNITS_BY_NAME: Final[Mapping[str, str]] = dict(METRIC_UNITS)


__all__ = [
    "EVENT_ID_FIELD",
    "FEEDBACK_RESPONSE_ID_FIELD",
    "FEEDBACK_RESPONSE_ID_SOURCE",
    "FEEDBACK_VALUE_FIELD",
    "FEEDBACK_VALUE_SOURCE",
    "RESERVED_SPAN_FIELDS",
    "SPAN_ID_FIELD",
    "TRACE_DURATION_FIELD",
    "TRACE_ID_FIELD",
    "Filters",
    "Percentiles",
    "Sample",
    "SpanRecord",
    "TelemetryQuery",
    "TelemetryQueryError",
    "TimeRange",
    "TimeSeries",
    "TraceSummary",
    "UnindexablePatternError",
    "keyword_field",
    "numeric_field",
    "source_field",
]
