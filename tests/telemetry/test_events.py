# ruff: noqa: RUF001, RUF002  # Turkish quoted from §15 of ReferralEasy_Architecture_v3.md is kept verbatim
"""The §15 measurements, as events: dedup keys, the closed vocabularies, and the three rules that a
``try/except`` gets wrong by default.

The three rules, each of which produces a chart that looks fine and is wrong:

* ``success_empty`` is a success. A finder that searched and legitimately found nothing did its job.
  Counting it as an error inflates the failure rate with the platform working correctly.
* A **timeout is still a latency sample**. Measured in a ``finally``, so the failing path cannot
  return early past the measurement.
* ``trace_id`` is **never** a dimension. A terms aggregation on it produces one bucket per request.

Nothing here needs the stack: the emitter writes into :class:`conftest.RecordingLogger` and the
assertions are on the records themselves. What that deliberately does *not* prove is that the records
reach OpenSearch — that is ``test_pipeline_live.py``, and it is the one that matters most.
"""

from __future__ import annotations

import pytest
from opentelemetry._logs import SeverityNumber

from referraleasy_telemetry.conventions import (
    ATTR_AGENT_ROLE,
    ATTR_ATTEMPT_MAX,
    ATTR_ATTEMPT_NUMBER,
    ATTR_DEPLOYMENT_ANNOTATION,
    ATTR_EVENT_ID,
    ATTR_EVENT_NAME,
    ATTR_METRIC_NAME,
    ATTR_METRIC_UNIT,
    ATTR_METRIC_VALUE,
    ATTR_OUTCOME_CLASS,
    ATTR_SYNTHETIC,
    EventName,
    LatencyKind,
    MetricName,
    OutcomeClass,
    Synthetic,
)
from referraleasy_telemetry.events import (
    METRIC_UNITS,
    EventIdDedup,
    MissingEventIdentityError,
    TelemetryEmitter,
    UnknownDimensionError,
    build_event,
    derive_event_id,
    validate_dimensions,
)
from referraleasy_telemetry.identity import CorrelationIds

TRACE_ID = "5b8efff798038103d269b633813fc60c"
SPAN_ID = "eee19b7ec3c1b174"


class TestDerivedEventIds:
    """Dedup is structural: the id is a function of the emission, not a random value."""

    def test_the_same_emission_derives_the_same_id(self) -> None:
        kwargs = {
            "event_name": "attempt",
            "metric_name": "event.count",
            "trace_id": TRACE_ID,
            "span_id": SPAN_ID,
        }
        assert derive_event_id(**kwargs) == derive_event_id(**kwargs)

    def test_it_is_stable_across_processes(self) -> None:
        """A literal expectation, because the whole dedup rests on the namespace and the separator
        being the same in the re-exporting process as in the original one. If this number changes,
        every id derived before the change stops matching the ids derived after it — silently, and
        in the direction of double-counting."""
        assert (
            derive_event_id(event_name="attempt", metric_name="event.count", span_id=SPAN_ID)
            == "e9dea312-6d5c-5777-ae85-685677d26cf6"
        )

    @pytest.mark.parametrize(
        "changed",
        [
            {"event_name": "outcome"},
            {"metric_name": "latency.attempt.ms"},
            {"span_id": "0000000000000001"},
            {"trace_id": "0" * 31 + "1"},
            {"discriminator": "count:2"},
        ],
    )
    def test_any_difference_yields_a_different_id(self, changed: dict[str, str]) -> None:
        base: dict[str, object] = {
            "event_name": "attempt",
            "metric_name": "event.count",
            "trace_id": TRACE_ID,
            "span_id": SPAN_ID,
            "discriminator": "count:1",
        }
        assert derive_event_id(**{**base, **changed}) != derive_event_id(**base)  # type: ignore[arg-type]

    def test_a_discriminator_distinguishes_two_attempts_of_one_span(self) -> None:
        """One span, two attempts: same trace, same span, same measurement, different id."""
        one = derive_event_id(
            event_name="attempt",
            metric_name="event.count",
            span_id=SPAN_ID,
            discriminator="count:1",
        )
        two = derive_event_id(
            event_name="attempt",
            metric_name="event.count",
            span_id=SPAN_ID,
            discriminator="count:2",
        )
        assert one != two

    def test_no_identity_and_no_discriminator_is_refused(self) -> None:
        """Two such emissions would be indistinguishable, so hashing them to one id would
        under-count silently. Refusing is what makes the caller supply the missing identity."""
        with pytest.raises(MissingEventIdentityError, match="discriminator"):
            derive_event_id(event_name="attempt", metric_name="event.count")

    def test_a_discriminator_alone_is_enough(self) -> None:
        assert derive_event_id(
            event_name="attempt", metric_name="event.count", discriminator="cmd-1"
        )

    def test_a_trace_id_alone_is_enough(self) -> None:
        assert derive_event_id(event_name="attempt", metric_name="event.count", trace_id=TRACE_ID)


