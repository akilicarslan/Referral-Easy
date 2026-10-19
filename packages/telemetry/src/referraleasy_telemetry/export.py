"""OTLP/HTTP exporters, and the reason this package carries its own.

**Why the JSON encoding and not the protobuf one.** The canonical Python exporters
(``opentelemetry-exporter-otlp-proto-http``, ``-grpc``) are not in this repository's virtual
environment, and the virtual environment is shared with other work in progress, so it cannot be
re-synced to add them mid-flight. The two options were therefore: (a) ship a package whose exporter
does not exist and claim the pipeline works, or (b) speak the protocol.

OTLP defines **two** wire encodings over the same HTTP endpoints — protobuf and JSON — and the
Collector accepts both. :class:`OTLPHTTPJSONSpanExporter` and :class:`OTLPHTTPJSONLogExporter`
implement the JSON one, which is a real OTLP client, not a stand-in for one: it posts real OTLP
payloads to the real collector, and the whole of §15 is verified end to end through it
(``tests/telemetry/test_pipeline_live.py`` reads the documents back out of OpenSearch).

What that buys and what it costs, stated plainly so nobody has to infer it:

* Bought: the pipeline proof is of the *pipeline*. A unit test against a mocked exporter would
  have proved the serialisation and nothing else.
* Cost: this encoder is ours, so a bug in it is ours. It is bounded to the subset of OTLP this
  platform emits, and the live tests are what hold it honest. Swapping in the vendor exporter is a
  one-line change in :func:`referraleasy_telemetry.setup.configure_telemetry`, and when the workspace is
  next synced that swap should be made — the vendor encoder has the field coverage this one
  deliberately does not.

**Encoding rules that are easy to get wrong and are therefore pinned by tests.** OTLP JSON is
protobuf-JSON, so it is not a free-form JSON dialect:

* ``int64`` fields are **strings** (``"startTimeUnixNano": "175..."``). ``google.protobuf`` emits
  them as strings because JSON numbers are doubles and would lose precision past 2^53; the
  collector's parser accepts either, but emitting a number is what the spec's own tooling flags.
* ids are lowercase hex, ``traceId`` 32 characters and ``spanId`` 16.
* enums are integers (``kind``, ``status.code``, ``severityNumber``), not names.
* bytes are base64.
* field names are lowerCamelCase.

**Failure never propagates.** Every export catches everything and returns a FAILURE result.
§15 requires that a telemetry outage does not take a booking down, and an exception raised out of
an exporter would surface in whichever request happened to be in flight. Failures are counted on
:class:`ExportStats` rather than swallowed, so the drop rate is a number someone can look at.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode

from referraleasy_telemetry.masking import (
    DEFAULT_POLICY,
    MaskingPolicy,
    mask_log_record,
    mask_span_attributes,
)

logger = logging.getLogger(__name__)

#: Path suffix per signal, appended to the configured OTLP base URL. The base URL is what
#: ``OTEL_EXPORTER_OTLP_ENDPOINT`` holds (``http://otel-collector:4318``); the OTLP spec appends
#: the signal path, and the collector's HTTP receiver listens on exactly these.
TRACES_PATH: Final[str] = "/v1/traces"
LOGS_PATH: Final[str] = "/v1/logs"

#: Bounded, and small on purpose. The collector is on the same Docker network, so a healthy round
#: trip is single-digit milliseconds; anything slower than this is a collector that is down or
#: wedged, and waiting longer only delays whatever request the exporter was called from. §15's
#: "a telemetry outage does not cause booking to fail" is only true if the failure mode is fast.
DEFAULT_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(connect=1.0, read=2.0, write=2.0, pool=1.0)


@dataclass(slots=True)
class ExportStats:
    """Counters for one exporter. Mutable and shared, which is why it is a dataclass and not state
    hidden in a closure: a test asserts on it directly after a live export."""

    attempted: int = 0
    exported: int = 0
    failed: int = 0
    masked: int = 0
    last_error: str | None = None

    @property
    def dropped(self) -> int:
        """Records that never reached the collector. §15 wants this to be a visible number."""
        return self.failed

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "exported": self.exported,
            "failed": self.failed,
            "masked": self.masked,
            "last_error": self.last_error,
        }


# ---------------------------------------------------------------------------
# protobuf-JSON primitives
# ---------------------------------------------------------------------------


def encode_value(value: Any) -> dict[str, Any]:
    """One OTLP ``AnyValue`` in JSON form."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        # int64 as a string, per the protobuf JSON mapping.
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (bytes, bytearray)):
        return {"bytesValue": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, Mapping):
        return {
            "kvlistValue": {
                "values": [{"key": str(k), "value": encode_value(v)} for k, v in value.items()]
            }
        }
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [encode_value(v) for v in value]}}
    # Anything else is not representable in OTLP. Stringify rather than drop: an unrepresentable
    # attribute is a producer bug that should be visible in the index, not a silent absence.
    return {"stringValue": str(value)}


