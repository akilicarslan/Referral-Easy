"""The OTLP/HTTP JSON encoder and transport.

**This file is the weaker of the two verifications and it says so.** Every test here stops at the
HTTP boundary: a ``MockTransport`` stands in for the collector, so what is proven is that the
*payload* is a well-formed OTLP request with the right field names, the right integer widths and the
right enum values. What is **not** proven here is that the collector accepts it, that Data Prepper
maps it, or that the document is searchable — a mocked exporter proves the serialisation and not the
pipeline.

The stronger test is ``test_pipeline_live.py``, which posts the same payloads at the real collector
and reads the documents back out of OpenSearch with ``curl``-equivalent requests. Several bugs
recorded in this file were found by *that* test and only reproduced here afterwards: the ``SpanKind``
offset, the ``"None"`` timestamp that loses a whole batch, and the ``resource`` attribute that lives
on the wrapper rather than on the record. Where a test below exists because of a live finding, its
docstring says so.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from referraleasy_telemetry.export import (
    LOGS_PATH,
    TRACES_PATH,
    ExportStats,
    OTLPHTTPJSONLogExporter,
    OTLPHTTPJSONSpanExporter,
    encode_attributes,
    encode_log_record,
    encode_log_records,
    encode_span,
    encode_spans,
    encode_value,
    payload_size,
)


def _handler_recording(seen: list[httpx.Request], *, status: int = 200):
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    return handle


@pytest.fixture
def client_factory():
    """Build an ``httpx.Client`` backed by a handler, for the transport tests."""

    def build(seen: list[httpx.Request], *, status: int = 200) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(_handler_recording(seen, status=status)))

    return build


class TestProtobufJSONValueEncoding:
    """OTLP JSON is protobuf-JSON, not a free-form JSON dialect. These are the rules."""

    def test_an_int64_is_a_string(self) -> None:
        """JSON numbers are doubles, so a nanosecond timestamp would lose precision past 2^53."""
        assert encode_value(1) == {"intValue": "1"}
        assert encode_value(2**62) == {"intValue": str(2**62)}

    def test_a_bool_is_a_bool_and_not_an_int(self) -> None:
        """``True`` is an ``int`` in Python. Checking ``int`` first would emit ``{"intValue": "1"}``
        for a boolean attribute and the field's type would change under a query."""
        assert encode_value(True) == {"boolValue": True}
        assert encode_value(False) == {"boolValue": False}

    def test_a_float_stays_a_number(self) -> None:
        """``metric.value`` is the field every §15 percentile aggregates over, so it must not
        arrive as a string."""
        assert encode_value(1.5) == {"doubleValue": 1.5}
        assert encode_value(0.0) == {"doubleValue": 0.0}

    def test_a_string_is_a_string(self) -> None:
        assert encode_value("x") == {"stringValue": "x"}

    def test_bytes_are_base64(self) -> None:
        assert encode_value(b"\x00\x01") == {"bytesValue": base64.b64encode(b"\x00\x01").decode()}

    def test_a_mapping_becomes_a_kvlist(self) -> None:
        encoded = encode_value({"a": 1})
        assert encoded == {"kvlistValue": {"values": [{"key": "a", "value": {"intValue": "1"}}]}}

    def test_a_list_becomes_an_array(self) -> None:
        assert encode_value([1, "a"]) == {
            "arrayValue": {"values": [{"intValue": "1"}, {"stringValue": "a"}]}
        }

    def test_an_unrepresentable_value_is_stringified_not_dropped(self) -> None:
        """A producer bug should be visible in the index rather than produce a silent absence."""

        class Odd:
            def __str__(self) -> str:
                return "odd"

        assert encode_value(Odd()) == {"stringValue": "odd"}

    def test_attributes_are_key_value_pairs_in_order(self) -> None:
        encoded = encode_attributes({"b": 2, "a": 1})
        assert [pair["key"] for pair in encoded] == ["b", "a"]

    def test_no_attributes_is_an_empty_list(self) -> None:
        assert encode_attributes(None) == []
        assert encode_attributes({}) == []