class TestBuildEvent:
    def test_the_record_carries_the_four_metric_fields(self) -> None:
        record = build_event(
            event_name=EventName.LATENCY,
            metric_name=MetricName.LATENCY_ATTEMPT_MS,
            metric_value=1234.5,
            discriminator="latency:1",
        )
        assert record.attributes[ATTR_METRIC_NAME] == "latency.attempt.ms"
        assert record.attributes[ATTR_METRIC_VALUE] == 1234.5
        assert record.attributes[ATTR_METRIC_UNIT] == "ms"
        assert record.attributes[ATTR_EVENT_NAME] == "latency"

    def test_every_metric_has_a_unit_and_an_unknown_metric_falls_back_to_count(self) -> None:
        record = build_event(
            event_name=EventName.GUARDRAIL_DECISION,
            metric_name="custom.thing",
            metric_value=1,
            discriminator="x",
        )
        assert record.attributes[ATTR_METRIC_UNIT] == "count"
        assert set(METRIC_UNITS) == set(MetricName)

    def test_the_event_id_is_attached_to_the_attributes(self) -> None:
        record = build_event(
            event_name=EventName.ATTEMPT,
            metric_name=MetricName.EVENT_COUNT,
            metric_value=1,
            discriminator="count:1",
        )
        assert record.attributes[ATTR_EVENT_ID] == record.event_id

    def test_an_explicit_event_id_is_honoured(self) -> None:
        """Idempotent replay: a worker that re-emits the same logical event passes the id it used
        the first time, rather than a second one derived from a new span."""
        record = build_event(
            event_name=EventName.ATTEMPT,
            metric_name=MetricName.EVENT_COUNT,
            metric_value=1,
            event_id="11111111-2222-3333-4444-555555555555",
        )
        assert record.event_id == "11111111-2222-3333-4444-555555555555"

    def test_an_outcome_is_stored_under_the_outcome_attribute(self) -> None:
        record = build_event(
            event_name=EventName.OUTCOME,
            metric_name=MetricName.EVENT_COUNT,
            metric_value=1,
            outcome=OutcomeClass.SUCCESS_EMPTY,
            discriminator="o",
        )
        assert record.attributes[ATTR_OUTCOME_CLASS] == "success_empty"
        assert record.outcome is OutcomeClass.SUCCESS_EMPTY

    def test_an_outcome_that_contradicts_the_dimensions_is_refused(self) -> None:
        """The six classes are a partition; a record in two at once is counted twice."""
        with pytest.raises(ValueError, match="partition"):
            build_event(
                event_name=EventName.OUTCOME,
                metric_name=MetricName.EVENT_COUNT,
                metric_value=1,
                outcome=OutcomeClass.ERROR,
                dimensions={ATTR_OUTCOME_CLASS: "success_full"},
                discriminator="o",
            )

    def test_the_same_outcome_in_both_places_is_allowed(self) -> None:
        record = build_event(
            event_name=EventName.OUTCOME,
            metric_name=MetricName.EVENT_COUNT,
            metric_value=1,
            outcome=OutcomeClass.ERROR,
            dimensions={ATTR_OUTCOME_CLASS: OutcomeClass.ERROR},
            discriminator="o",
        )
        assert record.outcome is OutcomeClass.ERROR

    def test_a_correlation_id_is_attached_as_a_field(self) -> None:
        record = build_event(
            event_name=EventName.ATTEMPT,
            metric_name=MetricName.EVENT_COUNT,
            metric_value=1,
            correlation=CorrelationIds(workflow_id="wf-1", task_id="t-1"),
            discriminator="x",
        )
        assert record.attributes["workflow_id"] == "wf-1"
        assert record.attributes["a2a.task_id"] == "t-1"

    def test_a_body_is_generated_when_none_is_given(self) -> None:
        record = build_event(
            event_name=EventName.ATTEMPT,
            metric_name=MetricName.LATENCY_ATTEMPT_MS,
            metric_value=42,
            discriminator="x",
        )
        assert "latency.attempt.ms" in record.body and "42" in record.body

    def test_severity_follows_the_outcome_class(self) -> None:
        def severity(outcome: OutcomeClass) -> SeverityNumber:
            return build_event(
                event_name=EventName.OUTCOME,
                metric_name=MetricName.EVENT_COUNT,
                metric_value=1,
                outcome=outcome,
                discriminator="x",
            ).severity

        assert severity(OutcomeClass.ERROR) is SeverityNumber.ERROR
        # A guardrail block and a fallback are outcomes the platform handled. Marking them ERROR
        # would make an error-severity filter return things that did not fail.
        assert severity(OutcomeClass.BLOCKED) is SeverityNumber.WARN
        assert severity(OutcomeClass.FALLBACK) is SeverityNumber.WARN
        assert severity(OutcomeClass.SUCCESS_EMPTY) is SeverityNumber.INFO


