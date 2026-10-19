# ruff: noqa: RUF002  # Turkish quoted from §15 of ReferralEasy_Architecture_v3.md is kept verbatim
"""Failure isolation: a telemetry outage must not take a booking down (§15).

    Collector bounded buffer/drop metriği gösterir; **telemetry kesintisi booking'i düşürmez.**
    Gerekli kritik iş audit'i PostgreSQL'de kalır.

The tests here are the ones that decide whether that sentence is true, and the two that matter most
are the ones that assert a *counter* rather than an absence of exceptions:

* :meth:`TestIsolatedLogger.test_a_validation_error_below_the_logger_is_counted` — the swallowing is
  only acceptable because it is visible.
* :meth:`TestTheGapAboveTheIsolatedLogger.test_the_emitter_can_still_raise_above_the_isolated_logger`
  — the honest boundary of the guarantee. ``IsolatedLogger`` wraps the logger; the emitter's own
  validation runs *before* the logger is called, so a caller that emits outside a span can still get
  an exception in a request path. That is a real gap and this file says so rather than rounding it up
  to "isolated".
"""

from __future__ import annotations

import pytest

from referraleasy_telemetry.isolation import (
    AUDIT_TABLES,
    REFUSAL_METRICS,
    CollectorDrops,
    DropCounter,
    IsolatedLogger,
    IsolatedTracer,
    assert_not_audit_path,
    guard_telemetry,
    health_report,
    outage_guard,
    read_collector_drops,
)


class TestDropCounter:
    def test_reasons_are_counted_separately(self) -> None:
        """The reasons have different fixes — an encode failure is this package's bug, a validation
        failure is the caller's, an export failure is the collector being unavailable. Collapsing
        them makes the counter useless for deciding what to do."""
        counter = DropCounter()
        counter.record("export")
        counter.record("export")
        counter.record("validation")
        assert counter.reasons == {"export": 2, "validation": 1}
        assert counter.total == 3

    def test_the_last_error_is_kept_and_truncated(self) -> None:
        counter = DropCounter()
        counter.record("export", RuntimeError("x" * 5000))
        assert counter.last_error is not None
        assert len(counter.last_error) <= 300
        assert counter.last_error.startswith("RuntimeError: ")

    def test_a_reason_without_an_exception_keeps_the_previous_message(self) -> None:
        counter = DropCounter()
        counter.record("export", RuntimeError("boom"))
        counter.record("export")
        assert counter.last_error is not None and "boom" in counter.last_error

    def test_the_dict_form_is_the_health_shape(self) -> None:
        assert set(DropCounter().as_dict()) == {"total", "reasons", "last_error"}
        assert DropCounter().as_dict()["total"] == 0


class TestIsolatedLogger:
    def test_a_healthy_emit_passes_through(self) -> None:
        seen: list[dict[str, object]] = []

        class Inner:
            def emit(self, **kwargs: object) -> None:
                seen.append(kwargs)

        IsolatedLogger(Inner()).emit(body="x", attributes={"a": 1})
        assert seen == [{"body": "x", "attributes": {"a": 1}}]

    def test_none_valued_arguments_are_not_passed_on(self) -> None:
        """The SDK distinguishes "not supplied" from "supplied as None" in places, and the emitter
        builds its call from optional fields."""
        seen: list[dict[str, object]] = []

        class Inner:
            def emit(self, **kwargs: object) -> None:
                seen.append(kwargs)

        IsolatedLogger(Inner()).emit(body="x", severity_text=None, timestamp=1)
        assert seen == [{"body": "x", "timestamp": 1}]

    def test_a_validation_error_below_the_logger_is_counted(self) -> None:
        """Swallowing is only acceptable because it is visible. An unreported drop is
        indistinguishable from a record that was never produced."""

        class Inner:
            def emit(self, **kwargs: object) -> None:
                raise TypeError("the SDK did not like that")

        drops = DropCounter()
        IsolatedLogger(Inner(), drops).emit(body="x")
        assert drops.total == 1
        assert drops.reasons["emission"] == 1

    def test_a_value_error_is_classified_as_validation(self) -> None:
        """A ValueError from below is the caller's mistake, not the transport's, and the fix is
        different."""

        class Inner:
            def emit(self, **kwargs: object) -> None:
                raise ValueError("bad attribute")

        drops = DropCounter()
        IsolatedLogger(Inner(), drops).emit(body="x")
        assert set(drops.reasons) == {"validation"}

    def test_an_unexpected_exception_type_is_still_swallowed(self) -> None:
        """The last mile cannot be selective about *ordinary* failures: an exception escaping here
        would surface in whichever booking request happened to be in flight. ``except Exception``
        catches a type nobody anticipated.
        """

        class UnusualError(Exception):
            pass

        class Inner:
            def emit(self, **kwargs: object) -> None:
                raise UnusualError("something nobody thought of")

        drops = DropCounter()
        IsolatedLogger(Inner(), drops).emit(body="x")
        assert drops.total == 1

    @pytest.mark.parametrize("escape", [KeyboardInterrupt, SystemExit])
    def test_a_keyboard_interrupt_is_not_swallowed(self, escape: type[BaseException]) -> None:
        """``except Exception`` and not ``except BaseException``, which is the right choice and was
        worth checking: a logger that caught ``KeyboardInterrupt`` would make a process that is
        emitting telemetry impossible to interrupt, and the operator's Ctrl-C would go nowhere.
        ``SystemExit`` is the same case.

        Parametrised rather than looped: a class defined inside a ``for`` body closes over the loop
        variable, so both copies of ``Inner`` would raise whatever ``escape`` ended up as. It works
        here by accident — each is used within its own iteration — and a parameter is the version
        that cannot stop working.
        """

        class Inner:
            def emit(self, **kwargs: object) -> None:
                raise escape

        drops = DropCounter()
        with pytest.raises(escape):
            IsolatedLogger(Inner(), drops).emit(body="x")
        assert drops.total == 0, "an interrupt is not a telemetry drop"

    def test_it_has_its_own_counter_when_none_is_given(self) -> None:
        """Used by hand rather than by ``configure_telemetry``, the wrapper must still be
        observable."""
        logger = IsolatedLogger(object())
        logger.emit(body="x")
        assert logger.drops.total == 1