class TestSpanEncoding:
    def test_the_otlp_span_kind_is_not_the_python_enum_value(self, span_factory) -> None:
        """**Found live.** ``SpanKind.INTERNAL == 0`` in the Python API, but on the OTLP wire
        ``SPAN_KIND_UNSPECIFIED == 0`` and ``SPAN_KIND_INTERNAL == 1``. Sending ``member.value``
        mislabelled every span by one — and invisibly, because a unit test asserting "kind is an
        int" passes either way. It was caught by reading a document back out of OpenSearch and
        seeing ``"kind": "SPAN_KIND_UNSPECIFIED"`` on a span that was plainly internal.
        """
        from opentelemetry.trace import SpanKind

        expected = {
            SpanKind.INTERNAL: 1,
            SpanKind.SERVER: 2,
            SpanKind.CLIENT: 3,
            SpanKind.PRODUCER: 4,
            SpanKind.CONSUMER: 5,
        }
        for kind, wire in expected.items():
            span = span_factory(kind=kind)
            assert encode_span(span)["kind"] == wire, f"{kind} encoded as the wrong wire enum"

    def test_the_ids_are_lowercase_hex_of_the_right_width(self, span_factory) -> None:
        span = span_factory()
        encoded = encode_span(span)
        assert len(encoded["traceId"]) == 32
        assert len(encoded["spanId"]) == 16
        assert encoded["traceId"] == f"{span.context.trace_id:032x}"
        assert encoded["spanId"] == f"{span.context.span_id:016x}"
        assert encoded["traceId"] == encoded["traceId"].lower()

    def test_a_root_span_omits_the_parent_field(self, span_factory) -> None:
        """An absent ``parentSpanId`` is a root span. A run of zeros would be a *link to a
        non-existent span* rather than no parent — measured: OTLP omits it."""
        assert "parentSpanId" not in encode_span(span_factory())

    def test_a_child_span_carries_its_parent(self, span_factory) -> None:
        parent = span_factory(name="parent")
        child = span_factory(name="child", parent=parent)

        encoded_child = encode_span(child)
        assert encoded_child["parentSpanId"] == f"{parent.context.span_id:016x}"
        assert encoded_child["parentSpanId"] != encoded_child["spanId"]
        assert encoded_child["traceId"] == f"{parent.context.trace_id:032x}"

    def test_the_timestamps_are_strings(self, span_factory) -> None:
        encoded = encode_span(span_factory())
        assert isinstance(encoded["startTimeUnixNano"], str)
        assert isinstance(encoded["endTimeUnixNano"], str)
        assert int(encoded["endTimeUnixNano"]) >= int(encoded["startTimeUnixNano"])

    def test_the_status_code_is_the_otlp_integer(self, span_factory) -> None:
        from opentelemetry.trace import Status, StatusCode

        assert encode_span(span_factory())["status"]["code"] == 0
        ok = span_factory(status=Status(StatusCode.OK))
        error = span_factory(status=Status(StatusCode.ERROR, "boom"))
        assert encode_span(ok)["status"]["code"] == 1
        assert encode_span(error)["status"] == {"code": 2, "message": "boom"}

    def test_events_and_links_are_encoded(self, span_factory) -> None:
        from opentelemetry.trace import Link

        other = span_factory(name="other")
        span = span_factory(
            events=[("resumed", {"link.reason": "x"})],
            links=[Link(other.context, attributes={"link.reason": "resumed_after_wait"})],
        )
        encoded = encode_span(span)
        assert encoded["events"][0]["name"] == "resumed"
        assert encoded["links"][0]["traceId"] == f"{other.context.trace_id:032x}"
        assert encoded["links"][0]["spanId"] == f"{other.context.span_id:016x}"

    def test_the_span_name_and_scope_survive(self, span_factory) -> None:
        encoded = encode_span(span_factory(name="agent.scheduling.invoke"))
        assert encoded["name"] == "agent.scheduling.invoke"
        assert encoded["droppedAttributesCount"] == 0

    def test_an_attribute_is_masked_before_it_is_encoded(self, span_factory) -> None:
        span = span_factory(attributes={"authorization": "Bearer secret"})
        assert encode_span(span)["attributes"] == [
            {"key": "authorization", "value": {"stringValue": "[REDACTED]"}}
        ]