class TestDimensionsAreChecked:
    """The check that makes the trace-id rule enforceable rather than aspirational."""

    def test_trace_id_as_a_dimension_is_refused_with_the_spec_reason(self) -> None:
        with pytest.raises(UnknownDimensionError) as caught:
            validate_dimensions({"trace_id": TRACE_ID})
        assert "metric label yapılmaz" in str(caught.value)
        assert "trace_id=" in str(caught.value), "the message must say what to do instead"

    def test_the_discriminator_is_the_way_out(self) -> None:
        """Same information, attached as a field: the refusal is not a dead end."""
        record = build_event(
            event_name=EventName.ATTEMPT,
            metric_name=MetricName.EVENT_COUNT,
            metric_value=1,
            trace_id=TRACE_ID,
            span_id=SPAN_ID,
            discriminator="x",
        )
        assert record.attributes["trace_id"] == TRACE_ID

    @pytest.mark.parametrize(
        "key", ["conversation_id", "workflow_id", "command_id", "booking_request_id", "event.id"]
    )
    def test_a_per_request_identifier_is_refused_as_a_dimension(self, key: str) -> None:
        with pytest.raises(UnknownDimensionError):
            validate_dimensions({key: "some-value"})

    def test_an_unknown_dimension_names_the_allowed_set(self) -> None:
        with pytest.raises(UnknownDimensionError, match="DIMENSION_ATTRIBUTES"):
            validate_dimensions({"whoops": 1})

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            (ATTR_AGENT_ROLE, "not_a_role"),
            (ATTR_OUTCOME_CLASS, "sort_of_worked"),
            (ATTR_SYNTHETIC, "maybe"),
            (ATTR_AGENT_ROLE, ""),
        ],
    )
    def test_a_dimension_value_outside_its_vocabulary_is_refused(
        self, key: str, value: str
    ) -> None:
        """An unbounded or misspelled vocabulary splits a chart into buckets nobody can compare."""
        with pytest.raises(UnknownDimensionError, match="known value"):
            validate_dimensions({key: value})

    def test_every_closed_vocabulary_accepts_all_its_members(self) -> None:
        for outcome in OutcomeClass:
            validate_dimensions({ATTR_OUTCOME_CLASS: outcome})
        for kind in LatencyKind:
            validate_dimensions({"latency.kind": kind})
        for name in EventName:
            validate_dimensions({ATTR_EVENT_NAME: name})


class TestEmptyIsNotAnError:
    """§15: "Empty geçerli sonuç error değildir" — one more place, because it is the rule most
    likely to be lost when someone writes a dashboard query in a hurry."""

    def test_a_success_empty_record_is_info_severity(self) -> None:
        record = build_event(
            event_name=EventName.OUTCOME,
            metric_name=MetricName.EVENT_COUNT,
            metric_value=1,
            outcome=OutcomeClass.SUCCESS_EMPTY,
            discriminator="x",
        )
        assert record.severity is SeverityNumber.INFO
        assert record.outcome is not None and not record.outcome.is_error

    def test_the_six_classes_are_all_reachable(self, emitter) -> None:
        for outcome in OutcomeClass:
            emitter.attempt(agent_role="scheduling", duration_ms=1.0, number=1, outcome=outcome)
        seen = {record["attributes"].get(ATTR_OUTCOME_CLASS) for record in emitter._logger.records}
        assert seen == {str(outcome) for outcome in OutcomeClass}


class TestAttempt:
    def test_one_attempt_is_three_measurements_with_three_ids(self, emitter, recorder) -> None:
        """A count and a distribution aggregate differently, so they are different documents. Two
        records with one id would be deduplicated into one and the count would be wrong."""
        counted, latency, outcome = emitter.attempt(
            agent_role="scheduling", duration_ms=250.0, number=1, outcome=OutcomeClass.SUCCESS_FULL
        )
        assert len({counted.event_id, latency.event_id, outcome.event_id}) == 3
        assert counted.attributes[ATTR_METRIC_NAME] == "event.count"
        assert latency.attributes[ATTR_METRIC_NAME] == "latency.attempt.ms"
        assert latency.attributes[ATTR_METRIC_VALUE] == 250.0
        assert len(recorder.records) == 3

    def test_the_attempt_number_rides_on_the_count_and_the_latency(self, emitter) -> None:
        counted, latency, _ = emitter.attempt(
            agent_role="scheduling", duration_ms=1.0, number=2, max_attempts=3
        )
        for record in (counted, latency):
            assert record.attributes[ATTR_ATTEMPT_NUMBER] == 2
            assert record.attributes[ATTR_ATTEMPT_MAX] == 3

    def test_the_outcome_record_does_not_carry_the_attempt_number(self, emitter) -> None:
        """It is the *workflow's* outcome, not attempt 2's. Splitting it by attempt would count the
        same outcome once per retry."""
        _, _, outcome = emitter.attempt(
            agent_role="scheduling", duration_ms=1.0, number=2, outcome=OutcomeClass.SUCCESS_FULL
        )
        assert ATTR_ATTEMPT_NUMBER not in outcome.attributes

    def test_a_zero_duration_is_recorded(self, emitter) -> None:
        """Not special-cased. A zero-length attempt is a real (fast) sample, and treating 0 as
        "no measurement" is how a fast path silently disappears from a percentile."""
        _, latency, _ = emitter.attempt(agent_role="scheduling", duration_ms=0.0, number=1)
        assert latency.attributes[ATTR_METRIC_VALUE] == 0.0

    @pytest.mark.parametrize("kind", list(LatencyKind))
    def test_the_latency_kind_selects_the_metric(self, emitter, kind: LatencyKind) -> None:
        _, latency, _ = emitter.attempt(
            agent_role="scheduling", duration_ms=1.0, number=1, latency_kind=kind
        )
        assert latency.attributes[ATTR_METRIC_NAME] == f"latency.{kind}.ms"

    def test_the_deployment_annotation_rides_on_every_record(self, recorder) -> None:
        annotated = TelemetryEmitter(recorder, deployment_annotation="release-2026-10-15")
        _, latency, outcome = annotated.attempt(agent_role="scheduling", duration_ms=1.0, number=1)
        for record in (latency, outcome):
            assert record.attributes[ATTR_DEPLOYMENT_ANNOTATION] == "release-2026-10-15"