def encode_attributes(attributes: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """An OTLP ``KeyValue`` list. Order is preserved so a diff of two payloads is readable."""
    if not attributes:
        return []
    return [{"key": str(k), "value": encode_value(v)} for k, v in attributes.items()]


def encode_resource(resource: Resource | None) -> dict[str, Any]:
    """The OTLP ``Resource``. ``droppedAttributesCount`` is emitted as 0 rather than omitted."""
    attrs = dict(resource.attributes) if resource is not None else {}
    return {"attributes": encode_attributes(attrs), "droppedAttributesCount": 0}


def encode_scope(scope: Any) -> dict[str, Any]:
    """The OTLP ``InstrumentationScope``."""
    if scope is None:
        return {}
    encoded: dict[str, Any] = {"name": getattr(scope, "name", "") or ""}
    version = getattr(scope, "version", None)
    if version:
        encoded["version"] = version
    return encoded


def _hex(value: int | None, width: int) -> str:
    """Zero-padded lowercase hex, or ``""`` for an absent id.

    OTLP omits an id by sending the empty string rather than a run of zeros, and the two mean
    different things downstream: an empty ``parentSpanId`` is a root span.
    """
    if not value:
        return ""
    return format(value, f"0{width}x")


# ---------------------------------------------------------------------------
# Span encoding
# ---------------------------------------------------------------------------

#: ``SpanKind`` -> the OTLP enum integer. Read from the API enum rather than hard-coded so a
#: renumbering in the SDK cannot silently mislabel a span.
_KIND_VALUE: Final[dict[Any, int]] = {}


def _span_kind(kind: Any) -> int:
    """The OTLP wire enum integer for an OTel API ``SpanKind``.

    **The two enums are not the same enum, and the Python one is offset by one.** In the API,
    ``SpanKind.INTERNAL == 0``; on the wire, ``SPAN_KIND_UNSPECIFIED == 0`` and
    ``SPAN_KIND_INTERNAL == 1``. So ``member.value`` is exactly the wrong thing to send, and
    sending it makes every internal span arrive as ``SPAN_KIND_UNSPECIFIED`` while every server
    span arrives as ``SPAN_KIND_INTERNAL`` — a mislabelling that is invisible in a unit test and
    obvious the moment a document is read back out of OpenSearch (which is how it was found: a
    ``start_as_current_span`` span, which is ``INTERNAL``, arrived as
    ``"kind": "SPAN_KIND_UNSPECIFIED"``).

    The mapping below is the OTLP ``SpanKind`` enum from the specification, written out rather than
    derived, because deriving it from the Python enum is precisely the mistake being avoided.
    """
    if not _KIND_VALUE:
        from opentelemetry.trace import SpanKind

        _KIND_VALUE.update(
            {
                SpanKind.INTERNAL: 1,  # SPAN_KIND_INTERNAL
                SpanKind.SERVER: 2,  # SPAN_KIND_SERVER
                SpanKind.CLIENT: 3,  # SPAN_KIND_CLIENT
                SpanKind.PRODUCER: 4,  # SPAN_KIND_PRODUCER
                SpanKind.CONSUMER: 5,  # SPAN_KIND_CONSUMER
            }
        )
    return _KIND_VALUE.get(kind, 0)


_STATUS_VALUE: Final[dict[Any, int]] = {
    StatusCode.UNSET: 0,
    StatusCode.OK: 1,
    StatusCode.ERROR: 2,
}


def encode_span(
    span: Any, policy: MaskingPolicy = DEFAULT_POLICY, stats: ExportStats | None = None
) -> dict[str, Any]:
    """One ``ReadableSpan`` as an OTLP JSON span.

    Masking is applied to the *copy* of the attributes that is being encoded — the span object is
    left alone, because this exporter is one of possibly several attached to a provider and the
    others must not be handed someone else's transformed view.
    """
    context = span.context
    attributes = dict(span.attributes or {})
    masked = mask_span_attributes(attributes, policy) if attributes else {}
    if stats is not None and masked != attributes:
        stats.masked += 1

    parent = span.parent
    encoded: dict[str, Any] = {
        "traceId": _hex(context.trace_id, 32),
        "spanId": _hex(context.span_id, 16),
        "name": span.name or "",
        "kind": _span_kind(span.kind),
        "startTimeUnixNano": str(span.start_time),
        "endTimeUnixNano": str(span.end_time) if span.end_time else "0",
        "attributes": encode_attributes(masked),
        "droppedAttributesCount": 0,
        "events": [
            {
                "timeUnixNano": str(event.timestamp),
                "name": event.name,
                "attributes": encode_attributes(event.attributes),
                "droppedAttributesCount": 0,
            }
            for event in (span.events or ())
        ],
        "droppedEventsCount": 0,
        "links": [
            {
                "traceId": _hex(link.context.trace_id, 32),
                "spanId": _hex(link.context.span_id, 16),
                "attributes": encode_attributes(link.attributes),
                "droppedAttributesCount": 0,
            }
            for link in (span.links or ())
        ],
        "droppedLinksCount": 0,
        "status": {
            "code": _STATUS_VALUE.get(span.status.status_code, 0),
            **({"message": span.status.description} if span.status.description else {}),
        },
    }
    trace_state = getattr(context, "trace_state", None)
    encoded["traceState"] = str(trace_state) if trace_state else ""
    if parent is not None:
        encoded["parentSpanId"] = _hex(parent.span_id, 16)
    return encoded


def encode_spans(
    spans: Sequence[Any], policy: MaskingPolicy = DEFAULT_POLICY, stats: ExportStats | None = None
) -> dict[str, Any]:
    """A whole ``ExportTraceServiceRequest``. Spans are grouped by resource and scope, as OTLP
    requires — a flat list is not a valid request body."""
    by_resource: dict[int, dict[str, Any]] = {}
    for span in spans:
        resource = span.resource
        key = id(resource)
        group = by_resource.get(key)
        if group is None:
            group = {
                "resource": encode_resource(resource),
                "scopeSpans": {},
                "scopeOrder": [],
            }
            by_resource[key] = group
        scope = span.instrumentation_scope
        scope_key = getattr(scope, "name", "") or ""
        scope_group = group["scopeSpans"].get(scope_key)
        if scope_group is None:
            scope_group = {"scope": encode_scope(scope), "spans": []}
            group["scopeSpans"][scope_key] = scope_group
            group["scopeOrder"].append(scope_key)
        scope_group["spans"].append(encode_span(span, policy, stats))

    resource_spans = [
        {
            "resource": group["resource"],
            "scopeSpans": [group["scopeSpans"][k] for k in group["scopeOrder"]],
        }
        for group in by_resource.values()
    ]
    return {"resourceSpans": resource_spans}


# ---------------------------------------------------------------------------
# Log record encoding
# ---------------------------------------------------------------------------


def _severity_number(record: Any) -> int:
    """The OTLP integer for a record's severity, never silently zero.

    **Found by a test that asserted the value rather than its type.** ``SeverityNumber`` is a plain
    ``Enum``, not an ``IntEnum``, so ``int(SeverityNumber.ERROR)`` raises ``TypeError`` — and the
    ``except TypeError: return 0`` that was here for "a defensive fallback" caught it on the
    ordinary path. Every document therefore reached OpenSearch with ``severityNumber: 0``
    (``SEVERITY_NUMBER_UNSPECIFIED``) and an empty ``severityText``, discarding the error/warning
    distinction the emitter computes from the outcome class. Nothing failed; a filter on severity
    simply returned nothing.

    An enum member carries its wire value in ``.value``, so that is read first. The fallback to 0
    now only covers a value that is genuinely not a number, which is the case it was meant for.
    """
    number = getattr(record, "severity_number", None)
    if number is None:
        return 0
    value = getattr(number, "value", number)
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):  # pragma: no cover - a non-numeric severity from a third party
        return 0