class TestSpanRequestAssembly:
    def test_a_request_wraps_resource_then_scope_then_spans(self, span_factory) -> None:
        payload = encode_spans([span_factory(name="one"), span_factory(name="two")])
        (resource_spans,) = payload["resourceSpans"]
        (scope_spans,) = resource_spans["scopeSpans"]
        assert scope_spans["scope"]["name"] == "referraleasy.telemetry.tests"
        assert scope_spans["scope"]["version"] == "0.1.0"
        assert [span["name"] for span in scope_spans["spans"]] == ["one", "two"]

    def test_the_resource_carries_the_service_name(self, span_factory) -> None:
        payload = encode_spans([span_factory()])
        pairs = {
            pair["key"]: pair["value"]
            for pair in payload["resourceSpans"][0]["resource"]["attributes"]
        }
        assert pairs["service.name"] == {"stringValue": "telemetry-tests"}

    def test_spans_from_two_providers_are_two_resource_groups(self, span_factory) -> None:
        """OTLP groups by resource; a flat list is not a valid request body."""
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        other_exporter = InMemorySpanExporter()
        other_provider = TracerProvider(resource=Resource.create({"service.name": "other"}))
        other_provider.add_span_processor(SimpleSpanProcessor(other_exporter))
        try:
            with other_provider.get_tracer("other").start_as_current_span("s"):
                pass
            payload = encode_spans([span_factory(), other_exporter.get_finished_spans()[0]])
        finally:
            other_provider.shutdown()
        assert len(payload["resourceSpans"]) == 2

    def test_no_spans_is_an_empty_request_not_a_missing_key(self) -> None:
        assert encode_spans([]) == {"resourceSpans": []}