class TestAttemptSpanKeepsTimeoutsInTheDistribution:
    """§15: "timeout'lar latency dağılımından çıkarılmaz" — the rule this class exists for."""

    def test_a_successful_block_is_measured(self, emitter, recorder) -> None:
        with emitter.attempt_span(agent_role="scheduling", number=1) as info:
            info["outcome"] = OutcomeClass.SUCCESS_FULL
        latencies = recorder.by_metric("latency.attempt.ms")
        assert len(latencies) == 1
        assert latencies[0]["attributes"][ATTR_METRIC_VALUE] >= 0.0
        assert latencies[0]["attributes"][ATTR_OUTCOME_CLASS] == "success_full"

    def test_a_raising_block_is_still_measured(self, emitter, recorder) -> None:
        """The rule. A `try/except` that measures only on the success path is exactly how a p95
        looks healthy while users wait."""
        with (
            pytest.raises(TimeoutError),
            emitter.attempt_span(agent_role="scheduling", number=1) as info,
        ):
            info["outcome"] = OutcomeClass.ERROR
            info["error_type"] = "timeout"
            raise TimeoutError

        latencies = recorder.by_metric("latency.attempt.ms")
        assert len(latencies) == 1, "the timed-out attempt vanished from the distribution"
        assert latencies[0]["attributes"][ATTR_OUTCOME_CLASS] == "error"

    def test_an_unclassified_failure_is_recorded_as_error(self, emitter, recorder) -> None:
        """Defaulting to success would be the worse error, and it is the tempting default when the
        block never got as far as setting anything."""
        with pytest.raises(ValueError), emitter.attempt_span(agent_role="scheduling", number=1):
            raise ValueError("boom")
        latencies = recorder.by_metric("latency.attempt.ms")
        assert latencies[0]["attributes"][ATTR_OUTCOME_CLASS] == "error"

    def test_an_explicit_success_still_wins_when_the_block_returns(self, emitter, recorder) -> None:
        """`finally` runs on the returning path too, and must not overwrite a recorded outcome."""
        with emitter.attempt_span(agent_role="scheduling", number=1) as info:
            info["outcome"] = OutcomeClass.SUCCESS_EMPTY
        latencies = recorder.by_metric("latency.attempt.ms")
        assert latencies[0]["attributes"][ATTR_OUTCOME_CLASS] == "success_empty"

    def test_the_error_type_reaches_the_record(self, emitter, recorder) -> None:
        with (
            pytest.raises(TimeoutError),
            emitter.attempt_span(agent_role="scheduling", number=2) as info,
        ):
            info["error_type"] = "read_timeout"
            info["retry_reason"] = "upstream_slow"
            raise TimeoutError
        counted = recorder.by_event("attempt")[0]
        assert counted["attributes"]["error.type"] == "read_timeout"
        assert counted["attributes"]["retry.reason"] == "upstream_slow"


class TestAgentTriggerIsLogicalNotPhysical:
    def test_a_trigger_is_one_record_however_many_attempts_follow(
        self, emitter, recorder, local_tracer
    ) -> None:
        """§15 asks for the *logical* trigger count. An invocation that succeeded on its third
        attempt was triggered once; making the trigger count a second name for the attempt count is
        what turns a retry storm into a traffic spike."""
        with local_tracer.start_as_current_span("workflow.invocation"):
            emitter.agent_trigger(agent_role="scheduling")
            for number in (1, 2, 3):
                emitter.attempt(agent_role="scheduling", duration_ms=1.0, number=number)

        assert len(recorder.by_event("agent_trigger")) == 1
        assert len(recorder.by_event("attempt")) == 3
        assert len(recorder.by_event("outcome")) == 3
        assert len(recorder.by_event("latency")) == 3
        # One trigger and three attempts, distinguishable by ``event.name`` and not by the metric
        # they share: both counting measurements are ``event.count``, so a chart that grouped only
        # by ``metric.name`` would have reported four triggers.
        assert len(recorder.by_metric("event.count")) == 1 + 3 + 3
        trigger_names = {
            record["attributes"][ATTR_EVENT_NAME] for record in recorder.by_metric("event.count")
        }
        assert trigger_names == {"agent_trigger", "attempt", "outcome"}

    def test_an_emission_outside_a_span_is_refused(self, emitter) -> None:
        """The refusal is deliberate and it is the one thing in this package that *does* raise into
        a caller. Two triggers by the same agent outside any span are indistinguishable, so hashing
        them to one id would under-count every retry storm by exactly the amount that matters.

        This is also the boundary of ``IsolatedLogger``'s guarantee: the exception is raised while
        *building* the record, above the logger, so a process that wraps its logger has not made
        this safe. See ``test_isolation.py::test_the_emitter_can_still_raise_above_the_isolated_logger``.
        """
        with pytest.raises(MissingEventIdentityError, match="discriminator"):
            emitter.agent_trigger(agent_role="scheduling")

    def test_a_discriminator_is_the_alternative(self, emitter, recorder) -> None:
        """A caller with no span has to say what makes this emission distinct."""
        emitter.agent_trigger(agent_role="scheduling", discriminator="command-1")
        assert len(recorder.by_event("agent_trigger")) == 1


