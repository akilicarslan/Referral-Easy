# ruff: noqa: RUF002, RUF003  # Turkish quoted from §15 of ReferralEasy_Architecture_v3.md is kept verbatim
"""The §15 vocabulary, and the rules that are properties of the vocabulary itself.

These are the cheapest tests in the suite and they hold the most valuable line: each one asserts a
sentence from §15 that a later refactor could quietly drop. A closed set that quietly loses a member
does not fail anything — it makes a chart missing a bar.
"""

from __future__ import annotations

import pytest

from referraleasy_telemetry.conventions import (
    ACTORS,
    AGENT_ROLES,
    ALLOWED_QUERY_PATTERNS,
    ATTR_EVENT_ID,
    ATTR_METRIC_VALUE,
    CORRELATION_ATTRIBUTES,
    DIMENSION_ATTRIBUTES,
    DIRECTORY_INDEX_PATTERN,
    EVALUATION_INDEX_PATTERN,
    EVALUATION_RETENTION_DAYS,
    EVENT_INDEX_PATTERN,
    FLATTENED_DOT,
    PRINCIPALS,
    TELEMETRY_RETENTION_DAYS,
    TRACE_INDEX_PATTERN,
    WORKFLOW_STATES,
    EventName,
    MetricName,
    OutcomeClass,
    field_name,
)


class TestClosedSets:
    """§15's enums, by count and by content. A set that changes size is a set that changed."""

    def test_sixteen_workflow_states(self) -> None:
        assert len(WORKFLOW_STATES) == 16
        assert len(set(WORKFLOW_STATES)) == 16, "a duplicated state would double-count a filter"

    def test_the_states_are_the_ones_the_spec_names(self) -> None:
        assert WORKFLOW_STATES == (
            "INTAKE",
            "COVERAGE_CHECK",
            "PROVIDER_MATCHING",
            "AWAITING_HOSPITAL_CHOICE",
            "AWAITING_DOCTOR_CHOICE",
            "READINESS_CHECK",
            "AWAITING_SLOT_CHOICE",
            "AWAITING_FINAL_APPROVAL",
            "BOOKING_IN_FLIGHT",
            "BOOKED",
            "BOOKING_UNVERIFIED",
            "RECOVERY",
            "HUMAN_HANDOFF",
            "ABANDONED",
            "CANCELLED",
            "FAILED",
        )

    def test_seven_agent_roles_and_two_principals(self) -> None:
        assert len(AGENT_ROLES) == 7
        assert PRINCIPALS == ("operator", "system")
        assert len(ACTORS) == 9
        # Operators and the system are not agents. Counting them as agents would inflate every
        # per-agent chart by whatever the operator queue did.
        assert not set(AGENT_ROLES) & set(PRINCIPALS)

    def test_six_outcome_classes(self) -> None:
        assert [str(c) for c in OutcomeClass] == [
            "success_full",
            "success_empty",
            "success_partial",
            "error",
            "blocked",
            "fallback",
        ]


class TestEmptyIsNotAnError:
    """§15: "Empty geçerli sonuç error değildir". Stated, so it gets a test of its own."""

    def test_success_empty_is_a_success(self) -> None:
        assert OutcomeClass.SUCCESS_EMPTY.is_success
        assert not OutcomeClass.SUCCESS_EMPTY.is_error

    def test_only_error_is_an_error(self) -> None:
        assert OutcomeClass.ERROR.is_error
        assert not OutcomeClass.SUCCESS_FULL.is_error
        assert not OutcomeClass.SUCCESS_PARTIAL.is_error

    def test_blocked_and_fallback_are_neither(self) -> None:
        """They are their own outcomes, and folding either into `error` or into `success` would
        make the failure rate disagree with what actually happened."""
        for outcome in (OutcomeClass.BLOCKED, OutcomeClass.FALLBACK):
            assert not outcome.is_error
            assert not outcome.is_success

    def test_every_class_is_exactly_one_of_the_three_groups(self) -> None:
        for outcome in OutcomeClass:
            groups = sum([outcome.is_success, outcome.is_error, _neither(outcome)])
            assert groups == 1, f"{outcome} falls into {groups} groups"


def _neither(outcome: OutcomeClass) -> bool:
    return outcome in (OutcomeClass.BLOCKED, OutcomeClass.FALLBACK)