class TestLogRecordEncoding:
    def test_the_timestamp_falls_back_to_the_observed_one(self, log_record_factory) -> None:
        """**Found live.** The SDK leaves ``timestamp`` as ``None`` when the producer does not pass
        one, and ``str(None)`` serialised to ``timeUnixNano: "None"`` — which the collector answers
        with an HTTP 400, discarding the entire *batch* and not just that record. The batch was
        silently losing ten records at a time. This test pins the fallback.
        """
        record = log_record_factory(body="no explicit timestamp")
        assert record.log_record.timestamp is None, "the SDK set one; this test proves nothing now"
        encoded = encode_log_record(record)
        assert encoded["timeUnixNano"] != "None"
        assert int(encoded["timeUnixNano"]) > 0
        assert encoded["timeUnixNano"] == encoded["observedTimeUnixNano"]

    def test_an_explicit_timestamp_is_used(self, log_record_factory) -> None:
        record = log_record_factory(timestamp=1_700_000_000_000_000_000)
        assert encode_log_record(record)["timeUnixNano"] == "1700000000000000000"

    def test_severity_text_is_derived_when_the_sdk_leaves_it_none(self, log_record_factory) -> None:
        """**Found by asserting the value rather than its type.** The SDK leaves ``severityText`` as
        ``None`` because only ``severity_number`` is passed, and the encoder's own
        ``int(severity_number)`` raised ``TypeError`` because ``SeverityNumber`` is a plain ``Enum``
        and not an ``IntEnum``. The exception went into a "defensive" ``except`` that returned 0, so
        every document reached OpenSearch with ``severityNumber: 0`` and an empty ``severityText``
        — and the error/warning distinction the emitter computes from the outcome class was thrown
        away one step before the wire, with nothing failing.
        """
        from opentelemetry._logs import SeverityNumber

        assert not issubclass(SeverityNumber, __import__("enum").IntEnum), (
            "if this ever becomes an IntEnum the bug cannot recur; the test still pins the value"
        )
        record = log_record_factory(severity=SeverityNumber.ERROR)
        encoded = encode_log_record(record)
        assert encoded["severityNumber"] == SeverityNumber.ERROR.value == 17
        assert encoded["severityText"] == "ERROR"

    def test_the_severity_survives_every_member_of_the_enum(self, log_record_factory) -> None:
        from opentelemetry._logs import SeverityNumber

        for member in SeverityNumber:
            encoded = encode_log_record(log_record_factory(severity=member))
            assert encoded["severityNumber"] == member.value, (
                f"{member} was encoded as the wrong number"
            )
            if member.value == 0:
                # ``SEVERITY_NUMBER_UNSPECIFIED`` is deliberately given no text. A filter on
                # ``severityText: "WARN"`` that started matching unspecified records would be
                # worse than one that matches nothing, and the number 0 is still on the document
                # for anyone who wants to find them.
                assert encoded["severityText"] == ""
            else:
                assert encoded["severityText"] == member.name

    def test_a_record_without_a_severity_encodes_as_unspecified(self, log_record_factory) -> None:
        encoded = encode_log_record(log_record_factory(severity=None))
        assert encoded["severityNumber"] == 0

    def test_the_body_is_encoded_as_a_value(self, log_record_factory) -> None:
        assert encode_log_record(log_record_factory(body="hello"))["body"] == {
            "stringValue": "hello"
        }

    def test_the_body_is_masked(self, log_record_factory) -> None:
        encoded = encode_log_record(log_record_factory(body="wrote to jane.doe@example.org"))
        assert "jane.doe" not in json.dumps(encoded)
        assert encoded["body"]["stringValue"].endswith("[REDACTED]")

    def test_an_empty_body_stays_an_empty_value(self, log_record_factory) -> None:
        """``body`` is not omitted: a record with no body is still a record, and an empty
        ``stringValue`` is a different thing from a missing field."""
        assert encode_log_record(log_record_factory(body=None))["body"] == {"stringValue": ""}

    def test_the_trace_context_is_attached_when_present(self, log_record_factory) -> None:
        record = log_record_factory(trace_id=int("a" * 32, 16), span_id=int("b" * 16, 16))
        encoded = encode_log_record(record)
        assert encoded["traceId"] == "a" * 32
        assert encoded["spanId"] == "b" * 16

    def test_a_record_outside_a_trace_omits_the_ids(self, log_record_factory) -> None:
        encoded = encode_log_record(log_record_factory())
        assert "traceId" not in encoded
        assert "spanId" not in encoded

    def test_the_resource_comes_from_the_wrapper_not_the_inner_record(
        self, log_record_factory
    ) -> None:
        """**Found live:** ``AttributeError: 'LogRecord' object has no attribute 'resource'``. The
        ``ReadableLogRecord`` has one; the ``LogRecord`` it wraps does not.
        """
        record = log_record_factory()
        assert hasattr(record, "resource")
        assert not hasattr(record.log_record, "resource")
        pairs = {
            pair["key"]: pair["value"]
            for pair in encode_log_records([record])["resourceLogs"][0]["resource"]["attributes"]
        }
        assert pairs["service.name"] == {"stringValue": "telemetry-tests"}

    def test_the_request_is_grouped_by_scope(self, log_record_factory) -> None:
        payload = encode_log_records([log_record_factory(body="a"), log_record_factory(body="b")])
        (resource_logs,) = payload["resourceLogs"]
        (scope_logs,) = resource_logs["scopeLogs"]
        assert scope_logs["scope"]["name"] == "referraleasy.telemetry.tests"
        assert len(scope_logs["logRecords"]) == 2

    def test_the_attributes_are_masked(self, log_record_factory) -> None:
        encoded = encode_log_record(log_record_factory(attributes={"patient.email": "a@b.co"}))
        assert encoded["attributes"] == [
            {"key": "patient.email", "value": {"stringValue": "[REDACTED]"}}
        ]