def _severity_text(record: Any) -> str:
    """The severity as text, derived from the number when the caller supplied no text.

    Needed because the SDK leaves ``severity_text`` as ``None`` when only ``severity_number`` is
    passed (measured — it is how the emitter calls it), and an empty ``severityText`` on every
    document makes the field useless for filtering. The derived text is the OTel enum member name,
    so an index that groups by it gets ``INFO``/``WARN``/``ERROR`` and not a second spelling.
    """
    text = getattr(record, "severity_text", None)
    if text:
        return str(text)
    number = _severity_number(record)
    if not number:
        return ""
    try:
        from opentelemetry._logs import SeverityNumber

        return str(SeverityNumber(number).name)
    except ValueError:  # pragma: no cover - an out-of-range severity from a third party
        return ""


def _record_timestamp(record: Any) -> int:
    """``timeUnixNano`` for a log record, never ``None``.

    The SDK only sets ``timestamp`` when the caller passes one, and ``observed_timestamp`` always.
    Falling back to ``observed_timestamp`` keeps the record ordered; omitting the field entirely
    would send ``timeUnixNano: "None"``, which the collector rejects with an HTTP 400 and the whole
    *batch* is lost with it. That is measured, not hypothetical: it is what this line was written
    in response to, and it is why the fallback is a value rather than an omission.
    """
    timestamp = getattr(record, "timestamp", None)
    if timestamp:
        return int(timestamp)
    observed = getattr(record, "observed_timestamp", None)
    return int(observed) if observed else 0