class TestIsolatedTracer:
    def test_a_working_tracer_passes_through(self) -> None:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider(resource=Resource.create({"service.name": "t"}))
        try:
            span = IsolatedTracer(provider.get_tracer("t")).start_span("x")
            assert span.get_span_context().is_valid
            span.end()
        finally:
            provider.shutdown()

    def test_a_failing_tracer_yields_an_invalid_span(self) -> None:
        """The caller still gets a span object, so the code below it does not need a branch — and
        the drop is counted rather than silent."""

        class Exploding:
            def start_span(self, name: str, **kwargs: object) -> object:
                raise RuntimeError("sampler exploded")

        drops = DropCounter()
        span = IsolatedTracer(Exploding(), drops).start_span("x")
        assert not span.get_span_context().is_valid
        assert drops.reasons["span_start"] == 1


class TestGuardTelemetry:
    def test_a_working_call_returns_its_value(self) -> None:
        @guard_telemetry
        def emit() -> str:
            return "ok"

        assert emit() == "ok"

    def test_a_failing_call_returns_none(self) -> None:
        @guard_telemetry
        def emit() -> str:
            raise RuntimeError("collector gone")

        assert emit() is None

    def test_the_name_and_docstring_survive(self) -> None:
        """A decorator that erases them turns every traceback into ``guarded``."""

        @guard_telemetry
        def emit() -> None:
            """Records one attempt."""

        assert emit.__name__ == "emit"
        assert emit.__doc__ == "Records one attempt."


class TestOutageGuard:
    def test_the_block_completes_normally(self) -> None:
        with outage_guard():
            value = 1
        assert value == 1

    def test_a_failure_inside_the_block_is_absorbed_and_counted(self) -> None:
        drops = DropCounter()
        with outage_guard(drops):
            raise RuntimeError("boom")
        assert drops.reasons["guarded_block"] == 1

    def test_the_reason_is_configurable(self) -> None:
        drops = DropCounter()
        with outage_guard(drops, reason="booking_emit"):
            raise RuntimeError("boom")
        assert set(drops.reasons) == {"booking_emit"}

    def test_code_after_the_guard_still_runs(self) -> None:
        """The point of a context manager rather than a decorator: the protected region is usually
        *part* of a function. Wrapping the whole handler would swallow the booking write too."""
        reached = False
        with outage_guard(DropCounter()):
            raise RuntimeError("telemetry only")
        reached = True
        assert reached


class TestTheGapAboveTheIsolatedLogger:
    """Where the isolation guarantee stops. Tested so it is a known boundary, not a discovery."""

    def test_the_emitter_can_still_raise_above_the_isolated_logger(self) -> None:
        """``IsolatedLogger`` wraps the logger, and the emitter validates *before* it calls the
        logger — so ``agent_trigger`` outside any span raises ``MissingEventIdentityError`` straight
        into the caller's stack, where the wrapper cannot see it.

        This is deliberate: the alternative is to invent an identity, which would destroy the dedup
        the whole package rests on. But it means a caller on a path that must not fail — the booking
        gate §15 names — has to wrap the *emission*, not just configure the logger::

            with outage_guard(drops, reason="booking_emit"):
                emitter.state_transition(from_state=..., to_state="BOOKED")

        Recorded here rather than fixed, because the fix belongs in how a service adopts this
        package, which is where the seam documentation points.
        """
        from referraleasy_telemetry.events import MissingEventIdentityError, TelemetryEmitter

        emitter = TelemetryEmitter(IsolatedLogger(object()))
        with pytest.raises(MissingEventIdentityError):
            emitter.agent_trigger(agent_role="scheduling")

    def test_the_guard_closes_that_gap(self) -> None:
        """What the adoption seam must do, demonstrated."""
        from referraleasy_telemetry.events import TelemetryEmitter

        drops = DropCounter()
        emitter = TelemetryEmitter(IsolatedLogger(object(), drops))
        with outage_guard(drops, reason="booking_emit"):
            emitter.agent_trigger(agent_role="scheduling")
        assert drops.reasons.get("booking_emit") == 1

    def test_a_validation_error_raised_above_the_logger_is_also_caught_by_the_guard(self) -> None:
        from referraleasy_telemetry.events import TelemetryEmitter

        drops = DropCounter()
        emitter = TelemetryEmitter(IsolatedLogger(object(), drops))
        with outage_guard(drops):
            emitter.agent_trigger(agent_role="not_a_role")
        assert drops.total == 1