class TestExportStatsAndMaskingCounters:
    def test_the_stats_start_at_zero(self) -> None:
        stats = ExportStats()
        assert (stats.attempted, stats.exported, stats.failed, stats.masked) == (0, 0, 0, 0)
        assert stats.dropped == 0

    def test_masking_is_counted_when_a_span_is_encoded(self, span_factory) -> None:
        stats = ExportStats()
        encode_span(span_factory(attributes={"authorization": "Bearer x"}), stats=stats)
        assert stats.masked == 1

    def test_an_unmasked_span_does_not_increment_the_counter(self, span_factory) -> None:
        """Otherwise the counter is just "spans seen" and tells a reader nothing."""
        stats = ExportStats()
        encode_span(span_factory(attributes={"agent.role": "scheduling"}), stats=stats)
        assert stats.masked == 0

    def test_the_dict_form_is_the_health_report_shape(self) -> None:
        assert set(ExportStats().as_dict()) == {
            "attempted",
            "exported",
            "failed",
            "masked",
            "last_error",
        }


class TestTheTransport:
    """The transport's contract is that it **never raises**, whatever the network does."""

    def test_a_payload_is_posted_to_the_signal_path(self, client_factory) -> None:
        seen: list[httpx.Request] = []
        transport = _transport(client_factory(seen))
        assert transport.post({"a": 1}, TRACES_PATH, ExportStats()) is True
        assert str(seen[0].url) == "http://collector:4318/v1/traces"
        assert seen[0].headers["content-type"] == "application/json"

    def test_the_two_signals_have_the_paths_the_collector_listens_on(self) -> None:
        assert TRACES_PATH == "/v1/traces"
        assert LOGS_PATH == "/v1/logs"

    @pytest.mark.parametrize("status", [400, 429, 500, 503])
    def test_a_rejection_is_a_failure_and_is_recorded(self, client_factory, status: int) -> None:
        """A 503 is the collector's memory limiter refusing data. §15 wants that to be a visible
        drop, not a silent one, so the status is kept on the stats."""
        stats = ExportStats()
        transport = _transport(client_factory([], status=status))
        assert transport.post({"a": 1}, TRACES_PATH, stats) is False
        assert stats.failed == 1
        assert f"HTTP {status}" in (stats.last_error or "")

    def test_a_connection_error_is_swallowed(self) -> None:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        stats = ExportStats()
        transport = _transport(httpx.Client(transport=httpx.MockTransport(explode)))
        assert transport.post({"a": 1}, TRACES_PATH, stats) is False
        assert stats.failed == 1
        assert "ConnectError" in (stats.last_error or "")

    def test_a_timeout_is_swallowed(self) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        stats = ExportStats()
        transport = _transport(httpx.Client(transport=httpx.MockTransport(slow)))
        assert transport.post({"a": 1}, TRACES_PATH, stats) is False
        assert stats.failed == 1

    def test_the_default_timeout_is_bounded_and_small(self) -> None:
        """The collector is on the same Docker network. Anything slower than this is a collector
        that is down, and waiting longer only delays the request the exporter was called from —
        §15's "telemetry kesintisi booking'i düşürmez" is only true if the failure is fast."""
        from referraleasy_telemetry.export import DEFAULT_TIMEOUT

        assert DEFAULT_TIMEOUT.connect is not None and DEFAULT_TIMEOUT.connect <= 1.0
        assert DEFAULT_TIMEOUT.read is not None and DEFAULT_TIMEOUT.read <= 2.0

    def test_an_error_message_is_truncated(self, client_factory) -> None:
        """A 200KB HTML error page must not end up in every log line and in the health report."""
        stats = ExportStats()
        stats.last_error = "x" * 5000
        assert len(stats.as_dict()["last_error"]) == 5000  # the dataclass does not truncate
        transport = _transport(client_factory([], status=500))
        transport.post({"a": 1}, TRACES_PATH, stats)
        assert len(stats.last_error or "") <= 300

    def test_the_endpoint_trailing_slash_is_normalised(self) -> None:
        transport = _transport(None, endpoint="http://collector:4318/")
        assert transport.endpoint == "http://collector:4318"


def _transport(client: httpx.Client | None, endpoint: str = "http://collector:4318"):
    from referraleasy_telemetry.export import _OTLPHTTPTransport

    return _OTLPHTTPTransport(endpoint, client=client)