def encode_log_record(
    record: Any, policy: MaskingPolicy = DEFAULT_POLICY, stats: ExportStats | None = None
) -> dict[str, Any]:
    """One ``ReadableLogRecord`` as an OTLP JSON log record.

    §15's measurements travel as these. The numeric payload arrives as ``metric.value``, which the
    events index template maps as a double, and as an OTLP ``doubleValue`` on the wire.
    """
    body = record.log_record.body
    attributes = dict(record.log_record.attributes or {})
    masked_attrs, masked_body = mask_log_record(
        attributes, body if isinstance(body, str) else None, policy
    )
    if stats is not None and (masked_attrs != attributes or masked_body != body):
        stats.masked += 1
    if not isinstance(body, str):
        masked_body = body

    encoded: dict[str, Any] = {
        "timeUnixNano": str(_record_timestamp(record.log_record)),
        "observedTimeUnixNano": str(_record_timestamp(record.log_record)),
        "severityNumber": _severity_number(record.log_record),
        "severityText": _severity_text(record.log_record),
        "body": encode_value(masked_body if masked_body is not None else ""),
        "attributes": encode_attributes(masked_attrs),
        "droppedAttributesCount": 0,
        "flags": int(getattr(record.log_record, "flags", 0) or 0),
    }
    trace_id = getattr(record.log_record, "trace_id", None)
    span_id = getattr(record.log_record, "span_id", None)
    if trace_id:
        encoded["traceId"] = _hex(trace_id, 32)
    if span_id:
        encoded["spanId"] = _hex(span_id, 16)
    return encoded