class TestApiFailure:
    def test_it_carries_the_endpoint_and_the_status(self, emitter) -> None:
        record = emitter.api_failure(endpoint="mock-api:/appointments", status_code=503)
        assert record.attributes["api.endpoint"] == "mock-api:/appointments"
        assert record.attributes["api.status_code"] == 503
        assert record.attributes[ATTR_OUTCOME_CLASS] == "error"

    def test_a_bare_path_is_accepted(self, emitter) -> None:
        assert emitter.api_failure(endpoint="/appointments", status_code=500)

    @pytest.mark.parametrize(
        "endpoint",
        [
            "https://mock-api.example.com/appointments",
            "mock-api:/appointments?patient=123",
            "/appointments#frag",
            "/appointments&x=1",
            "not-a-ref",
        ],
    )
    def test_a_url_or_query_string_is_refused(self, emitter, endpoint: str) -> None:
        """A URL is unbounded — it carries an id — and would put a per-request value into a field
        used as a dimension."""
        with pytest.raises(ValueError, match="endpoint"):
            emitter.api_failure(endpoint=endpoint, status_code=500)

    def test_the_failure_kind_is_optional_but_kept_when_given(self, emitter) -> None:
        record = emitter.api_failure(
            endpoint="/appointments", status_code=429, failure_kind="rate_limited"
        )
        assert record.attributes["api.failure_kind"] == "rate_limited"


class TestModelUsage:
    def test_three_measurements_with_three_ids(self, emitter, recorder, local_tracer) -> None:
        with local_tracer.start_as_current_span("model.call"):
            records = emitter.model_usage(
                agent_role="scheduling",
                model_name="synthetic-model",
                input_tokens=1200,
                output_tokens=340,
                cost_usd=0.0042,
            )
        assert len(records) == 3
        assert len({r.event_id for r in records}) == 3, (
            "one span, one measurement name each: three documents that must not collapse into one"
        )
        assert {r.metric_name for r in records} == {
            "model.tokens.input",
            "model.tokens.output",
            "model.cost.usd",
        }

    def test_the_units_are_the_right_ones(self, emitter, local_tracer) -> None:
        with local_tracer.start_as_current_span("model.call"):
            records = emitter.model_usage(
                agent_role="scheduling",
                model_name="m",
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.1,
            )
        units = {record.metric_name: record.metric_unit for record in records}
        assert units["model.cost.usd"] == "usd"
        assert units["model.tokens.input"] == "tokens"

    def test_the_model_name_is_a_dimension(self, emitter, local_tracer) -> None:
        """Two models are two series: a cost chart that merges them cannot answer which one is
        expensive, which is the only question a cost chart is asked."""
        with local_tracer.start_as_current_span("model.call"):
            records = emitter.model_usage(
                agent_role="scheduling",
                model_name="synthetic-model",
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.0,
                model_version="2026-05",
            )
        assert records[0].attributes["model.name"] == "synthetic-model"
        assert records[0].attributes["model.version"] == "2026-05"

    def test_a_second_call_in_one_span_is_a_second_measurement(self, emitter, local_tracer) -> None:
        """A retried model call happens inside the same span. Without the attempt discriminator the
        two would hash to one id and the token total would be half of what was spent."""
        with local_tracer.start_as_current_span("model.call"):
            first = emitter.model_usage(
                agent_role="scheduling",
                model_name="m",
                input_tokens=10,
                output_tokens=1,
                cost_usd=0.0,
                discriminator="call:1",
            )
            second = emitter.model_usage(
                agent_role="scheduling",
                model_name="m",
                input_tokens=10,
                output_tokens=1,
                cost_usd=0.0,
                discriminator="call:2",
            )
        assert first[0].event_id != second[0].event_id