class TestTheExporters:
    """What a caller actually attaches to a provider. Still mocked — see the module docstring."""

    def test_the_span_exporter_reports_success_and_counts_what_it_sent(
        self, span_factory, client_factory
    ) -> None:
        from opentelemetry.sdk.trace.export import SpanExportResult

        seen: list[httpx.Request] = []
        exporter = OTLPHTTPJSONSpanExporter("http://collector:4318", client=client_factory(seen))
        result = exporter.export([span_factory(), span_factory()])

        assert result is SpanExportResult.SUCCESS
        assert (exporter.stats.attempted, exporter.stats.exported, exporter.stats.failed) == (
            2,
            2,
            0,
        )
        body = json.loads(seen[0].content)
        assert len(body["resourceSpans"][0]["scopeSpans"][0]["spans"]) == 2

    def test_the_span_exporter_reports_failure_without_raising(
        self, span_factory, client_factory
    ) -> None:
        from opentelemetry.sdk.trace.export import SpanExportResult

        exporter = OTLPHTTPJSONSpanExporter(
            "http://collector:4318", client=client_factory([], status=503)
        )
        assert exporter.export([span_factory()]) is SpanExportResult.FAILURE
        assert exporter.stats.failed == 1
        assert exporter.stats.exported == 0

    def test_the_log_exporter_reports_success(self, log_record_factory, client_factory) -> None:
        # `LogRecordExportResult`, not the deprecated `LogExportResult`: in this SDK version those
        # are two distinct enum classes with equal members, and asserting on the wrong one is what
        # let the exporter return the wrong one. See TestTheExportersAreSDKExporters below.
        from opentelemetry.sdk._logs.export import LogRecordExportResult

        seen: list[httpx.Request] = []
        exporter = OTLPHTTPJSONLogExporter("http://collector:4318", client=client_factory(seen))
        assert exporter.export([log_record_factory()]) is LogRecordExportResult.SUCCESS
        assert exporter.stats.exported == 1

    def test_the_log_exporter_reports_failure_without_raising(
        self, log_record_factory, client_factory
    ) -> None:
        from opentelemetry.sdk._logs.export import LogRecordExportResult

        exporter = OTLPHTTPJSONLogExporter(
            "http://collector:4318", client=client_factory([], status=400)
        )
        assert exporter.export([log_record_factory()]) is LogRecordExportResult.FAILURE
        assert exporter.stats.failed == 1

    def test_an_encoding_error_is_a_failure_not_an_exception(self, client_factory) -> None:
        """A span whose attributes cannot be encoded must not raise into the batch processor's
        thread, where the traceback would go nowhere useful."""
        from opentelemetry.sdk.trace.export import SpanExportResult

        exporter = OTLPHTTPJSONSpanExporter("http://collector:4318", client=client_factory([]))

        class Hostile:
            @property
            def resource(self):
                raise RuntimeError("nope")

        assert exporter.export([Hostile()]) is SpanExportResult.FAILURE
        assert exporter.stats.failed == 1
        assert "encode" in (exporter.stats.last_error or "")

    def test_the_endpoint_property_names_the_full_url(self, client_factory) -> None:
        """If this is wrong the OTLP receiver answers 404 and the drop is silent."""
        exporter = OTLPHTTPJSONSpanExporter("http://localhost:4318", client=client_factory([]))
        assert exporter.endpoint == "http://localhost:4318/v1/traces"

    def test_force_flush_is_immediate(self, client_factory) -> None:
        """There is nothing buffered in this exporter — the SDK's processor owns the queue — so a
        caller asking to flush is already satisfied. Returning False would make a shutdown look
        like it lost data."""
        exporter = OTLPHTTPJSONSpanExporter("http://collector:4318", client=client_factory([]))
        assert exporter.force_flush() is True

    def test_both_exporters_accept_a_custom_masking_policy(
        self, span_factory, client_factory
    ) -> None:
        from referraleasy_telemetry.masking import REDACTED, MaskingPolicy

        seen: list[httpx.Request] = []
        exporter = OTLPHTTPJSONSpanExporter(
            "http://collector:4318",
            client=client_factory(seen),
            policy=MaskingPolicy(redact_keys=frozenset({"internal.note"})),
        )
        exporter.export([span_factory(attributes={"internal.note": "secret"})])
        body = json.loads(seen[0].content)
        assert body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"] == [
            {"key": "internal.note", "value": {"stringValue": REDACTED}}
        ]