def encode_log_records(
    records: Sequence[Any], policy: MaskingPolicy = DEFAULT_POLICY, stats: ExportStats | None = None
) -> dict[str, Any]:
    """A whole ``ExportLogsServiceRequest``, grouped by resource and scope."""
    by_resource: dict[int, dict[str, Any]] = {}
    for record in records:
        # `resource` lives on the ReadableLogRecord, not on the inner LogRecord it wraps
        # (measured: the inner one has no such attribute).
        resource = record.resource
        key = id(resource)
        group = by_resource.get(key)
        if group is None:
            group = {"resource": encode_resource(resource), "scopeLogs": {}, "scopeOrder": []}
            by_resource[key] = group
        scope = record.instrumentation_scope
        scope_key = getattr(scope, "name", "") or ""
        scope_group = group["scopeLogs"].get(scope_key)
        if scope_group is None:
            scope_group = {"scope": encode_scope(scope), "logRecords": []}
            group["scopeLogs"][scope_key] = scope_group
            group["scopeOrder"].append(scope_key)
        scope_group["logRecords"].append(encode_log_record(record, policy, stats))

    resource_logs = [
        {
            "resource": group["resource"],
            "scopeLogs": [group["scopeLogs"][k] for k in group["scopeOrder"]],
        }
        for group in by_resource.values()
    ]
    return {"resourceLogs": resource_logs}


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class _OTLPHTTPTransport:
    """Posts an encoded OTLP payload and classifies the result. Never raises."""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: httpx.Timeout | None = None,
        headers: Mapping[str, str] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout or DEFAULT_TIMEOUT
        self.headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if headers:
            self.headers.update(headers)
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def post(self, payload: dict[str, Any], path: str, stats: ExportStats) -> bool:
        """POST ``payload`` to ``<endpoint><path>``. Returns success. Records the failure if not."""
        url = f"{self.endpoint}{path}"
        try:
            response = self._http().post(url, json=payload, headers=self.headers)
        except Exception as exc:
            stats.failed += 1
            stats.last_error = f"{type(exc).__name__}: {exc}"[:300]
            logger.debug("OTLP export to %s failed: %s", url, exc)
            return False
        if response.status_code >= 300:
            # A 503 here is the collector's memory_limiter refusing data, which §15 wants to be a
            # visible drop rather than a silent one.
            stats.failed += 1
            stats.last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            logger.debug("OTLP export to %s rejected: %s", url, response.status_code)
            return False
        return True

    def shutdown(self) -> None:
        if self._client is not None and self._owns_client:
            close = getattr(self._client, "close", None)
            if close is not None:
                close()