class TestHitl:
    def test_wait_and_resolve_are_separate_measurements(
        self, emitter, recorder, local_tracer
    ) -> None:
        """A human's response time is not the platform's. Mixing them would put human thinking time
        into the p95 the §16 alert rules watch."""
        with local_tracer.start_as_current_span("hitl.wait"):
            emitter.hitl_wait(
                wait_ms=90_000, ticket_ref="ticket-1", workflow_state="AWAITING_HOSPITAL_CHOICE"
            )
            emitter.hitl_resolve(
                resolve_ms=30_000, resolution="chose_hospital", ticket_ref="ticket-1"
            )

        waits = recorder.by_metric("hitl.wait.ms")
        resolves = recorder.by_metric("hitl.resolve.ms")
        assert len(waits) == 1
        assert len(resolves) == 1
        assert waits[0]["attributes"]["workflow.state"] == "AWAITING_HOSPITAL_CHOICE"
        assert resolves[0]["attributes"]["hitl.resolution"] == "chose_hospital"

    def test_the_ticket_ref_is_a_field_not_a_dimension(self, emitter, local_tracer) -> None:
        """One ticket is one request; grouping by it yields one bucket per ticket."""
        from referraleasy_telemetry.conventions import DIMENSION_ATTRIBUTES

        with local_tracer.start_as_current_span("hitl.wait"):
            record = emitter.hitl_wait(wait_ms=1.0, ticket_ref="ticket-7")
        assert record.attributes["hitl.ticket_ref"] == "ticket-7"
        assert "hitl.ticket_ref" not in DIMENSION_ATTRIBUTES

    def test_a_wait_with_no_workflow_state_is_allowed(self, emitter, local_tracer) -> None:
        """A wait that began before the workflow reached a state is still a wait."""
        with local_tracer.start_as_current_span("hitl.wait"):
            record = emitter.hitl_wait(wait_ms=1.0)
        assert ATTR_METRIC_VALUE in record.attributes

    def test_a_long_wait_is_recorded_at_full_length(self, emitter, local_tracer) -> None:
        """A day-long wait is a real number in a real distribution, not a capped or dropped one."""
        with local_tracer.start_as_current_span("hitl.wait"):
            record = emitter.hitl_wait(wait_ms=86_400_000.0, ticket_ref="t")
        assert record.attributes[ATTR_METRIC_VALUE] == 86_400_000.0


class TestGuardrailDecisions:
    def test_a_block_is_blocked_and_not_an_error(self, emitter) -> None:
        """A guardrail doing its job is not a platform failure."""
        record = emitter.guardrail_decision(decision="block", stage="input", rule_id="pii-1")
        assert record.attributes[ATTR_OUTCOME_CLASS] == "blocked"
        assert record.severity is SeverityNumber.WARN
        assert record.outcome is not None and not record.outcome.is_error

    def test_an_allow_carries_no_outcome(self, emitter) -> None:
        """The outcome is a property of the *work*, and a guardrail allowing a message says nothing
        about whether the work succeeded."""
        record = emitter.guardrail_decision(decision="allow", stage="input")
        assert ATTR_OUTCOME_CLASS not in record.attributes

    def test_the_stage_and_rule_are_dimensions(self, emitter) -> None:
        record = emitter.guardrail_decision(decision="allow", stage="output", rule_id="r-9")
        assert record.attributes["guardrail.stage"] == "output"
        assert record.attributes["guardrail.rule_id"] == "r-9"


class TestEvaluationScores:
    def test_a_scored_evaluation_carries_its_number(self, emitter) -> None:
        record = emitter.eval_score(
            run_id="run-1", metric="faithfulness", score=0.87, status="scored"
        )
        assert record.attributes[ATTR_METRIC_VALUE] == 0.87
        assert record.attributes["eval.status"] == "scored"
        assert record.attributes["eval.run_id"] == "run-1"

    @pytest.mark.parametrize("status", ["not_applicable", "eval_error"])
    def test_an_unscoreable_case_is_zero_with_a_reason(self, emitter, status: str) -> None:
        """§14: N/A and eval_error are not zero. The value is 0 with a status naming why, and the
        query layer excludes those statuses from aggregates — so the case stays visible in the
        sample count without dragging an average down."""
        record = emitter.eval_score(
            run_id="run-1", metric="faithfulness", score=None, status=status
        )
        assert record.attributes[ATTR_METRIC_VALUE] == 0.0
        assert record.attributes["eval.status"] == status

    def test_scored_without_a_score_is_refused(self, emitter) -> None:
        with pytest.raises(ValueError, match="requires a score"):
            emitter.eval_score(run_id="r", metric="m", score=None, status="scored")

    def test_a_number_with_a_non_scored_status_is_refused(self, emitter) -> None:
        """A number with a non-scored status is exactly how N/A gets back into the distribution."""
        with pytest.raises(ValueError, match="must not carry a score"):
            emitter.eval_score(run_id="r", metric="m", score=0.0, status="not_applicable")

    def test_different_judges_are_never_merged(self, emitter) -> None:
        """§14: different judges and rubrics are separate series. The ``run_id:metric``
        discriminator alone would collide here, so the judge is part of what makes the id."""
        one = emitter.eval_score(
            run_id="r", metric="m", score=1.0, status="scored", judge="judge-a", rubric_version="v1"
        )
        two = emitter.eval_score(
            run_id="r", metric="m", score=1.0, status="scored", judge="judge-b", rubric_version="v1"
        )
        assert one.attributes["eval.judge"] != two.attributes["eval.judge"]
        assert one.event_id != two.event_id, "two judges are two measurements, not a duplicate"

    def test_two_cases_in_one_run_are_two_measurements(self, emitter) -> None:
        """The same collision one level down, and the one that reached a chart: a run scores many
        cases under one metric, and without the case in the discriminator all of them hash to a
        single ``event.id`` — so ``cardinality(event.id)``, which is §14's sample count, reports 1
        for a bucket holding ten."""
        one = emitter.eval_score(
            run_id="r",
            metric="m",
            score=1.0,
            status="scored",
            case_id="case/a",
            judge="rules",
            rubric_version="v1",
        )
        two = emitter.eval_score(
            run_id="r",
            metric="m",
            score=1.0,
            status="scored",
            case_id="case/b",
            judge="rules",
            rubric_version="v1",
        )
        assert one.event_id != two.event_id, "two cases are two measurements, not a duplicate"

    def test_the_same_case_measured_twice_is_one_event(self, emitter) -> None:
        """The other half: dedup still has to work. Re-emitting the same case — an at-least-once
        redelivery, a replayed run — derives the same id, which is what makes the copies harmless."""
        one = emitter.eval_score(
            run_id="r", metric="m", score=1.0, status="scored", case_id="case/a", judge="rules"
        )
        again = emitter.eval_score(
            run_id="r", metric="m", score=1.0, status="scored", case_id="case/a", judge="rules"
        )
        assert one.event_id == again.event_id


