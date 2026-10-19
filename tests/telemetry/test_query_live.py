# ruff: noqa: RUF002  # Turkish quoted from §15 of Referraleasy_Architecture_v3.md is kept verbatim
"""The query layer, run against the live index rather than against canned responses (§15).

**This is the stronger of the two verifications of :class:`TelemetryQuery`.** ``test_query.py``
hands the client a ``MockTransport`` and asserts on the request bodies and on the numbers it then
reads back out of a fabricated response; that proves the shapes and the arithmetic, and it proves
nothing about whether the *fields those queries name* exist in the index Data Prepper actually
writes. Three defects were found here and none of them by the mocked tests:

* ``feedback()`` asked ``_source`` for ``...feedback@value.keyword``. Source filtering does not
  follow a multi-field, and OpenSearch answers ``200`` with the path merely absent — so a populated
  index returned ``raw_count: 0``, a plausible answer that was wrong.
* ``latency()`` read ``values["min"]``/``values["max"]``. The ``percentiles`` aggregation returns
  only the percents it was asked for, so both were ``None`` next to a valid p95.
* a ``term`` query against a dynamically mapped string attribute matches nothing and says nothing,
  which is why every clause here goes through ``keyword_field()``.

The tests emit through the real pipeline with the ``live_pipeline`` fixture's marker and then read
with a real :class:`TelemetryQuery`, scoped to that marker. A number asserted here came out of
OpenSearch.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from referraleasy_telemetry.conventions import (
    EVENT_INDEX_PATTERN,
    TELEMETRY_RETENTION_DAYS,
    OutcomeClass,
    Synthetic,
)
from referraleasy_telemetry.query import (
    Filters,
    TelemetryQuery,
    TimeRange,
    UnindexablePatternError,
    keyword_field,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def live_query(live_pipeline) -> Iterator[TelemetryQuery]:
    """A real ``TelemetryQuery`` against the running OpenSearch, on the shared event index."""
    query = TelemetryQuery(live_pipeline.opensearch, index=EVENT_INDEX_PATTERN)
    try:
        yield query
    finally:
        query.close()


def recent_window(hours: float = 2.0) -> TimeRange:
    """A window that contains "now" and reaches past it.

    Wide on purpose: the assertion being made is about which documents match, and a window that
    ended at the instant the first document was written would make a correct pipeline look broken at
    the boundary. The end is in the future, which is also what a dashboard's "today" range does.
    """
    now = datetime.now(UTC)
    return TimeRange.custom(now - timedelta(hours=hours), now + timedelta(hours=1), timezone="UTC")


@pytest.fixture
def scoped(live_pipeline) -> Filters:
    """The §12 filter set pinned to this test's own writes."""
    return Filters(deployment_annotation=live_pipeline.marker)


