"""The typed query layer: the bodies it sends, the refusals it makes, the answers it builds (§15).

    Filters for day, month, custom range, timezone, agent/version, release/model, and
    synthetic/live-demo traffic.
    Deployment annotations and sample counts are included.

**This file is the weaker of the two verifications, and it is labelled so on purpose.** Every
response below is canned, served by an ``httpx.MockTransport``. That proves the request body this
package *sends* and the arithmetic it does on the reply. It proves nothing about whether OpenSearch
accepts the body, whether the field names match the mapping, or whether an aggregation that looks
right returns anything at all. ``tests/telemetry/test_query_live.py`` runs the same methods against
the running index, and where the two disagree the live one is right.

The distinction is not academic here. Three of this module's request bodies were wrong in ways that
returned an empty result rather than an error, and all three were found live, not here:

* the ``_source`` filter asked for ``log.attributes.feedback@value.keyword`` — a multi-field, which
  source filtering does not follow, so the votes arrived as ``None`` and every feedback panel read
  zero while the documents were in the index (``source_field``);
* a ``term`` query on a dynamically mapped string attribute matches *tokens*, so
  ``log.attributes.deployment@annotation`` with the value ``probe-1`` matches nothing, while the same
  query on the ``.keyword`` sub-field matches everything (``keyword_field``);
* the aggregation that groups feedback by response needs the ``text`` field's sub-field, and the
  ``top_hits`` inside it needs the source path. Both are in the same query, and they are different
  strings.

That is why the live file exists and why this one's tests assert the *field names* explicitly: a
field-name mistake is the failure mode, so the names are the thing under test.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from referraleasy_telemetry.conventions import (
    ALLOWED_QUERY_PATTERNS,
    DIRECTORY_INDEX_PATTERN,
    EVALUATION_INDEX_PATTERN,
    EVENT_INDEX_PATTERN,
    TELEMETRY_RETENTION_DAYS,
    MetricName,
    OutcomeClass,
)
from referraleasy_telemetry.events import UnknownDimensionError
from referraleasy_telemetry.query import (
    EVENT_ID_FIELD,
    FEEDBACK_RESPONSE_ID_FIELD,
    FEEDBACK_RESPONSE_ID_SOURCE,
    FEEDBACK_VALUE_FIELD,
    FEEDBACK_VALUE_SOURCE,
    METRIC_UNITS_BY_NAME,
    Bucket,
    Filters,
    Percentiles,
    Sample,
    TelemetryQuery,
    TelemetryQueryError,
    TimeRange,
    TimeSeries,
    UnindexablePatternError,
    event_dimensions,
    keyword_field,
    numeric_field,
    source_field,
    zone,
)


class Stub:
    """A ``MockTransport`` that answers with canned payloads and records what it was asked.

    ``bodies`` is the point: most of what this layer does is decide *which* query to send, and the
    only way to check a decision like "the ``_source`` filter uses the source path and the
    aggregation uses the keyword field" is to look at the body that went out.
    """

    def __init__(self, *payloads: Any, status: int = 200) -> None:
        self.payloads = list(payloads)
        self.bodies: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.bodies.append(json.loads(request.content) if request.content else {})
        if not self.payloads:
            payload: Any = {}
        else:
            payload = self.payloads[min(len(self.bodies) - 1, len(self.payloads) - 1)]
        return httpx.Response(self.status, json=payload)

    @property
    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    @property
    def last_query(self) -> dict[str, Any]:
        return self.bodies[-1].get("query", {}).get("bool", {})

    @property
    def last_clauses(self) -> list[dict[str, Any]]:
        return list(self.last_query.get("filter", []))


def _query(stub: Stub, *, index: str = EVENT_INDEX_PATTERN) -> TelemetryQuery:
    return TelemetryQuery("http://opensearch:9200", index=index, client=stub.client)


def _range(days: float = 1.0, *, timezone: str = "UTC") -> TimeRange:
    """A window of ``days`` ending where it starts, in UTC unless said otherwise."""
    start = datetime(2026, 10, 14, tzinfo=UTC)
    return TimeRange.custom(start, start + timedelta(days=days), timezone=timezone)


def _sample_response(
    *, distinct: int, documents: int, extra: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """A search response carrying the sample aggregation, in the shape the cluster returns it.

    ``{"sample": {"value": n}}`` — a cardinality agg named ``sample``, and its ``value``. This
    helper used to build ``{"sample": {"distinct_events": {"value": n}}}`` instead, which was the
    shape :func:`~referraleasy_telemetry.query._sample_agg` *produced* and the shape OpenSearch
    rejects with ``400 Unknown aggregation type [distinct_events]``. The stubs agreeing with the
    builder is what kept the defect invisible to this file; the live tests found it on the first
    run.
    """
    body: dict[str, Any] = {
        "hits": {"total": {"value": documents, "relation": "eq"}},
        "aggregations": {"sample": {"value": distinct}},
    }
    if extra:
        body["aggregations"].update(extra)
    return body


# ---------------------------------------------------------------------------
# Field naming
# ---------------------------------------------------------------------------


class TestFieldNaming:
    """The three names an attribute has, and the two that must never be swapped."""

    def test_an_aggregatable_string_is_the_keyword_sub_field(self) -> None:
        assert keyword_field("event.name") == "log.attributes.event@name.keyword"

    def test_a_numeric_attribute_has_no_sub_field(self) -> None:
        """Numbers are dynamically mapped without one, and appending `.keyword` to a number asks for
        a field that does not exist — which OpenSearch answers with an empty result, not an error."""
        assert numeric_field("metric.value") == "log.attributes.metric@value"
        assert numeric_field("attempt.number") == "log.attributes.attempt@number"

    def test_the_source_path_is_neither_of_the_searchable_names(self) -> None:
        """Measured live: `_source: ["…@value.keyword"]` returns a hit whose `_source` holds only
        `time`, and the same search with the flat path returns the value. The two are different
        strings and only one of them works for reading."""
        assert source_field("feedback.value") == "log.attributes.feedback@value"
        assert source_field("feedback.value") != FEEDBACK_VALUE_FIELD
        assert not source_field("feedback.value").endswith(".keyword")

    def test_the_dot_in_the_attribute_becomes_an_at_sign(self) -> None:
        """This is the translation that makes the field names match Data Prepper's output, and every
        name in `_source` confirms it: a live document carries `log.attributes.metric@name`, never
        `log.attributes.metric.name`."""
        assert "@" in keyword_field("latency.kind")
        assert "." not in keyword_field("latency.kind").split("attributes.")[1].removesuffix(
            ".keyword"
        )

    def test_the_prebuilt_field_names_are_the_live_ones(self) -> None:
        assert EVENT_ID_FIELD == "log.attributes.event@id.keyword"
        assert FEEDBACK_RESPONSE_ID_FIELD == "log.attributes.feedback@response_id.keyword"
        assert FEEDBACK_VALUE_FIELD == "log.attributes.feedback@value.keyword"
        assert FEEDBACK_RESPONSE_ID_SOURCE == "log.attributes.feedback@response_id"
        assert FEEDBACK_VALUE_SOURCE == "log.attributes.feedback@value"


# ---------------------------------------------------------------------------
# Time ranges
# ---------------------------------------------------------------------------


class TestTimeRange:
    def test_a_day_is_a_local_day_and_not_a_utc_one(self) -> None:
        """Chicago is five hours behind UTC in October, so midnight in Chicago is 05:00 UTC. The
        window has to move with the zone or a daily chart is offset from everybody's working day."""
        window = TimeRange.day(now=datetime(2026, 10, 15, 12, 0, tzinfo=UTC))
        assert window.timezone == "America/Chicago"
        assert window.start.utcoffset() == timedelta(hours=-5)
        assert window.to_filter()["range"]["time"]["gte"] == "2026-10-15T05:00:00+00:00"
        assert window.to_filter()["range"]["time"]["lte"] == "2026-10-16T05:00:00+00:00"
        assert window.window_days == pytest.approx(1.0)

    def test_a_day_in_chicago_is_twenty_five_hours_long_but_counts_as_one_day(self) -> None:
        """The DST fall-back, and the one place where "days" is ambiguous.

        The two local midnights carry different offsets, so the window is 25 hours of elapsed time
        and 1.0 of ``window_days``. That is not a rounding error: Python subtracts two aware
        datetimes sharing a ``tzinfo`` by their wall-clock fields, so ``window_days`` measures
        calendar days. It is the right unit for its only consumer — ``calendar_interval`` asks how
        many *buckets* a chart wants, and a day is a day — and it is the wrong unit for anything
        that wants elapsed time, so both numbers are asserted here and the query layer's docstring
        says which one the property is.
        """
        window = TimeRange.day(now=datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
        assert window.start.utcoffset() == timedelta(hours=-5), "still CDT at midnight"
        assert window.end.utcoffset() == timedelta(hours=-6), "CST by the next midnight"

        elapsed = (window.end.astimezone(UTC) - window.start.astimezone(UTC)).total_seconds() / 3600
        assert elapsed == 25.0
        assert window.window_days == 1.0, "calendar days, which is what the bucket width wants"

        spec = window.to_filter()["range"]["time"]
        assert (spec["gte"], spec["lte"]) == (
            "2026-11-01T05:00:00+00:00",
            "2026-11-02T06:00:00+00:00",
        )

    def test_a_month_runs_from_the_first_to_the_first(self) -> None:
        window = TimeRange.month(now=datetime(2026, 10, 15, 12, 0, tzinfo=UTC))
        assert window.window_days == pytest.approx(31.0)
        assert window.calendar_interval() == "day"

    def test_the_month_rolls_the_year_over(self) -> None:
        window = TimeRange.month(now=datetime(2026, 12, 20, 12, 0, tzinfo=UTC))
        assert window.end.year == 2027
        assert window.end.month == 1

    def test_a_custom_range_reads_naive_datetimes_as_local(self) -> None:
        """Passing a bare date-time means the operator's date-time. Reading it as UTC would shift a
        nine-to-five window to 14:00-22:00 without saying anything."""
        window = TimeRange.custom(
            datetime(2026, 10, 15, 9, 0),
            datetime(2026, 10, 15, 17, 0),
            timezone="America/Chicago",
        )
        spec = window.to_filter()["range"]["time"]
        assert spec["gte"] == "2026-10-15T14:00:00+00:00"
        assert spec["lte"] == "2026-10-15T22:00:00+00:00"
        assert spec["time_zone"] == "America/Chicago"

    def test_an_aware_datetime_is_left_alone(self) -> None:
        window = TimeRange.custom(
            datetime(2026, 10, 15, 14, 0, tzinfo=UTC),
            datetime(2026, 10, 15, 22, 0, tzinfo=UTC),
            timezone="America/Chicago",
        )
        assert window.to_filter()["range"]["time"]["gte"] == "2026-10-15T14:00:00+00:00"

    def test_an_inverted_range_is_refused_rather_than_sent(self) -> None:
        """OpenSearch answers an inverted range with zero hits and no error, so the dashboard would
        read "nothing happened" instead of "the dates are backwards"."""
        with pytest.raises(TelemetryQueryError, match="ends before it starts"):
            TimeRange(datetime(2026, 10, 15, tzinfo=UTC), datetime(2026, 10, 14, tzinfo=UTC))

    def test_an_empty_range_is_refused_too(self) -> None:
        stamp = datetime(2026, 10, 15, tzinfo=UTC)
        with pytest.raises(TelemetryQueryError, match="ends before it starts"):
            TimeRange(stamp, stamp)

    def test_an_unknown_timezone_is_refused_at_construction(self) -> None:
        """A bad zone has to fail here, where the message can name the field, and not as a
        ZoneInfoNotFoundError thrown from inside a request builder."""
        with pytest.raises(TelemetryQueryError, match="unknown timezone"):
            TimeRange(
                datetime(2026, 10, 15, tzinfo=UTC),
                datetime(2026, 10, 16, tzinfo=UTC),
                "Mars/Olympus",
            )

    def test_an_empty_timezone_is_refused_with_its_own_reason(self) -> None:
        with pytest.raises(TelemetryQueryError, match="timezone is required"):
            zone("")

    def test_a_known_zone_resolves_without_a_lookup(self) -> None:
        assert str(zone("Europe/Istanbul")) == "Europe/Istanbul"

    @pytest.mark.parametrize(
        ("days", "expected"),
        [
            (0.5, "hour"),
            (2.0, "hour"),
            (2.1, "day"),
            (62.0, "day"),
            (63.0, "week"),
            (400.0, "week"),
            (401.0, "month"),
        ],
    )
    def test_the_bucket_width_follows_the_window(self, days: float, expected: str) -> None:
        assert _range(days).calendar_interval() == expected

    def test_the_range_filter_can_name_the_trace_time_field(self) -> None:
        """Trace documents carry `startTime` rather than `time`, and a query against the wrong one
        returns nothing."""
        assert "startTime" in _range().to_filter("startTime")["range"]


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TestFilters:
    @pytest.mark.parametrize(
        ("filters", "expected"),
        [
            (
                Filters(agents=("scheduling",)),
                {"terms": {"log.attributes.agent@role.keyword": ["scheduling"]}},
            ),
            (
                Filters(agent_versions=("1.4.0",)),
                {"terms": {"log.attributes.agent@version.keyword": ["1.4.0"]}},
            ),
            (
                Filters(releases=("rel-2026.10",)),
                {"terms": {"log.attributes.agent@release.keyword": ["rel-2026.10"]}},
            ),
            (
                Filters(models=("gemini-2.5-pro",)),
                {"terms": {"log.attributes.model@name.keyword": ["gemini-2.5-pro"]}},
            ),
            (
                Filters(event_names=("feedback",)),
                {"terms": {"log.attributes.event@name.keyword": ["feedback"]}},
            ),
            (
                Filters(workflow_states=("BOOKED",)),
                {"terms": {"log.attributes.workflow@state.keyword": ["BOOKED"]}},
            ),
            (
                Filters(outcomes=("error",)),
                {"terms": {"log.attributes.outcome@class.keyword": ["error"]}},
            ),
            (
                Filters(synthetic="synthetic"),
                {"term": {"log.attributes.synthetic.keyword": "synthetic"}},
            ),
            (
                Filters(deployment_annotation="rel-9"),
                {"term": {"log.attributes.deployment@annotation.keyword": "rel-9"}},
            ),
        ],
    )
    def test_each_filter_maps_to_the_field_that_exists(
        self, filters: Filters, expected: dict[str, Any]
    ) -> None:
        """Every field name here is one that a live document actually carries, which is the only
        thing that makes a filter clause more than a plausible-looking string."""
        assert expected in filters.clauses()

    def test_no_filter_is_no_clause(self) -> None:
        assert Filters().clauses() == []

    def test_an_outcome_is_normalised_through_the_enum(self) -> None:
        """The six classes are the closed vocabulary; a caller passing the enum member itself gets
        the same clause as one passing its value."""
        assert (
            Filters(outcomes=(OutcomeClass.SUCCESS_EMPTY,)).clauses()
            == Filters(outcomes=("success_empty",)).clauses()
        )

    def test_an_outcome_outside_the_six_is_refused(self) -> None:
        with pytest.raises(ValueError):
            Filters(outcomes=("failure",)).clauses()

    def test_a_string_dimension_uses_the_keyword_field(self) -> None:
        clauses = Filters(dimensions={"agent.role": "scheduling"}).clauses()
        assert clauses == [{"term": {"log.attributes.agent@role.keyword": "scheduling"}}]

    def test_a_misspelled_dimension_value_is_refused(self) -> None:
        """The vocabulary is closed, and "intake" is not one of the nine. Storing it would split a
        chart into a bucket that is the real ``referral_intake`` and a bucket that is a typo, and
        nobody comparing the two bars could tell which was which."""
        with pytest.raises(UnknownDimensionError, match="not a known value"):
            Filters(dimensions={"agent.role": "intake"}).clauses()

    def test_a_numeric_dimension_uses_the_numeric_field(self) -> None:
        clauses = Filters(dimensions={"attempt.number": 2}).clauses()
        assert clauses == [{"term": {"log.attributes.attempt@number": 2}}]

    def test_a_trace_id_dimension_is_refused_with_the_section_reason(self) -> None:
        """§15: the trace id is never a metric label. It is a *field* on the record, so a caller that
        wants one trace reads it, and a caller that groups by it gets one bucket per request — which
        answers nothing while looking like a chart."""
        with pytest.raises(UnknownDimensionError, match="not a dimension"):
            Filters(dimensions={"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"}).clauses()

    def test_an_unknown_dimension_is_refused(self) -> None:
        with pytest.raises(UnknownDimensionError, match="not a dimension"):
            Filters(dimensions={"patient.email": "a@b.test"}).clauses()

    def test_a_dimension_outside_its_vocabulary_is_refused(self) -> None:
        """A dimension is bounded by construction. An unbounded value is not a dimension, it is a
        filter that grows the index's field count."""
        with pytest.raises(UnknownDimensionError):
            Filters(dimensions={"outcome.class": "probably_fine"}).clauses()

    def test_the_dimension_list_is_the_conventions_list(self) -> None:
        assert event_dimensions() == event_dimensions()
        for forbidden in ("trace_id", "span_id", "event.id", "turn.id", "conversation_id"):
            assert forbidden not in event_dimensions()


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


class TestBodyAssembly:
    def test_filters_are_filter_clauses_and_never_must_clauses(self) -> None:
        """A scored clause makes the result relevance-ordered, and no aggregation here is a
        relevance question — the answer would drift with the analyzer."""
        body = TelemetryQuery.body(
            time_range=_range(), filters=Filters(agents=("scheduling",), event_names=("feedback",))
        )
        assert "must" not in body["query"]["bool"]
        assert len(body["query"]["bool"]["filter"]) == 3, "the range plus two filters"

    def test_the_range_is_the_first_clause(self) -> None:
        window = _range()
        body = TelemetryQuery.body(time_range=window, filters=Filters(outcomes=("error",)))
        assert body["query"]["bool"]["filter"][0] == window.to_filter()

    def test_hits_are_not_requested_by_default(self) -> None:
        """These are counting queries; asking for documents would move the whole index across the
        wire to compute a number."""
        body = TelemetryQuery.body(time_range=_range())
        assert body["size"] == 0
        assert body["track_total_hits"] is True

    def test_aggregations_appear_only_when_asked_for(self) -> None:
        assert "aggs" not in TelemetryQuery.body(time_range=_range())
        body = TelemetryQuery.body(time_range=_range(), aggs={"sample": {"x": 1}})
        assert body["aggs"] == {"sample": {"x": 1}}


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class TestSample:
    def test_duplicates_are_visible_rather_than_corrected(self) -> None:
        """`count` is cardinality(event.id) — the dedup rule — and `documents` is the raw hit count.
        When they differ the same event really was written twice, and a reader should see it."""
        sample = Sample(count=10, documents=12)
        assert sample.duplicated == 2
        assert sample.as_dict() == {
            "count": 10,
            "documents": 12,
            "duplicated": 2,
            "truncated": False,
            "other": 0,
        }

    def test_duplication_is_never_negative(self) -> None:
        """A cardinality aggregation without a precision threshold can estimate above the true count
        on a large index, and a negative "duplicates" number would be nonsense."""
        assert Sample(count=12, documents=10).duplicated == 0

    def test_truncation_is_carried(self) -> None:
        assert Sample(count=1, documents=1, truncated=True, other=200).as_dict()["other"] == 200


class TestPercentiles:
    def test_the_wire_form_carries_its_sample(self) -> None:
        """§12's "sample count on every chart" is enforced by the shape: there is no way to build one
        of these without a count."""
        payload = Percentiles(
            metric="latency.attempt.ms", p50=1.0, p95=2.0, p99=3.0, sample=Sample(4, 4)
        )
        assert payload.as_dict()["sample"]["count"] == 4
        assert set(payload.as_dict()) >= {"metric", "p50", "p95", "p99", "min", "max", "sample"}

    def test_a_bucket_carries_its_own_sample(self) -> None:
        assert Bucket(key="error", value=None, sample=Sample(2, 3)).as_dict() == {
            "key": "error",
            "value": None,
            "sample": {"count": 2, "documents": 3, "duplicated": 1, "truncated": False, "other": 0},
        }


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class TestTheDirectoryIndexIsRefused:
    """§15: "Directory and telemetry indices are separate"."""

    def test_the_directory_family_is_refused(self) -> None:
        """Retrieved directory text is untrusted content. A query that joined the two families would
        let a document that was *retrieved* be read as a document that was *recorded*."""
        with pytest.raises(UnindexablePatternError, match="not a telemetry index family"):
            TelemetryQuery("http://opensearch:9200", index=DIRECTORY_INDEX_PATTERN)

    def test_the_refusal_lists_what_is_allowed(self) -> None:
        with pytest.raises(UnindexablePatternError) as caught:
            TelemetryQuery("http://opensearch:9200", index="referraleasy-directory-*")
        assert "otel-events-*" in str(caught.value)
        assert "§15" in str(caught.value)

    @pytest.mark.parametrize("pattern", list(ALLOWED_QUERY_PATTERNS))
    def test_every_allowed_family_is_accepted(self, pattern: str) -> None:
        assert TelemetryQuery("http://opensearch:9200", index=pattern).index == pattern

    def test_the_evaluation_family_is_separately_addressable(self) -> None:
        """Evaluation lives 30 days and telemetry 7, so a query has to be able to name one of them
        without silently reading both."""
        assert TelemetryQuery("http://opensearch:9200", index=EVALUATION_INDEX_PATTERN)


class TestClientLifecycle:
    def test_an_injected_client_is_not_closed(self) -> None:
        """A caller that brought its own client owns it — usually a long-lived one shared across
        queries, which a `with` block must not tear down on the way out."""
        stub = Stub(_sample_response(distinct=1, documents=1))
        query = TelemetryQuery("http://opensearch:9200", client=stub.client)
        query.close()
        assert query.search({"size": 0})["hits"]["total"]["value"] == 1

    def test_a_client_it_created_is_closed(self) -> None:
        query = TelemetryQuery("http://opensearch:9200")
        client = query._http()
        query.close()
        assert client.is_closed

    def test_close_is_idempotent(self) -> None:
        query = TelemetryQuery("http://opensearch:9200")
        query.close()
        query.close()

    def test_the_context_manager_closes(self) -> None:
        with TelemetryQuery("http://opensearch:9200") as query:
            client = query._http()
        assert client.is_closed


# ---------------------------------------------------------------------------
# §15 measurements
# ---------------------------------------------------------------------------


class TestEventCount:
    def test_the_count_is_deduplicated_and_the_documents_are_not(self) -> None:
        stub = Stub(_sample_response(distinct=10, documents=12))
        sample = _query(stub).event_count(time_range=_range())
        assert (sample.count, sample.documents, sample.duplicated) == (10, 12, 2)

    def test_the_query_counts_event_ids(self) -> None:
        stub = Stub(_sample_response(distinct=1, documents=1))
        _query(stub).event_count(time_range=_range())
        # The path is itself the assertion: `aggs.sample` must be a *cardinality* aggregation. The
        # version that shipped had `aggs.sample.distinct_events.cardinality` here, which OpenSearch
        # answers with `400 Unknown aggregation type [distinct_events]` and never runs.
        agg = stub.bodies[0]["aggs"]["sample"]["cardinality"]
        assert agg["field"] == EVENT_ID_FIELD
        assert agg["precision_threshold"] > 3000, "the default is a small-index estimate"

    def test_a_metric_filter_narrows_without_replacing_the_callers_filters(self) -> None:
        stub = Stub(_sample_response(distinct=1, documents=1))
        _query(stub).event_count(
            time_range=_range(),
            filters=Filters(agents=("scheduling",)),
            metric=MetricName.LATENCY_TURN_MS,
        )
        assert {"terms": {"log.attributes.agent@role.keyword": ["scheduling"]}} in stub.last_clauses
        assert {
            "term": {"log.attributes.metric@name.keyword": "latency.turn.ms"}
        } in stub.last_clauses


class TestOutcomeBreakdown:
    """§15: "A valid empty result is not an error"."""

    def _response(self) -> dict[str, Any]:
        return {
            "aggregations": {
                "outcomes": {
                    "buckets": [
                        {"key": "success_full", "doc_count": 5, "sample": {"value": 5}},
                        {"key": "success_empty", "doc_count": 1, "sample": {"value": 1}},
                        {"key": "error", "doc_count": 2, "sample": {"value": 2}},
                    ]
                }
            }
        }

    def test_every_class_is_present_even_at_zero(self) -> None:
        """A missing key renders as a missing bar, which reads as "this never happens" without
        saying so."""
        breakdown = _query(Stub(self._response())).outcome_breakdown(time_range=_range())
        assert set(breakdown) == {str(cls) for cls in OutcomeClass}
        assert breakdown["blocked"] == Sample(0, 0)
        assert breakdown["fallback"] == Sample(0, 0)

    def test_an_empty_result_is_counted_on_its_own_and_not_as_a_failure(self) -> None:
        breakdown = _query(Stub(self._response())).outcome_breakdown(time_range=_range())
        assert breakdown["success_empty"].count == 1
        assert breakdown["error"].count == 2, "the empty result is not folded into the failures"

    def test_the_breakdown_groups_on_the_keyword_field(self) -> None:
        stub = Stub(self._response())
        _query(stub).outcome_breakdown(time_range=_range())
        assert (
            stub.bodies[0]["aggs"]["outcomes"]["terms"]["field"]
            == "log.attributes.outcome@class.keyword"
        )

    def test_each_bucket_carries_a_well_formed_sample_sub_aggregation(self) -> None:
        """The request path, not the response: ``aggs.outcomes.aggs.sample.cardinality``.

        A missing assertion here is why two 400s survived a green suite. ``_sample_agg`` was called
        two ways in this file — ``{"sample": _sample_agg()}`` and ``sub=_sample_agg()`` — and only
        one of them could be well formed at a time, because the helper returned the *body* of a
        named aggregation. The unwrapped call produced ``{"aggs": {"cardinality": {...}}}``, which
        OpenSearch reads as a sub-aggregation **named** ``cardinality`` and rejects:
        ``Expected [START_OBJECT] under [field], but got a [VALUE_STRING] in [cardinality]``.

        Asserting the path in both places is what makes the two conventions visible as one.
        """
        stub = Stub(self._response())
        _query(stub).outcome_breakdown(time_range=_range())
        sub = stub.bodies[0]["aggs"]["outcomes"]["aggs"]
        assert set(sub) == {"sample"}, "a named sub-aggregation, not a bare agg body"
        assert sub["sample"]["cardinality"]["field"] == EVENT_ID_FIELD


class TestApiFailures:
    def test_each_bucket_is_an_endpoint_and_a_status(self) -> None:
        stub = Stub(
            {
                "aggregations": {
                    "by_endpoint": {
                        "buckets": [
                            {
                                "key": "mock-api:/appointments",
                                "doc_count": 3,
                                "by_status": {
                                    "buckets": [
                                        {
                                            "key": "503",
                                            "doc_count": 3,
                                            "sample": {"value": 3},
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                }
            }
        )
        buckets = _query(stub).api_failures(time_range=_range())
        assert buckets[0].key == "mock-api:/appointments 503"
        assert buckets[0].value == 503.0
        assert buckets[0].sample.count == 3

    def test_the_status_is_a_numeric_field(self) -> None:
        stub = Stub({"aggregations": {"by_endpoint": {"buckets": []}}})
        _query(stub).api_failures(time_range=_range())
        status_agg = stub.bodies[0]["aggs"]["by_endpoint"]["aggs"]["by_status"]["terms"]
        assert status_agg["field"] == "log.attributes.api@status_code"

    def test_the_scope_is_the_api_failure_event(self) -> None:
        stub = Stub({"aggregations": {"by_endpoint": {"buckets": []}}})
        _query(stub).api_failures(time_range=_range())
        assert {"term": {"log.attributes.event@name.keyword": "api_failure"}} in stub.last_clauses


class TestLatency:
    def test_timeouts_are_in_the_distribution_by_default(self) -> None:
        """§15: "In particular, timeouts are not removed from the latency distribution". A latency
        chart that drops the
        slow requests is the chart that hides the outage, and the default has to be the honest one."""
        stub = Stub(
            {
                "hits": {"total": {"value": 9}},
                "aggregations": {
                    "latency": {
                        "values": {
                            "0.0": 40.0,
                            "50.0": 120.0,
                            "95.0": 300.0,
                            "99.0": 500.0,
                            "100.0": 900.0,
                        }
                    },
                    "sample": {"value": 9},
                },
            }
        )
        result = _query(stub).latency(time_range=_range())
        assert not any("outcome@class" in json.dumps(clause) for clause in stub.last_clauses)
        assert (result.p50, result.p95, result.p99) == (120.0, 300.0, 500.0)
        assert result.min_ms == 40.0 and result.max_ms == 900.0
        assert result.sample.count == 9

    def test_the_bounds_are_requested_as_the_zeroth_and_hundredth_percentile(self) -> None:
        """**The regression test for a live measurement.** A ``percentiles`` aggregation returns only
        the percents it was asked for — measured on the running node, with 23 documents in range the
        response held ``{"50.0", "95.0", "99.0"}`` and no ``min``/``max`` keys at all. So asking for
        min/max by name returned ``None`` on every query, forever, and the chart said "no data" next
        to a valid p95."""
        stub = Stub({"hits": {"total": {"value": 0}}, "aggregations": {"latency": {"values": {}}}})
        _query(stub).latency(time_range=_range())
        percents = stub.bodies[0]["aggs"]["latency"]["percentiles"]["percents"]
        assert percents == [0, 50, 95, 99, 100]

    def test_a_response_that_does_not_carry_the_bounds_reads_as_no_data(self) -> None:
        """The trap itself, asserted: names that look like they should be in the response are not."""
        stub = Stub(
            {
                "hits": {"total": {"value": 3}},
                "aggregations": {
                    "latency": {"values": {"50.0": 10.0, "min": 1.0, "max": 99.0}},
                    "sample": {"value": 3},
                },
            }
        )
        result = _query(stub).latency(time_range=_range())
        assert result.p50 == 10.0
        assert result.min_ms is None, "a `min` key is not what OpenSearch sends"

    def test_a_success_only_view_has_to_be_asked_for_by_name(self) -> None:
        """The flag exists so a caller can ask for the rosier number while reading the call site
        says what it is doing."""
        stub = Stub({"hits": {"total": {"value": 0}}, "aggregations": {"latency": {"values": {}}}})
        _query(stub).latency(time_range=_range(), include_errors=False)
        assert {
            "terms": {"log.attributes.outcome@class.keyword": ["success_full", "success_partial"]}
        } in stub.last_clauses

    def test_the_three_distributions_are_separate_metrics(self) -> None:
        for kind, metric in (
            ("attempt", "latency.attempt.ms"),
            ("invocation", "latency.invocation.ms"),
            ("turn", "latency.turn.ms"),
        ):
            stub = Stub(
                {"hits": {"total": {"value": 0}}, "aggregations": {"latency": {"values": {}}}}
            )
            result = _query(stub).latency(time_range=_range(), kind=kind)
            assert result.metric == metric
            assert {"term": {"log.attributes.metric@name.keyword": metric}} in stub.last_clauses

    def test_an_unknown_kind_is_a_key_error_rather_than_a_silent_default(self) -> None:
        with pytest.raises(KeyError):
            _query(Stub({})).latency(time_range=_range(), kind="round-trip")

    @pytest.mark.parametrize("value", ["Infinity", "-Infinity", "NaN"])
    def test_an_unbounded_percentile_is_no_data_and_not_a_number(self, value: str) -> None:
        """OpenSearch reports a percentile over an empty or single-value distribution as ±Infinity
        or NaN. Charting Infinity as a latency is worse than charting nothing."""
        stub = Stub(
            {
                "hits": {"total": {"value": 0}},
                "aggregations": {
                    "latency": {"values": {"50.0": value, "95.0": value, "99.0": value}}
                },
            }
        )
        result = _query(stub).latency(time_range=_range())
        assert (result.p50, result.p95, result.p99) == (None, None, None)

    def test_no_data_is_none_and_not_zero_milliseconds(self) -> None:
        """Zero would plot as an impossibly fast system — the fastest possible number for the worst
        possible reason."""
        stub = Stub({"hits": {"total": {"value": 0}}, "aggregations": {"latency": {"values": {}}}})
        result = _query(stub).latency(time_range=_range())
        assert result.p50 is None and result.sample.count == 0


class TestTimeSeries:
    def _response(self) -> dict[str, Any]:
        return {
            "hits": {"total": {"value": 6}},
            "aggregations": {
                "series": {
                    "buckets": [
                        {
                            "key_as_string": "2026-10-14T00:00:00.000-05:00",
                            "key": 1760427600000,
                            "doc_count": 3,
                            "sample": {"value": 3},
                        },
                        {
                            "key_as_string": "2026-10-14T01:00:00.000-05:00",
                            "doc_count": 2,
                            "sample": {"value": 2},
                        },
                        {
                            "key": "not-a-timestamp",
                            "doc_count": 1,
                            "sample": {"value": 1},
                        },
                    ]
                },
                "sample": {"value": 5},
            },
        }

    def test_the_buckets_are_local_and_each_carries_its_sample(self) -> None:
        series = _query(Stub(self._response())).time_series(time_range=_range(days=0.5))
        assert isinstance(series, TimeSeries)
        assert series.interval == "hour"
        assert series.timezone == "UTC"
        assert len(series.points) == 2, "the unparseable key is dropped rather than invented"
        # The bucket keeps the offset OpenSearch labelled it with — 00:00 in the requested zone,
        # which is 05:00 UTC — rather than being rewritten into the caller's own zone.
        assert series.points[0][0].isoformat() == "2026-10-14T00:00:00-05:00"
        assert series.points[0][0].astimezone(UTC).hour == 5
        assert series.points[0][1].count == 3
        assert series.sample.count == 5

    def test_the_histogram_uses_the_requested_zone(self) -> None:
        """A daily chart whose buckets start at an arbitrary hour is a chart nobody can read."""
        stub = Stub(self._response())
        _query(stub).time_series(time_range=_range(days=10, timezone="America/Chicago"))
        histogram = stub.bodies[0]["aggs"]["series"]["date_histogram"]
        assert histogram["time_zone"] == "America/Chicago"
        assert histogram["calendar_interval"] == "day"
        assert histogram["min_doc_count"] == 0, "an empty hour is a real reading, not a gap"


class TestCostAndHumanWork:
    def test_model_usage_is_three_distributions_with_three_sample_counts(self) -> None:
        stub = Stub(
            {
                "hits": {"total": {"value": 4}},
                "aggregations": {
                    "m": {"values": {"50.0": 1.0, "95.0": 2.0}},
                    "sample": {"value": 4},
                },
            }
        )
        usage = _query(stub).model_usage(time_range=_range())
        assert set(usage) == {"model.tokens.input", "model.tokens.output", "model.cost.usd"}
        assert len(stub.bodies) == 3, "one query per metric, so one sample count per metric"
        for payload in usage.values():
            assert payload.sample.count == 4
            assert payload.p99 is None, "not asked for, so not invented"

    def test_the_three_model_queries_are_distinct(self) -> None:
        stub = Stub(
            {
                "hits": {"total": {"value": 0}},
                "aggregations": {"m": {"values": {}}, "sample": {"value": 0}},
            }
        )
        _query(stub).model_usage(time_range=_range())
        scopes = {
            clause["term"]["log.attributes.metric@name.keyword"]
            for body in stub.bodies
            for clause in body["query"]["bool"]["filter"]
            if "term" in clause and "log.attributes.metric@name.keyword" in clause["term"]
        }
        assert scopes == {"model.tokens.input", "model.tokens.output", "model.cost.usd"}

    def test_hitl_waiting_and_resolution_stay_two_distributions(self) -> None:
        """They answer different questions — how long a human took to arrive, and how long the ticket
        took to close — and one number for "HITL time" would be neither."""
        stub = Stub(
            {
                "hits": {"total": {"value": 2}},
                "aggregations": {
                    "m": {"values": {"50.0": 5.0, "95.0": 9.0}},
                    "sample": {"value": 2},
                },
            }
        )
        hitl = _query(stub).hitl(time_range=_range())
        assert set(hitl) == {"hitl.wait.ms", "hitl.resolve.ms"}
        assert hitl["hitl.wait.ms"].p50 == 5.0


class TestGuardrailDecisions:
    def test_the_breakdown_is_by_decision_and_carries_a_sample(self) -> None:
        stub = Stub(
            {
                "aggregations": {
                    "by_decision": {
                        "buckets": [
                            {"key": "allow", "doc_count": 9, "sample": {"value": 9}},
                            {"key": "block", "doc_count": 1, "sample": {"value": 1}},
                        ]
                    }
                }
            }
        )
        buckets = _query(stub).guardrail_decisions(time_range=_range())
        assert [(bucket.key, bucket.sample.count) for bucket in buckets] == [
            ("allow", 9),
            ("block", 1),
        ]
        assert (
            stub.bodies[0]["aggs"]["by_decision"]["terms"]["field"]
            == "log.attributes.guardrail@decision.keyword"
        )


class TestEvaluationScores:
    def _response(self) -> dict[str, Any]:
        def status(key: str, *, distinct: int, score: Any) -> dict[str, Any]:
            return {
                "key": key,
                "doc_count": distinct,
                "sample": {"value": distinct},
                "score": {"values": {"50.0": score}},
            }

        return {
            "aggregations": {
                "by_metric": {
                    "buckets": [
                        {
                            "key": "correctness",
                            "by_judge": {
                                "buckets": [
                                    {
                                        "key": "judge-a",
                                        "by_rubric": {
                                            "buckets": [
                                                {
                                                    "key": "rubric-3",
                                                    "by_status": {
                                                        "buckets": [
                                                            status(
                                                                "scored", distinct=4, score=0.85
                                                            ),
                                                            status(
                                                                "not_applicable",
                                                                distinct=2,
                                                                score=0.0,
                                                            ),
                                                            status(
                                                                "eval_error", distinct=1, score=0.0
                                                            ),
                                                        ]
                                                    },
                                                }
                                            ]
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        }

    def test_the_bucket_key_names_the_judge_and_the_rubric(self) -> None:
        """§14 forbids merging different judges and rubrics into one series, and an average across
        them is that merge with the seams hidden."""
        buckets = _query(Stub(self._response())).evaluation_scores(time_range=_range())
        assert {bucket.key for bucket in buckets} == {
            "correctness|judge-a|rubric-3|scored",
            "correctness|judge-a|rubric-3|not_applicable",
            "correctness|judge-a|rubric-3|eval_error",
        }

    def test_only_a_scored_status_carries_a_number(self) -> None:
        """§14: N/A and an evaluation error are not a zero. The stored value is 0.0 so it cannot leak
        into an average through a missing field, and the query layer renders them as no data."""
        buckets = {
            bucket.key: bucket
            for bucket in _query(Stub(self._response())).evaluation_scores(time_range=_range())
        }
        assert buckets["correctness|judge-a|rubric-3|scored"].value == 0.85
        assert buckets["correctness|judge-a|rubric-3|not_applicable"].value is None
        assert buckets["correctness|judge-a|rubric-3|eval_error"].value is None

    def test_every_status_bucket_carries_its_own_sample(self) -> None:
        buckets = {
            bucket.key: bucket
            for bucket in _query(Stub(self._response())).evaluation_scores(time_range=_range())
        }
        assert buckets["correctness|judge-a|rubric-3|scored"].sample.count == 4
        assert buckets["correctness|judge-a|rubric-3|not_applicable"].sample.count == 2

    def test_the_evaluation_query_reads_the_event_stream_by_default(self) -> None:
        """`eval_score` is a §15 event and the Data Prepper event pipeline writes those into
        `otel-events-*`. Defaulting this to `referraleasy-evaluation-*` sent the admin Evaluations
        page to a family that holds a different data class — and so answered zero buckets while 44
        scores sat in the event stream."""
        stub = Stub({"aggregations": {"by_metric": {"buckets": []}}})
        _query(stub).evaluation_scores(time_range=_range())
        assert stub.paths[0].startswith("/otel-events-*")

    def test_the_evaluation_query_honours_an_explicit_index(self) -> None:
        """The parameter names the stream to read, and a named one is used rather than the default.
        The evaluation family is an allowed index, so it is a legal value here — it is simply not
        where `eval_score` events are, which is why it is not the default."""
        stub = Stub({"aggregations": {"by_metric": {"buckets": []}}})
        _query(stub).evaluation_scores(time_range=_range(), index=EVALUATION_INDEX_PATTERN)
        assert stub.paths[0].startswith("/referraleasy-evaluation-*")

    def test_a_per_call_index_is_refused_when_it_is_not_a_telemetry_family(self) -> None:
        """The allow-list has to hold on the override path too. `search(index=...)` reaches the same
        URL builder as the constructor, and the family it would otherwise let through is the one
        §15 separates because its contents are retrieved text, not recorded telemetry."""
        with pytest.raises(UnindexablePatternError):
            _query(Stub({})).search({}, index=DIRECTORY_INDEX_PATTERN)


class TestFeedbackQuery:
    """The three numbers, computed by the search rather than after it (see ``feedback.py``)."""

    def _response(self, *, other: int = 0) -> dict[str, Any]:
        def vote(response_id: str, value: str, at: str) -> dict[str, Any]:
            return {
                "key": response_id,
                "doc_count": 1,
                "latest": {
                    "hits": {
                        "hits": [
                            {
                                "_source": {
                                    FEEDBACK_VALUE_SOURCE: value,
                                    FEEDBACK_RESPONSE_ID_SOURCE: response_id,
                                    "time": at,
                                }
                            }
                        ]
                    }
                },
            }

        return {
            "aggregations": {
                "by_response": {
                    "sum_other_doc_count": other,
                    "buckets": [
                        vote("resp-1", "up", "2026-10-14T10:00:05Z"),
                        vote("resp-2", "down", "2026-10-14T10:00:06Z"),
                    ],
                }
            }
        }

    def test_the_three_numbers_are_three_numbers(self) -> None:
        summary = _query(Stub(self._response())).feedback(time_range=_range(), total_responses=8)
        assert summary.raw_count == 2
        assert summary.like_rate == 0.5
        assert summary.participation_rate == 0.25

    def test_it_groups_on_the_keyword_field(self) -> None:
        stub = Stub(self._response())
        _query(stub).feedback(time_range=_range(), total_responses=2)
        assert stub.bodies[0]["aggs"]["by_response"]["terms"]["field"] == FEEDBACK_RESPONSE_ID_FIELD

    def test_the_hits_are_read_from_source_paths_and_not_from_multi_fields(self) -> None:
        """**The regression test for a live bug.** The aggregation needs ``.keyword`` and the
        ``_source`` filter must not have it: source filtering does not follow a multi-field, so
        asking for the keyword name returns hits with that path simply absent. Measured on the
        running stack — the votes were in the index and this method read zero."""
        stub = Stub(self._response())
        _query(stub).feedback(time_range=_range(), total_responses=2)
        top_hits = stub.bodies[0]["aggs"]["by_response"]["aggs"]["latest"]["top_hits"]
        assert set(top_hits["_source"]) == {
            FEEDBACK_VALUE_SOURCE,
            FEEDBACK_RESPONSE_ID_SOURCE,
            "time",
        }
        assert not any(name.endswith(".keyword") for name in top_hits["_source"])

    def test_the_last_active_vote_is_chosen_by_the_search(self) -> None:
        """`top_hits` size 1 sorted newest-first is what makes "last active" a property of the query.
        The alternative — fetching every vote and collapsing in Python — is the same answer computed
        after moving every document over the network, and it stops working at month scale."""
        stub = Stub(self._response())
        _query(stub).feedback(time_range=_range(), total_responses=2)
        top_hits = stub.bodies[0]["aggs"]["by_response"]["aggs"]["latest"]["top_hits"]
        assert top_hits["size"] == 1
        assert top_hits["sort"][0] == {"time": {"order": "desc"}}
        assert top_hits["sort"][1] == {"_doc": {"order": "desc"}}, (
            "time ties at millisecond resolution; insertion order decides the tie"
        )

    def test_the_scope_is_the_feedback_event(self) -> None:
        stub = Stub(self._response())
        _query(stub).feedback(time_range=_range(), total_responses=2)
        assert {"term": {"log.attributes.event@name.keyword": "feedback"}} in stub.last_clauses

    def test_the_terms_overflow_is_reported_rather_than_hidden(self) -> None:
        """When more responses were rated than the terms aggregation returned, the like rate is
        computed over a subset. A chart with a caveat beats a chart that is quietly wrong."""
        summary = _query(Stub(self._response(other=200))).feedback(
            time_range=_range(), total_responses=500
        )
        assert summary.truncated is True
        assert summary.other_responses == 200

    def test_a_bucket_with_no_hits_is_skipped(self) -> None:
        stub = Stub(
            {"aggregations": {"by_response": {"buckets": [{"key": "resp-1", "doc_count": 0}]}}}
        )
        summary = _query(stub).feedback(time_range=_range(), total_responses=1)
        assert summary.raw_count == 0 and summary.like_rate is None

    def test_no_votes_at_all_is_no_data_rather_than_a_zero_rate(self) -> None:
        stub = Stub({"aggregations": {"by_response": {"buckets": []}}})
        summary = _query(stub).feedback(time_range=_range(), total_responses=300)
        assert summary.like_rate is None
        assert summary.participation_rate == 0.0


class TestIndexHealth:
    def test_it_lists_the_indices_and_the_retention_promise(self) -> None:
        stub = Stub(
            [
                {"index": "otel-events-2026.10.15", "docs.count": "11"},
                {"index": "otel-events-2026.10.14", "docs.count": "1"},
            ]
        )
        report = _query(stub).index_health()
        assert report["pattern"] == EVENT_INDEX_PATTERN
        assert [row["index"] for row in report["indices"]] == [
            "otel-events-2026.10.14",
            "otel-events-2026.10.15",
        ]
        assert report["indices"][1]["docs"] == 11
        assert report["retention_days"] == TELEMETRY_RETENTION_DAYS == 7

    def test_it_asks_the_cat_api(self) -> None:
        stub = Stub([])
        _query(stub).index_health()
        assert stub.paths[0] == "/_cat/indices/otel-events-*"


class TestThePathologyWasTheWholePoint:
    """Two rules that the shape of this module enforces, kept together because they are the same
    rule: a number is never presented without saying what it was computed from, and a value that is
    absent is never presented as a value."""

    def test_every_aggregating_method_returns_a_sample(self) -> None:
        def samples_of(result: Any) -> list[Any]:
            if isinstance(result, Sample):
                return [result]
            if isinstance(result, TimeSeries):
                return [result.sample, *(sample for _, sample in result.points)]
            if isinstance(result, list):
                return [bucket.sample for bucket in result]
            return [
                value.sample if isinstance(value, Percentiles) else value
                for value in result.values()
            ]

        for method in ("event_count", "outcome_breakdown", "time_series", "model_usage", "hitl"):
            stub = Stub(_sample_response(distinct=1, documents=1))
            result = getattr(_query(stub), method)(time_range=_range())
            samples = samples_of(result)
            assert samples, method
            assert all(isinstance(sample, Sample) for sample in samples), method

    def test_the_metric_units_are_enumerable_for_a_form(self) -> None:
        """§15's list of measurements, so an admin page can offer them rather than hard-coding a
        second copy that drifts."""
        assert set(METRIC_UNITS_BY_NAME) == {str(metric) for metric in MetricName}
        assert set(METRIC_UNITS_BY_NAME.values()) == {"count", "ms", "usd", "score", "tokens"}


# ---------------------------------------------------------------------------
# §12's traces and dimension values
# ---------------------------------------------------------------------------
#
# Added for the admin read API (ADR-0039). Both methods query a *different* index family from every
# other method here, so the failure this section is written against is a field name that belongs to
# one family and is used against the other — which returns an empty result rather than an error, and
# was the shape of all three defects named in this file's docstring.


class TestDimensionValues:
    """The aggregation that fills a filter list, and refuses to be pointed at an id."""

    def test_the_terms_aggregation_uses_the_keyword_sub_field(self) -> None:
        stub = Stub(
            _sample_response(
                distinct=0,
                documents=0,
                extra={
                    "values": {
                        "buckets": [{"key": "scheduling", "doc_count": 4, "sample": {"value": 4}}],
                        "sum_other_doc_count": 0,
                    }
                },
            )
        )
        buckets = _query(stub).dimension_values(
            "agent.role", time_range=_range(), filters=Filters()
        )

        assert buckets[0].key == "scheduling"
        assert buckets[0].value == 4.0
        assert buckets[0].sample.count == 4
        # A terms agg on the `text` field would bucket by token; this asserts the sub-field.
        assert stub.bodies[-1]["aggs"]["values"]["terms"]["field"] == keyword_field("agent.role")

    def test_truncation_is_reported_rather_than_hidden(self) -> None:
        """A filter list that silently omits values is one an operator cannot tell from a full one."""
        stub = Stub(
            _sample_response(
                distinct=0,
                documents=0,
                extra={
                    "values": {
                        "buckets": [{"key": "triage", "doc_count": 2, "sample": {"value": 2}}],
                        "sum_other_doc_count": 17,
                    }
                },
            )
        )
        buckets = _query(stub).dimension_values("agent.role", time_range=_range())

        assert buckets[0].sample.other == 17

    def test_a_value_that_is_not_a_dimension_is_refused(self) -> None:
        """`trace_id` is not a dimension, and §15 says so rather than leaving it to the caller."""
        stub = Stub(_sample_response(distinct=0, documents=0))
        with pytest.raises(UnknownDimensionError):
            _query(stub).dimension_values("trace_id", time_range=_range())


class TestTraces:
    """The trace family, whose fields are neither `event.id` nor the attribute namespace."""

    def _trace_response(self) -> dict[str, Any]:
        return {
            "hits": {"total": {"value": 6, "relation": "eq"}},
            "aggregations": {
                "traces": {
                    "buckets": [
                        {
                            "key": "4bf92f3577b34da6a3ce929d0e0e4736",
                            "doc_count": 6,
                            "first": {"value": 1_763_000_000_000},
                            "slowest": {"value": 250_000_000},
                            "spans": {"value": 6},
                            "entry": {
                                "hits": {
                                    "hits": [
                                        {
                                            "_source": {
                                                "serviceName": "gateway",
                                                "name": "POST /v1/workflows",
                                            }
                                        }
                                    ]
                                }
                            },
                        }
                    ],
                    "sum_other_doc_count": 0,
                }
            },
        }

    def test_recent_traces_group_by_trace_id_and_order_by_the_first_span(self) -> None:
        stub = Stub(self._trace_response())
        summaries = _query(stub, index="otel-traces-*").recent_traces(time_range=_range())

        assert summaries[0].trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert summaries[0].service_name == "gateway"
        assert summaries[0].span_count == 6
        # Nanoseconds in the index, milliseconds out — the conversion lives in one place.
        assert summaries[0].duration_ms == 250.0
        terms = stub.bodies[-1]["aggs"]["traces"]["terms"]
        assert terms["field"] == "traceId"
        # Sorted by the sub-aggregation, not by the page after it was returned.
        assert terms["order"] == {"first": "desc"}

    def test_the_time_filter_uses_the_trace_time_field(self) -> None:
        """`time` is the event family's; a trace document carries `startTime`."""
        stub = Stub(self._trace_response())
        _query(stub, index="otel-traces-*").recent_traces(time_range=_range())

        clause = stub.last_clauses[0]["range"]
        assert "startTime" in clause

    def test_the_synthetic_label_is_a_boolean_term_on_the_span(self) -> None:
        """§15 stores it as a mapped field on a span and under the attribute namespace on an event."""
        stub = Stub(self._trace_response())
        _query(stub, index="otel-traces-*").recent_traces(time_range=_range(), synthetic=True)
        assert {"term": {"synthetic": True}} in stub.last_clauses

        stub = Stub(self._trace_response())
        _query(stub, index="otel-traces-*").recent_traces(time_range=_range())
        assert not any("synthetic" in json.dumps(clause) for clause in stub.last_clauses)

    def test_trace_spans_are_not_windowed(self) -> None:
        """A trace is identified exactly; a window would cut off its root span."""
        stub = Stub(
            {
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "traceId": "abc",
                                "spanId": "s1",
                                "name": "root",
                                "serviceName": "gateway",
                                "kind": "SERVER",
                                "startTime": "2026-10-14T00:00:00Z",
                                "durationInNanos": 1_500_000,
                                "synthetic": False,
                                "http.status_code": 200,
                            }
                        }
                    ]
                }
            }
        )
        spans = _query(stub, index="otel-traces-*").trace_spans("abc")

        assert spans[0].span_id == "s1"
        assert spans[0].parent_span_id is None
        assert spans[0].duration_ms == 1.5
        # The mapped fields are named fields on the record; everything else is an attribute.
        assert spans[0].attributes == {"http.status_code": 200}
        # No range clause at all — the query is by id.
        assert "range" not in json.dumps(stub.bodies[-1]["query"])

    def test_a_span_with_no_duration_is_null_rather_than_zero(self) -> None:
        """A span that reported no duration is not a span that took no time."""
        stub = Stub(
            {
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "traceId": "abc",
                                "spanId": "s2",
                                "name": "child",
                                "serviceName": "scheduling",
                            }
                        }
                    ]
                }
            }
        )
        spans = _query(stub, index="otel-traces-*").trace_spans("abc")

        assert spans[0].duration_ms is None
        assert spans[0].started_at is None