class OTLPHTTPJSONSpanExporter(SpanExporter):
    """OTLP/HTTP JSON span exporter with masking built in.

    **Subclasses the SDK's ``SpanExporter`` deliberately.** An earlier version duck-typed the three
    methods and said so here — "satisfies ``SpanExporter`` structurally, without subclassing it".
    That was true at runtime and invisible to a type checker, and the reason is specific rather than
    a mypy limitation: measured against the installed SDK, ``SpanExporter`` is a **plain class**
    (``__bases__ == (object,)``, no ``__abstractmethods__``), not a ``typing.Protocol`` and not an
    ABC, so nominal inheritance is the only conformance a checker can see, and
    ``BatchSpanProcessor.__init__`` is annotated ``span_exporter: SpanExporter``.

    The old rationale — "stays importable if the base class moves" — does not survive contact with
    this file: the module imports ``Resource`` and ``StatusCode`` from the SDK at module scope
    already. Subclassing costs nothing and buys a checked override: a future SDK release that
    changes ``export``, ``force_flush`` or ``shutdown`` now fails ``mypy`` instead of raising
    ``TypeError`` inside whichever request happened to be in flight.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: httpx.Timeout | None = None,
        headers: Mapping[str, str] | None = None,
        client: httpx.Client | None = None,
        policy: MaskingPolicy = DEFAULT_POLICY,
    ) -> None:
        self._transport = _OTLPHTTPTransport(
            endpoint, timeout=timeout, headers=headers, client=client
        )
        self._policy = policy
        self.stats = ExportStats()

    @property
    def endpoint(self) -> str:
        return f"{self._transport.endpoint}{TRACES_PATH}"

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        spans = list(spans)
        self.stats.attempted += len(spans)
        try:
            payload = encode_spans(spans, self._policy, self.stats)
        except Exception as exc:
            self.stats.failed += len(spans)
            self.stats.last_error = f"encode: {type(exc).__name__}: {exc}"[:300]
            logger.debug("could not encode %d spans", len(spans), exc_info=True)
            return SpanExportResult.FAILURE
        if self._transport.post(payload, TRACES_PATH, self.stats):
            self.stats.exported += len(spans)
            return SpanExportResult.SUCCESS
        return SpanExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        self._transport.shutdown()


class OTLPHTTPJSONLogExporter(LogRecordExporter):
    """OTLP/HTTP JSON log/event exporter with masking built in. See the span exporter above.

    **Subclassing is load-bearing here, not just checkable.** Measured on the installed SDK:
    ``LogRecordExporter`` *is* an ``abc.ABC`` and marks **both** ``export`` and ``shutdown``
    ``@abc.abstractmethod`` (it has no ``force_flush`` at all — the one defined below is ours).
    A duck-typed exporter satisfies today's two methods but is not registered as an implementation,
    so an SDK release that grows a third abstract method would turn a working process into
    ``TypeError: Can't instantiate abstract class`` at ``configure_telemetry`` time — which is
    exactly the "could break at runtime" the subclass link converts into a startup failure.

    It also had a real bug that only the subclass link makes visible. ``export`` used to return
    ``LogExportResult``, the **deprecated** alias — and in this SDK version that is not an alias but
    a *second* ``enum.Enum`` class with equivalent members:
    ``LogExportResult.SUCCESS is not LogRecordExportResult.SUCCESS``. Nothing in the SDK compares
    the returned value (the enum appears only in definitions and re-exports), so nine live pipeline
    tests passed while the exporter answered the ABC's contract with a different class. It now
    returns ``LogRecordExportResult``, and ``TestTheExportersAreSDKExporters`` in
    ``tests/telemetry/test_export.py`` pins both the inheritance and the enum identity.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: httpx.Timeout | None = None,
        headers: Mapping[str, str] | None = None,
        client: httpx.Client | None = None,
        policy: MaskingPolicy = DEFAULT_POLICY,
    ) -> None:
        self._transport = _OTLPHTTPTransport(
            endpoint, timeout=timeout, headers=headers, client=client
        )
        self._policy = policy
        self.stats = ExportStats()

    @property
    def endpoint(self) -> str:
        return f"{self._transport.endpoint}{LOGS_PATH}"

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        batch = list(batch)
        self.stats.attempted += len(batch)
        try:
            payload = encode_log_records(batch, self._policy, self.stats)
        except Exception as exc:
            self.stats.failed += len(batch)
            self.stats.last_error = f"encode: {type(exc).__name__}: {exc}"[:300]
            logger.debug("could not encode %d log records", len(batch), exc_info=True)
            return LogRecordExportResult.FAILURE
        if self._transport.post(payload, LOGS_PATH, self.stats):
            self.stats.exported += len(batch)
            return LogRecordExportResult.SUCCESS
        return LogRecordExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        self._transport.shutdown()


def payload_size(payload: dict[str, Any]) -> int:
    """Serialised size in bytes. Used by tests to assert a bounded payload, and by nothing else."""
    return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


#: Re-exported so the masking rules are reachable from the exporter that applies them.
__all__ = [
    "LOGS_PATH",
    "TRACES_PATH",
    "ExportStats",
    "OTLPHTTPJSONLogExporter",
    "OTLPHTTPJSONSpanExporter",
    "encode_attributes",
    "encode_log_record",
    "encode_log_records",
    "encode_resource",
    "encode_span",
    "encode_spans",
    "encode_value",
    "payload_size",
]