class TestTheCounts:
    def test_the_count_deduplicates_a_delivery_that_arrived_twice(
        self, live_pipeline, live_query, scoped
    ) -> None:
        """``cardinality(event.id)`` over a live index, both numbers asserted.

        Two identical emissions produce two documents and one event. The query layer reports both —
        ``count`` is the deduplicated number a chart shows, ``documents`` is what is stored, and the
        difference is surfaced as ``duplicated`` rather than hidden, because two documents for one
        event is a fact about the pipeline that an operator may want to see.
        """
        for _ in range(2):
            live_pipeline.emitter.state_transition(
                from_state="INTAKE", to_state="BOOKED", synthetic=Synthetic.SYNTHETIC
            )
        live_pipeline.flush()
        live_pipeline.documents("state_transition", count=2)

        sample = live_query.event_count(
            time_range=recent_window(),
            filters=Filters(
                deployment_annotation=live_pipeline.marker,
                event_names=("state_transition",),
            ),
        )
        assert sample.count == 1
        assert sample.documents == 2
        assert sample.duplicated == 1

    def test_two_different_transitions_count_as_two(self, live_pipeline, live_query) -> None:
        """The mirror, so the test above cannot pass by counting nothing at all."""
        live_pipeline.emitter.state_transition(
            from_state="INTAKE", to_state="BOOKED", synthetic=Synthetic.SYNTHETIC
        )
        live_pipeline.emitter.state_transition(
            from_state="BOOKED", to_state="CANCELLED", synthetic=Synthetic.SYNTHETIC
        )
        live_pipeline.flush()
        live_pipeline.documents("state_transition", count=2)

        sample = live_query.event_count(
            time_range=recent_window(),
            filters=Filters(
                deployment_annotation=live_pipeline.marker,
                event_names=("state_transition",),
            ),
        )
        assert sample.count == 2
        assert sample.duplicated == 0

    def test_the_synthetic_filter_separates_fixture_traffic_from_demo_traffic(
        self, live_pipeline, live_query
    ) -> None:
        """§12's synthetic/live-demo filter, asked of the index rather than of a stub.

        One record is emitted under each label with **no other difference**, so the only thing that
        can move the count is the filter. The unfiltered read has to see both, which is the half that
        catches a filter that silently matches nothing.
        """
        with live_pipeline.span():
            live_pipeline.emitter.agent_trigger(
                agent_role="scheduling", synthetic=Synthetic.SYNTHETIC
            )
        with live_pipeline.span():
            live_pipeline.emitter.agent_trigger(
                agent_role="scheduling", synthetic=Synthetic.LIVE_DEMO
            )
        live_pipeline.flush()
        live_pipeline.documents("agent_trigger", count=2)

        mark = live_pipeline.marker
        both = live_query.event_count(
            time_range=recent_window(),
            filters=Filters(deployment_annotation=mark, event_names=("agent_trigger",)),
        )
        only_synthetic = live_query.event_count(
            time_range=recent_window(),
            filters=Filters(
                deployment_annotation=mark,
                event_names=("agent_trigger",),
                synthetic=Synthetic.SYNTHETIC,
            ),
        )
        only_live = live_query.event_count(
            time_range=recent_window(),
            filters=Filters(
                deployment_annotation=mark,
                event_names=("agent_trigger",),
                synthetic=Synthetic.LIVE_DEMO,
            ),
        )
        assert (both.count, only_synthetic.count, only_live.count) == (2, 1, 1)


class TestTheOutcomeBreakdown:
    def test_all_six_classes_are_present_and_empty_is_not_an_error(
        self, live_pipeline, live_query
    ) -> None:
        """§15: "Empty geçerli sonuç error değildir".

        Two attempts of one invocation are emitted — the first came back empty, the retry succeeded —
        and each carries its own attempt *number*. That detail is load-bearing: ``event.id`` is
        derived from the span and the attempt number, so two attempts stamped ``number=1`` from one
        span are **one** event with two documents, and the breakdown would have summed to 1 with
        every bucket looking right. The first draft of this test did exactly that and the count said
        so.

        The breakdown is scoped to the ``outcome`` event family. **It has to be**: ``attempt()``
        writes three records and all three carry the outcome class, so an unscoped breakdown counts
        one attempt three times — see the test below, which measures that rather than assuming it.

        All six classes must come back — a missing key renders as a missing bar and reads as "this
        never happens" — with ``success_empty`` at 1 and ``error`` at 0.
        """
        with live_pipeline.span():
            live_pipeline.emitter.attempt(
                agent_role="provider_matching",
                duration_ms=140.0,
                number=1,
                outcome=OutcomeClass.SUCCESS_EMPTY,
                synthetic=Synthetic.SYNTHETIC,
            )
            live_pipeline.emitter.attempt(
                agent_role="provider_matching",
                duration_ms=180.0,
                number=2,
                outcome=OutcomeClass.SUCCESS_FULL,
                synthetic=Synthetic.SYNTHETIC,
            )
        live_pipeline.flush()
        live_pipeline.documents("outcome", count=2)

        breakdown = live_query.outcome_breakdown(
            time_range=recent_window(),
            filters=Filters(deployment_annotation=live_pipeline.marker, event_names=("outcome",)),
        )

        assert set(breakdown) == {str(cls) for cls in OutcomeClass}
        assert breakdown["success_empty"].count == 1
        assert breakdown["success_full"].count == 1
        assert breakdown["error"].count == 0
        # And the empty result is not double-counted into some other bucket by a roll-up nobody
        # asked for: the classes are disjoint and sum to the number of outcome events.
        assert sum(sample.count for sample in breakdown.values()) == 2

    def test_an_unscoped_breakdown_counts_every_record_that_carries_the_outcome(
        self, live_pipeline, live_query, scoped
    ) -> None:
        """**Measured, and a trap for whoever wires the admin chart.**

        One ``attempt()`` call writes three records — the attempt count, the latency, and the outcome
        event — and all three carry ``outcome.class``. An outcome breakdown with no ``event_names``
        filter therefore counts that one attempt as **three**, and it is not wrong: it answers "how
        many records carry this outcome", which is what the query says. The chart wants "how many
        attempts ended this way", and those are different questions that happen to share a
        dimension.

        Recorded here rather than in a comment, because the number that comes back looks plausible.
        §15 does not provide the roll-up; the caller has to name the event family it means.
        """
        with live_pipeline.span():
            live_pipeline.emitter.attempt(
                agent_role="provider_matching",
                duration_ms=140.0,
                number=1,
                outcome=OutcomeClass.SUCCESS_EMPTY,
                synthetic=Synthetic.SYNTHETIC,
            )
        live_pipeline.flush()
        live_pipeline.documents("outcome", count=1)

        unscoped = live_query.outcome_breakdown(time_range=recent_window(), filters=scoped)
        assert unscoped["success_empty"].count == 3, "three records, one attempt"
        assert unscoped["success_empty"].documents == 3

        outcome_only = live_query.outcome_breakdown(
            time_range=recent_window(),
            filters=Filters(deployment_annotation=live_pipeline.marker, event_names=("outcome",)),
        )
        assert outcome_only["success_empty"].count == 1