class TestFeedbackIsRaw:
    def test_a_rating_is_one_record_with_its_response_id(self, emitter) -> None:
        record = emitter.feedback(response_id="resp-1", value="up", discriminator="rating-1")
        assert record.attributes["feedback.response_id"] == "resp-1"
        assert record.attributes["feedback.value"] == "up"

    def test_the_response_id_is_a_field_not_a_dimension(self, emitter) -> None:
        """One response is one request. The last-active-thumbs rule is a query over these records,
        so the id has to be readable per record without being groupable."""
        from referraleasy_telemetry.conventions import DIMENSION_ATTRIBUTES

        record = emitter.feedback(response_id="resp-1", value="up", discriminator="rating-1")
        assert "feedback.response_id" not in DIMENSION_ATTRIBUTES
        assert record.attributes["feedback.response_id"] == "resp-1"

    def test_two_ratings_of_one_response_are_two_records(self, emitter, recorder) -> None:
        """The "last active thumbs per response_id" rule is a *query* property: this emitter is
        stateless and may not have seen the earlier rating, so collapsing here would be a guess."""
        emitter.feedback(response_id="resp-1", value="up", discriminator="rating-1")
        emitter.feedback(response_id="resp-1", value="down", discriminator="rating-2")
        assert len(recorder.by_event("feedback")) == 2
        assert (
            len({record["attributes"][ATTR_EVENT_ID] for record in recorder.by_event("feedback")})
            == 2
        )

    @pytest.mark.parametrize("value", ["like", "UP", "", "1"])
    def test_an_unknown_vote_is_refused(self, emitter, value: str) -> None:
        with pytest.raises(ValueError, match=r"up.*down"):
            emitter.feedback(response_id="r", value=value)


class TestStateTransition:
    def test_the_state_recorded_is_the_one_entered(self, emitter) -> None:
        record = emitter.state_transition(from_state="INTAKE", to_state="COVERAGE_CHECK")
        assert record.attributes["workflow.state"] == "COVERAGE_CHECK"
        assert record.attributes["event.action"] == "transition:INTAKE->COVERAGE_CHECK"

    def test_an_unknown_state_is_refused(self, emitter) -> None:
        with pytest.raises(UnknownDimensionError, match="known value"):
            emitter.state_transition(from_state="INTAKE", to_state="NOT_A_STATE")

    def test_two_different_transitions_into_one_state_are_two_events(self, emitter) -> None:
        one = emitter.state_transition(from_state="INTAKE", to_state="FAILED")
        two = emitter.state_transition(from_state="BOOKING_IN_FLIGHT", to_state="FAILED")
        assert one.event_id != two.event_id


class TestSyntheticDimension:
    def test_the_marker_travels_when_set(self, emitter, local_tracer) -> None:
        with local_tracer.start_as_current_span("turn"):
            record = emitter.agent_trigger(agent_role="scheduling", synthetic=Synthetic.SYNTHETIC)
        assert record.attributes[ATTR_SYNTHETIC] == "synthetic"

    def test_absent_means_the_live_demo(self, emitter, local_tracer) -> None:
        """Not a third state: the filter is synthetic-vs-live, and an unmarked record is live."""
        with local_tracer.start_as_current_span("turn"):
            record = emitter.agent_trigger(agent_role="scheduling")
        assert ATTR_SYNTHETIC not in record.attributes
        assert Synthetic.LIVE_DEMO == "live_demo"

    def test_the_live_marker_is_settable_explicitly(self, emitter, local_tracer) -> None:
        with local_tracer.start_as_current_span("turn"):
            record = emitter.agent_trigger(agent_role="scheduling", synthetic=Synthetic.LIVE_DEMO)
        assert record.attributes[ATTR_SYNTHETIC] == "live_demo"

    def test_an_invented_marker_is_refused(self, emitter, local_tracer) -> None:
        with local_tracer.start_as_current_span("turn"), pytest.raises(UnknownDimensionError):
            emitter.agent_trigger(agent_role="scheduling", synthetic="probably-synthetic")