class TestTheAuditStaysInPostgres:
    """§15: "Gerekli kritik iş audit'i PostgreSQL'de kalır.\""""

    @pytest.mark.parametrize("name", sorted(AUDIT_TABLES))
    def test_an_audit_table_is_refused(self, name: str) -> None:
        with pytest.raises(ValueError, match="audit table"):
            assert_not_audit_path(name)

    @pytest.mark.parametrize("name", ["public.audit", '"audit"', "  AUDIT  ", "audit_log"])
    def test_the_check_survives_schema_quoting_and_case(self, name: str) -> None:
        """A guard that a quoted or schema-qualified name walks past is not a guard. ``audit_log``
        is a different table and is allowed, which is the boundary of what the check can know."""
        if name.strip().strip('"').lower().endswith("audit_log"):
            assert_not_audit_path(name)
        else:
            with pytest.raises(ValueError):
                assert_not_audit_path(name)

    @pytest.mark.parametrize(
        "target",
        ["otel-events-2026.10.15", "referraleasy.telemetry", "otel-traces", "bookings"],
    )
    def test_a_telemetry_target_is_allowed(self, target: str) -> None:
        assert_not_audit_path(target)

    def test_the_refusal_explains_why(self) -> None:
        with pytest.raises(ValueError) as caught:
            assert_not_audit_path("audit")
        message = str(caught.value)
        assert "PostgreSQL" in message
        assert "drop" in message.lower()


class TestCollectorDrops:
    def test_an_unreachable_endpoint_is_not_a_healthy_zero(self) -> None:
        """The distinction the ``available`` flag exists for: an idle healthy collector and an
        unreachable one both look like "no series", and a dashboard showing 0 for both is lying."""
        drops = read_collector_drops("http://127.0.0.1:1", timeout=0.2)
        assert drops.available is False
        assert drops.error is not None
        assert drops.total_refused == 0

    def test_the_metrics_endpoint_is_read_when_it_is_there(self) -> None:
        import httpx

        body = "\n".join(
            [
                "# HELP otelcol_processor_memory_limiter_refused_spans refused",
                'otelcol_processor_memory_limiter_refused_spans{processor="batch"} 3.0',
                'otelcol_receiver_refused_log_records{transport="http"} 2.0',
                'otelcol_exporter_sent_spans{exporter="otlp"} 99.0',
            ]
        )
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
        )
        drops = read_collector_drops("http://collector:8888", client=client)

        assert drops.available is True
        assert drops.counters["otelcol_processor_memory_limiter_refused_spans"] == 3.0
        assert drops.counters["otelcol_receiver_refused_log_records"] == 2.0
        # A successful-send counter is not a drop, and adding it would make the drop number
        # increase every time telemetry worked.
        assert drops.total_refused == 5.0

    def test_labelled_series_are_summed(self) -> None:
        import httpx

        body = "\n".join(
            [
                'otelcol_processor_memory_limiter_refused_spans{processor="a"} 1.0',
                'otelcol_processor_memory_limiter_refused_spans{processor="b"} 4.0',
            ]
        )
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
        )
        drops = read_collector_drops("http://collector:8888", client=client)
        assert drops.counters["otelcol_processor_memory_limiter_refused_spans"] == 5.0

    def test_an_http_error_is_unavailable(self) -> None:
        import httpx

        client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503)))
        drops = read_collector_drops("http://collector:8888", client=client)
        assert drops.available is False
        assert drops.error == "HTTP 503"

    def test_the_metric_names_are_the_prefixed_ones_the_config_measured(self) -> None:
        """The collector config records that on 0.162.0 the short ``otelcol_processor_refused_spans``
        does not exist. Reading the name the config file *expected* would report zero forever.
        """
        assert "otelcol_processor_memory_limiter_refused_spans" in REFUSAL_METRICS
        assert "otelcol_processor_refused_spans" not in REFUSAL_METRICS
        assert all(name.startswith("otelcol_") for name in REFUSAL_METRICS)

    def test_the_dict_form_names_the_availability(self) -> None:
        assert CollectorDrops(available=False).as_dict()["available"] is False


class TestHealthReport:
    def test_it_reports_both_layers(self) -> None:
        report = health_report(DropCounter(), collector=CollectorDrops(available=True))
        assert set(report) == {"process_drops", "collector"}

    def test_it_defaults_to_nothing_known_rather_than_all_clear(self) -> None:
        """A default of "healthy" would make a missing caller look like a healthy process."""
        report = health_report()
        assert report["process_drops"]["total"] == 0
        assert report["collector"]["available"] is False