class TestTheLatencyDistribution:
    def test_a_timeout_is_in_the_distribution_and_excluding_it_is_visible(
        self, live_pipeline, live_query, scoped
    ) -> None:
        """§15: "Özellikle timeout'lar latency dağılımından silinmez", asserted as a difference.

        The same aggregation is run twice over the same three attempts, once with the failures
        included and once without. ``include_errors=True`` is the default and the honest reading; the
        other exists only so the difference can be measured. The gap between the two maxima is the
        timeout, which is what makes the claim checkable rather than rhetorical.

        The three attempts are numbered 1, 2 and 3 because that is the retry story §15's attempt
        metric is for: one invocation, three attempts, the third of which timed out after nine
        seconds. ``event.id`` is derived from the attempt number, so leaving all three at ``number=1``
        would have made them one event with three documents, and the distribution would have been
        built from a single reading.
        """
        with live_pipeline.span():
            for number, (duration, outcome) in enumerate(
                (
                    (100.0, OutcomeClass.SUCCESS_FULL),
                    (120.0, OutcomeClass.SUCCESS_FULL),
                    (9000.0, OutcomeClass.ERROR),
                ),
                start=1,
            ):
                live_pipeline.emitter.attempt(
                    agent_role="scheduling",
                    duration_ms=duration,
                    number=number,
                    outcome=outcome,
                    error_type="timeout" if outcome is OutcomeClass.ERROR else None,
                    synthetic=Synthetic.SYNTHETIC,
                )
        live_pipeline.flush()
        live_pipeline.documents("latency", count=3)

        with_errors = live_query.latency(time_range=recent_window(), kind="attempt", filters=scoped)
        without_errors = live_query.latency(
            time_range=recent_window(), kind="attempt", filters=scoped, include_errors=False
        )

        assert with_errors.sample.count == 3
        # The bounds are the live regression test for the min/max bug: the percentiles aggregation
        # returns only the percents asked for, so the query asks for the 0th and 100th.
        assert with_errors.min_ms == pytest.approx(100.0)
        assert with_errors.max_ms == pytest.approx(9000.0)

        assert without_errors.sample.count == 2
        assert without_errors.max_ms == pytest.approx(120.0)
        assert without_errors.max_ms < with_errors.max_ms
        assert without_errors.min_ms == pytest.approx(100.0)
        assert without_errors.p50 is not None and 100.0 <= without_errors.p50 <= 120.0

    def test_an_empty_window_reports_no_data_rather_than_zero(
        self, live_pipeline, live_query
    ) -> None:
        """A zero-millisecond latency would plot as an impossibly fast system; the metric is
        ``None`` instead, and the sample count is what says why."""
        result = live_query.latency(
            time_range=recent_window(),
            kind="attempt",
            filters=Filters(deployment_annotation="no-such-deployment-annotation"),
        )
        assert result.sample.count == 0
        assert result.p50 is None
        assert result.min_ms is None and result.max_ms is None