class TestEventIdDedup:
    """The process-local half of dedup. The half that makes the *count* right is the query layer."""

    def test_a_repeat_is_reported_and_counted(self) -> None:
        dedup = EventIdDedup()
        assert dedup.seen("abc") is False
        assert dedup.seen("abc") is True
        assert dedup.duplicates == 1
        assert len(dedup) == 1

    def test_distinct_ids_are_not_duplicates(self) -> None:
        dedup = EventIdDedup()
        for value in ("a", "b", "c"):
            assert dedup.seen(value) is False
        assert dedup.duplicates == 0

    def test_the_set_is_bounded_and_evicts_oldest_first(self) -> None:
        """Unbounded, this is a slow leak in the one component that must never be the reason a
        booking fails."""
        dedup = EventIdDedup(capacity=3)
        for value in ("a", "b", "c", "d"):
            dedup.seen(value)
        assert len(dedup) == 3
        # "a" was evicted, so it is no longer recognised — an under-dedup, never a dropped unique
        # event, which is the safe direction for the error to point.
        assert dedup.seen("a") is False

    def test_recent_ids_survive_eviction(self) -> None:
        dedup = EventIdDedup(capacity=2)
        for value in ("a", "b", "c"):
            dedup.seen(value)
        assert dedup.seen("c") is True

    def test_repeating_an_id_refreshes_its_position(self) -> None:
        dedup = EventIdDedup(capacity=2)
        dedup.seen("a")
        dedup.seen("b")
        dedup.seen("a")  # moves "a" to the most-recent end
        dedup.seen("c")  # evicts "b"
        assert dedup.seen("a") is True

    @pytest.mark.parametrize("capacity", [0, -1])
    def test_a_nonsensical_capacity_is_refused(self, capacity: int) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            EventIdDedup(capacity=capacity)


class TestTheWritePathSetsATimestamp:
    def test_every_emitted_record_carries_a_timestamp(self, recorder, local_tracer) -> None:
        """The SDK leaves ``timestamp`` as ``None`` unless told, and a log record without one
        serialises to an ``int64`` the collector cannot parse — an HTTP 400 that discards the whole
        *batch*, not just that record."""
        emitter = TelemetryEmitter(recorder)
        with local_tracer.start_as_current_span("workflow"):
            emitter.agent_trigger(agent_role="scheduling")
            emitter.state_transition(from_state="INTAKE", to_state="BOOKED")
        assert recorder.records, "nothing was emitted"
        for record in recorder.records:
            assert record["timestamp"], "a record without a timestamp would lose the batch"
            assert isinstance(record["timestamp"], int)

    def test_the_attributes_are_a_copy(self, recorder, local_tracer) -> None:
        """The record is a frozen value object but its attribute map is a dict. Handing the same
        dict to the logger and to the caller lets one mutate the other."""
        emitter = TelemetryEmitter(recorder)
        with local_tracer.start_as_current_span("workflow"):
            record = emitter.agent_trigger(agent_role="scheduling")
        assert recorder.records[0]["attributes"] is not record.attributes

    def test_the_severity_reaches_the_logger(self, recorder, local_tracer) -> None:
        """Severity is what a log-based filter reads, so it has to survive the handover rather than
        being recomputed downstream from the outcome attribute."""
        emitter = TelemetryEmitter(recorder)
        with local_tracer.start_as_current_span("workflow"):
            emitter.attempt(
                agent_role="scheduling", duration_ms=1.0, number=1, outcome=OutcomeClass.ERROR
            )
        severities = {
            record["attributes"][ATTR_METRIC_NAME]: record["severity_number"]
            for record in recorder.records
        }
        assert severities["event.count"] is SeverityNumber.ERROR
        assert severities["latency.attempt.ms"] is SeverityNumber.ERROR

    def test_the_trace_id_of_the_ambient_span_is_attached(self, recorder, local_tracer) -> None:
        """The one path that makes a document navigable back to its trace."""
        emitter = TelemetryEmitter(recorder)
        with local_tracer.start_as_current_span("workflow") as span:
            record = emitter.agent_trigger(agent_role="scheduling")
        assert record.attributes["trace_id"] == f"{span.get_span_context().trace_id:032x}"
        assert record.attributes["span_id"] == f"{span.get_span_context().span_id:016x}"

    def test_the_event_id_is_derived_from_that_trace(self, recorder, local_tracer) -> None:
        """The end-to-end of the dedup: same span twice, same id; two spans, two ids."""
        emitter = TelemetryEmitter(recorder)
        with local_tracer.start_as_current_span("workflow"):
            first = emitter.agent_trigger(agent_role="scheduling")
            second = emitter.agent_trigger(agent_role="scheduling")
        with local_tracer.start_as_current_span("workflow"):
            third = emitter.agent_trigger(agent_role="scheduling")
        assert first.event_id == second.event_id, "one span, one logical trigger, one id"
        assert third.event_id != first.event_id, "two invocations are two triggers"

    def test_the_event_id_does_not_collide_across_measurements(
        self, recorder, local_tracer
    ) -> None:
        """The count, the latency and the outcome of one attempt are three documents. If they
        shared an id, ``cardinality(event.id)`` would count one and the attempt count would read
        one third of the truth."""
        emitter = TelemetryEmitter(recorder)
        with local_tracer.start_as_current_span("workflow"):
            records = emitter.attempt(agent_role="scheduling", duration_ms=1.0, number=1)
        assert len({record.event_id for record in records}) == 3