class TestTraceIdIsNeverALabel:
    """§15: "Trace ID metric label yapılmaz" — expressed as data, so it can be asserted."""

    def test_trace_id_is_not_a_dimension(self) -> None:
        assert "trace_id" not in DIMENSION_ATTRIBUTES
        assert "span_id" not in DIMENSION_ATTRIBUTES

    def test_no_per_request_identifier_is_a_dimension(self) -> None:
        for identifier in CORRELATION_ATTRIBUTES:
            assert identifier not in DIMENSION_ATTRIBUTES, (
                f"{identifier} is an identifier, not a dimension: a terms aggregation on it is one "
                f"bucket per request"
            )

    def test_event_id_is_not_a_dimension(self) -> None:
        """`event.id` is the dedup key *and* is unique per record, so it is the worst possible
        label — it would produce exactly one bucket per document."""
        assert ATTR_EVENT_ID not in DIMENSION_ATTRIBUTES

    def test_the_event_id_is_a_correlation_attribute(self) -> None:
        assert ATTR_EVENT_ID in CORRELATION_ATTRIBUTES

    def test_dimensions_are_unique(self) -> None:
        assert len(set(DIMENSION_ATTRIBUTES)) == len(DIMENSION_ATTRIBUTES)


class TestFieldNaming:
    """The flattening Data Prepper applies, encoded once.

    Every assertion here was read off a live document rather than inferred; see
    ``test_pipeline_live.py``, which proves the same thing end to end.
    """

    def test_single_dot_becomes_at(self) -> None:
        assert field_name("metric.name") == "log.attributes.metric@name"

    def test_every_dot_is_replaced(self) -> None:
        """Measured: the OTLP attribute ``http.request.header.authorization`` arrives as
        ``span.attributes.http@request@header@authorization`` — all three dots, not just the
        first."""
        assert (
            field_name("http.request.header.authorization", family="span")
            == "span.attributes.http@request@header@authorization"
        )

    def test_an_undotted_name_is_unchanged(self) -> None:
        assert field_name("synthetic") == "log.attributes.synthetic"

    def test_flat_form_matches_the_constant(self) -> None:
        assert FLATTENED_DOT == "@"

    @pytest.mark.parametrize("family", ["span", "log", "resource"])
    def test_every_family_is_accepted(self, family: str) -> None:
        assert field_name("x.y", family=family) == f"{family}.attributes.x@y"

    def test_an_unknown_family_is_refused(self) -> None:
        """A wrong family produces a query that matches nothing, so it is refused rather than
        sent."""
        with pytest.raises(ValueError, match="unknown attribute family"):
            field_name("metric.name", family="span_event")


class TestIndexFamiliesAndRetention:
    """§15: separate directory and telemetry indices; 7 days telemetry, 30 days evaluation."""

    def test_retention_is_seven_and_thirty_days(self) -> None:
        assert TELEMETRY_RETENTION_DAYS == 7
        assert EVALUATION_RETENTION_DAYS == 30

    def test_evaluation_is_kept_longer_than_telemetry(self) -> None:
        assert EVALUATION_RETENTION_DAYS > TELEMETRY_RETENTION_DAYS

    def test_the_directory_index_is_not_queryable_as_telemetry(self) -> None:
        assert DIRECTORY_INDEX_PATTERN not in ALLOWED_QUERY_PATTERNS

    def test_the_three_telemetry_families_are_queryable(self) -> None:
        assert set(ALLOWED_QUERY_PATTERNS) == {
            TRACE_INDEX_PATTERN,
            EVENT_INDEX_PATTERN,
            EVALUATION_INDEX_PATTERN,
        }


class TestMetricUnits:
    """Every metric has a unit, so a chart axis is never guessed."""

    def test_every_metric_has_a_unit(self) -> None:
        from referraleasy_telemetry.events import METRIC_UNITS

        assert set(METRIC_UNITS) == set(MetricName)

    def test_the_numeric_payload_field_is_the_one_the_query_layer_reads(self) -> None:
        from referraleasy_telemetry.query import METRIC_VALUE_FIELD

        assert field_name(ATTR_METRIC_VALUE) == METRIC_VALUE_FIELD
        assert METRIC_VALUE_FIELD == "log.attributes.metric@value"


class TestEventVocabulary:
    """Every §15 measurement has an event name, and nothing else does."""

    def test_the_measurements_of_section_15_are_all_present(self) -> None:
        required = {
            "agent_trigger",  # agent logical trigger count
            "attempt",  # attempt/retry count
            "retry",
            "outcome",  # the six classes
            "api_failure",  # API fail endpoint/status
            "latency",  # p50/p95/p99
            "model_usage",  # model token/maliyet
            "hitl_wait",  # HITL bekleme
            "hitl_resolve",  # HITL çözüm
            "guardrail_decision",  # guardrail kararları
            "eval_score",  # değerlendirme skorları
            "feedback",
        }
        assert required <= {str(name) for name in EventName}