class TestTheTimeSeries:
    def test_the_buckets_are_in_the_requested_timezone_and_each_carries_its_sample(
        self, live_pipeline, live_query
    ) -> None:
        """§12: "gün, ay, özel aralık, timezone ... ve sample count".

        The window is "today in Chicago", which is the range a dashboard's day view asks for and is
        *not* a UTC day. The documents were written now, so they are inside it; the assertions are
        that the points come back in that zone and that a bucket carrying hits carries its own
        sample count rather than the window's.

        The two triggers are emitted in two spans rather than together in one, because two agent
        triggers are two invocations and each one has its own span. Inside a single span they would
        share a ``trace_id`` and ``span_id`` and the same ``agent_trigger``/``event.count`` pair has
        no other discriminator — so they would be one event with two documents, and the sample count
        asserted below would be 1.
        """
        for _ in range(2):
            with live_pipeline.span():
                live_pipeline.emitter.agent_trigger(
                    agent_role="scheduling", synthetic=Synthetic.SYNTHETIC
                )
        live_pipeline.flush()
        live_pipeline.documents("agent_trigger", count=2)

        series = live_query.time_series(
            time_range=TimeRange.day(timezone="America/Chicago"),
            filters=Filters(
                deployment_annotation=live_pipeline.marker, event_names=("agent_trigger",)
            ),
        )

        assert series.interval == "hour"
        assert series.timezone == "America/Chicago"
        assert series.sample.count >= 2
        populated = [(at, sample) for at, sample in series.points if sample.count]
        assert populated, "the records were written inside this window"
        for at, sample in populated:
            assert at.utcoffset() is not None
            # Chicago is UTC-5 in summer and UTC-6 in winter, never UTC: a bucket stamped in UTC
            # would put the day boundary in the wrong place, which is the whole reason the zone is
            # passed to OpenSearch rather than applied afterwards.
            assert at.utcoffset().total_seconds() in {-5 * 3600, -6 * 3600}
            assert sample.count >= 1, "a bucket with hits carries its own sample count"
        # The per-bucket samples partition the window's total, so no bucket is silently reporting
        # the window's number instead of its own.
        assert sum(sample.count for _, sample in populated) == series.sample.count

    def test_a_month_window_buckets_by_day(self, live_pipeline, live_query) -> None:
        """The interval is chosen from the window, and the choice is visible in the result."""
        series = live_query.time_series(
            time_range=TimeRange.month(timezone="Europe/Istanbul"),
            filters=Filters(deployment_annotation=live_pipeline.marker),
        )
        assert series.interval == "day"
        assert series.timezone == "Europe/Istanbul"


class TestTheFeedbackNumbers:
    def test_the_last_vote_on_a_response_is_the_one_that_counts(
        self, live_pipeline, live_query, scoped
    ) -> None:
        """§15: feedback is the last active thumbs per ``response_id``, with the raw count, the like
        rate and the participation rate as **three separate numbers**.

        Three votes are emitted over two responses: up then down on ``r1`` (so the collapse has
        something to do), and up on ``r2``. ``total_responses`` is 4, and it is passed by the caller
        because the responses nobody rated are exactly the ones not in the index — deriving it from
        the votes would make the participation rate 100% by construction.

        The 20 ms gap between the two votes on ``r1`` is deliberate. The sort key is the epoch
        millisecond, so two votes inside one millisecond tie and are broken by index order — good
        enough for a flip-flop written by one batch (measured), but a test that *depends* on the
        tiebreaker would be testing the heuristic rather than the rule.
        """
        with live_pipeline.span():
            live_pipeline.emitter.feedback(
                response_id="r1", value="up", synthetic=Synthetic.SYNTHETIC
            )
            time.sleep(0.02)
            live_pipeline.emitter.feedback(
                response_id="r1", value="down", synthetic=Synthetic.SYNTHETIC
            )
            live_pipeline.emitter.feedback(
                response_id="r2", value="up", synthetic=Synthetic.SYNTHETIC
            )
        live_pipeline.flush()
        live_pipeline.documents("feedback", count=3)

        summary = live_query.feedback(time_range=recent_window(), total_responses=4, filters=scoped)

        assert summary.raw_count == 2, "two responses have a vote, not three votes"
        assert summary.likes == 1, "r1 ends on down, so only r2 is a like"
        assert summary.dislikes == 1
        assert summary.like_rate == pytest.approx(0.5), "1 like out of the 2 responses that voted"
        assert summary.participation_rate == pytest.approx(0.5), "2 of 4 responses were rated"
        assert summary.truncated is False

    def test_the_participation_rate_moves_with_the_response_total(
        self, live_pipeline, live_query, scoped
    ) -> None:
        """The same votes, a different denominator: the two rates are independent numbers and the
        participation rate is not derivable from the votes alone."""
        with live_pipeline.span():
            live_pipeline.emitter.feedback(
                response_id="solo", value="up", synthetic=Synthetic.SYNTHETIC
            )
        live_pipeline.flush()
        live_pipeline.documents("feedback", count=1)

        summary = live_query.feedback(
            time_range=recent_window(), total_responses=10, filters=scoped
        )
        assert summary.raw_count == 1
        assert summary.like_rate == pytest.approx(1.0)
        assert summary.participation_rate == pytest.approx(0.1)