class TestTheExportersAreSDKExporters:
    """The interface, not the payload: what the SDK's processors accept, and what they get back.

    These exist because the exporters used to duck-type their base classes — the span exporter's
    docstring said so in as many words ("satisfies ``SpanExporter`` structurally, without
    subclassing it"). That passed every test in this file and hid two things ``mypy`` then found:
    the log exporter was returning the *deprecated* result enum, and neither class was a registered
    implementation of the protocol it claimed to satisfy.
    """

    def _span_exporter(self, client_factory) -> OTLPHTTPJSONSpanExporter:
        return OTLPHTTPJSONSpanExporter("http://collector:4318", client=client_factory([]))

    def _log_exporter(self, client_factory) -> OTLPHTTPJSONLogExporter:
        return OTLPHTTPJSONLogExporter("http://collector:4318", client=client_factory([]))

    def test_the_span_exporter_is_an_sdk_span_exporter(self, client_factory) -> None:
        """``SpanExporter`` is a plain class in this SDK version (no ABC, no ``typing.Protocol``),
        so the base class is the only conformance a type checker can see — and
        ``BatchSpanProcessor`` is annotated to take exactly it."""
        from opentelemetry.sdk.trace.export import SpanExporter

        assert isinstance(self._span_exporter(client_factory), SpanExporter)

    def test_the_log_exporter_is_an_sdk_log_exporter_with_every_abstract_method_implemented(
        self, client_factory
    ) -> None:
        """``LogRecordExporter`` *is* an ``abc.ABC`` — ``export`` and ``shutdown`` are both
        abstract — so the empty ``__abstractmethods__`` is the ABC's own statement that this class
        is complete, and ``isinstance`` is the registration that matters to it. If a future SDK
        release adds a third abstract method, this class stops being constructible, which is a
        startup error rather than a runtime one."""
        from opentelemetry.sdk._logs.export import LogRecordExporter

        assert isinstance(self._log_exporter(client_factory), LogRecordExporter)
        assert not OTLPHTTPJSONLogExporter.__abstractmethods__

    def test_the_batch_processors_the_setup_module_builds_accept_them(self, client_factory) -> None:
        """The adoption seam itself: ``configure_telemetry`` hands these objects to the SDK's batch
        processors. Constructing them is the claim — the type contract is what this asserts, and
        nothing is exported, so no request is made."""
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        span_processor = BatchSpanProcessor(self._span_exporter(client_factory))
        log_processor = BatchLogRecordProcessor(self._log_exporter(client_factory))
        span_processor.shutdown()
        log_processor.shutdown()

    def test_the_log_exporter_returns_the_result_enum_the_abc_declares(
        self, log_record_factory, client_factory
    ) -> None:
        """**Found by mypy, not by a test.** In this SDK version the two result enums are separate
        classes, so ``LogExportResult.SUCCESS`` — what ``export`` used to return — is not the value
        ``LogRecordExporter.export`` is declared to produce, and a caller comparing against the
        declared enum would see a failure. Nine live pipeline tests passed on the deprecated one
        because nothing in the SDK compares the value it gets back.
        """
        from opentelemetry.sdk._logs.export import LogExportResult, LogRecordExportResult

        assert LogExportResult is not LogRecordExportResult, (
            "if the SDK turns one into an alias of the other, this test is obsolete rather than wrong"
        )
        assert LogExportResult.SUCCESS is not LogRecordExportResult.SUCCESS
        result = self._log_exporter(client_factory).export([log_record_factory()])
        assert result is LogRecordExportResult.SUCCESS
        assert type(result) is LogRecordExportResult


class TestPayloadSize:
    def test_a_payload_can_be_measured(self, span_factory) -> None:
        size = payload_size(encode_spans([span_factory()]))
        assert 0 < size < 2000

    def test_the_size_is_the_compact_form(self) -> None:
        assert payload_size({"a": 1}) == len(b'{"a":1}')