class TestTheIndexHealthAndTheSeparation:
    def test_the_health_report_names_the_retention_window(self, live_query) -> None:
        """§15 promises 7 days for telemetry; the report has to carry the promise, not just the
        indices, or the number lives only in a docstring."""
        health = live_query.index_health()
        assert health["pattern"] == EVENT_INDEX_PATTERN
        assert health["retention_days"] == TELEMETRY_RETENTION_DAYS == 7
        assert health["indices"], "the pipeline has written at least one events index by now"
        assert all(row["index"].startswith("otel-events-") for row in health["indices"])

    def test_the_directory_index_cannot_be_read_through_this_class(self) -> None:
        """§15: "Rehber ve telemetri indeksleri ayrılır". Retrieved directory text is untrusted
        content, and a query object that could be pointed at it would let a *retrieved* document be
        read as a *recorded* one."""
        with pytest.raises(UnindexablePatternError, match="directory"):
            TelemetryQuery("http://localhost:9200", index="referraleasy-directory-chunks")


class TestTheKeywordTrap:
    def test_a_term_query_on_the_text_field_finds_nothing_and_the_keyword_field_finds_it(
        self, live_pipeline, live_query
    ) -> None:
        """**Measured on the running stack**, and the worst kind of mistake for a live test.

        Data Prepper writes ``log.attributes.deployment@annotation`` as a string, OpenSearch maps it
        dynamically as ``text`` with a ``keyword`` sub-field, and a ``term`` query against an
        analysed field is compared against a *token*. Nothing errors: the query returns zero hits.
        An assertion of the form "these documents are not there" therefore passes for entirely the
        wrong reason, which is why :func:`~referraleasy_telemetry.query.keyword_field` exists and why
        ``conftest.term_query`` refuses the bare form.

        Both directions are asserted here, on one live document, so the difference is not a claim
        about OpenSearch but a measurement against this index.
        """
        with live_pipeline.span():
            live_pipeline.emitter.agent_trigger(
                agent_role="scheduling", synthetic=Synthetic.SYNTHETIC
            )
        live_pipeline.flush()
        live_pipeline.document("agent_trigger")

        marker = live_pipeline.marker
        plain = live_query.search(
            {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "term": {"log.attributes.deployment@annotation": marker},
                },
            }
        )
        sub_field = live_query.search(
            {
                "size": 0,
                "track_total_hits": True,
                "query": {"term": {keyword_field("deployment.annotation"): marker}},
            }
        )
        assert plain["hits"]["total"]["value"] == 0, "the analysed field matches no token"
        assert sub_field["hits"]["total"]["value"] >= 1, "the keyword sub-field is the one to use"

    def test_a_numeric_attribute_needs_no_sub_field(self, live_pipeline, live_query) -> None:
        """The other half of the trap: numbers are mapped without a sub-field, so appending
        ``.keyword`` to one would be the same silent zero in the opposite direction."""
        with live_pipeline.span():
            live_pipeline.emitter.attempt(
                agent_role="scheduling",
                duration_ms=321.5,
                number=1,
                outcome=OutcomeClass.SUCCESS_FULL,
                synthetic=Synthetic.SYNTHETIC,
            )
        live_pipeline.flush()
        live_pipeline.documents("latency", count=1)

        hit = live_query.search(
            {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            {
                                "term": {
                                    "log.attributes.deployment@annotation.keyword": live_pipeline.marker
                                }
                            },
                            {"term": {"log.attributes.metric@value": 321.5}},
                        ]
                    }
                },
            }
        )
        assert hit["hits"]["total"]["value"] == 1

        suffixed = live_query.search(
            {
                "size": 0,
                "track_total_hits": True,
                "query": {
                    "bool": {
                        "filter": [
                            {
                                "term": {
                                    "log.attributes.deployment@annotation.keyword": live_pipeline.marker
                                }
                            },
                            {"term": {"log.attributes.metric@value.keyword": 321.5}},
                        ]
                    }
                },
            }
        )
        assert suffixed["hits"]["total"]["value"] == 0, (
            "a number has no keyword sub-field, and asking for one is silent"
        )
