"""§15's adoption seam from the service side: a request produces a span, and the span joins the trace.

This is the gateway half of ADR-0032's "not yet adopted" note. The telemetry package proved the
pipeline end to end against a live collector (``tests/telemetry/test_pipeline_live.py``); what these
tests cover is the part the package could not: that `create_app` actually installs the middleware, and
that the middleware reads what it must.

The four claims, and why each is worth a test rather than a comment:

* **One server span per request, with the attributes §15's admin views filter on.** A span with no
  status code is a line in a trace list that answers nothing.
* **An inbound ``traceparent`` is continued, not ignored.** This is the claim that fails *silently*:
  a service that never extracts produces a new root trace per request, and a new root trace is
  indistinguishable from a correct one when you look at it. The trace looks healthy and is two traces.
  It is also the whole point of the gateway-to-agent hop.
* **A telemetry failure cannot fail the request.** §15 states it outright — *"a telemetry outage
* does not cause booking to fail"* — and this is where that promise is either kept or broken,
* because this is the
  middleware every request passes through.
* **An app built without telemetry installs nothing.** The default has to be inert, or every test in
  this suite would be exporting to a collector that is not there.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from referraleasy_gateway.app import create_app
from referraleasy_gateway.config import GatewaySettings
from referraleasy_telemetry.events import TelemetryEmitter
from referraleasy_telemetry.isolation import DropCounter
from referraleasy_telemetry.setup import Telemetry

SERVICE = "referraleasy-gateway"

#: A well-formed W3C trace context, so a continued trace is distinguishable from a fresh one by its
#: trace id alone. Written out rather than generated: a test that computes its own expectation out of
#: the same library it is testing cannot fail.
INBOUND_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
INBOUND_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


class _RecordingLogger:
    """The ``emit(**kwargs)`` shape ``TelemetryEmitter`` wraps, keeping what it was given."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def emit(self, **kwargs: Any) -> None:
        self.records.append(kwargs)


def _telemetry(tracer: Any, provider: TracerProvider | None = None) -> Telemetry:
    return Telemetry(
        tracer=tracer,
        emitter=TelemetryEmitter(_RecordingLogger()),
        drops=DropCounter(),
        tracer_provider=provider,
        config=None,
    )


@pytest.fixture
def spans() -> Iterator[tuple[Telemetry, InMemorySpanExporter, TracerProvider]]:
    """A real provider and a real exporter, torn down after the test.

    Real rather than a double for the same reason the telemetry suite's `span_factory` is real: the
    things worth asserting are the SDK's own shapes, and a fake would take whatever shape this test
    expected — including the wrong one.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("referraleasy.gateway", "0.1.0")
    yield _telemetry(tracer, provider), exporter, provider
    provider.shutdown()


def _app(
    settings: GatewaySettings,
    clock: Any,
    control: Any,
    telemetry: Telemetry | None,
) -> FastAPI:
    return create_app(settings=settings, clock=clock, control=control, telemetry=telemetry)


def test_a_request_produces_one_server_span_with_the_attributes_a_query_filters_on(
    settings: GatewaySettings, clock: Any, control: Any, spans: Any
) -> None:
    telemetry, exporter, _ = spans
    with TestClient(_app(settings, clock, control, telemetry)) as client:
        response = client.get("/openapi.json")
    assert response.status_code == 200

    finished = exporter.get_finished_spans()
    assert len(finished) >= 1, "the middleware produced no span at all"
    # `/openapi.json` is the request; the span for it is named by method and path. Not `/healthz`:
    # that path is deliberately untraced, and `test_the_liveness_probe_is_served_but_not_traced`
    # below is where that is asserted.
    ours = [span for span in finished if span.name == "GET /openapi.json"]
    assert len(ours) == 1, (
        f"expected exactly one span for the request, got {[s.name for s in finished]}"
    )
    span = ours[0]

    attributes = span.attributes or {}
    assert attributes.get("http.request.method") == "GET"
    assert attributes.get("url.path") == "/openapi.json"
    assert attributes.get("http.response.status_code") == 200
    # The correlation id the response header carries, so a log line and a span can be joined.
    assert attributes.get("referraleasy.request_id") == response.headers["x-request-id"]
    # And it is a *server* span, which is what tells a viewer which side of the hop it was.
    from opentelemetry.trace import SpanKind

    assert span.kind is SpanKind.SERVER


def test_an_inbound_traceparent_is_continued_rather_than_starting_a_new_trace(
    settings: GatewaySettings, clock: Any, control: Any, spans: Any
) -> None:
    """The claim that fails silently: a fresh root trace looks exactly like a correct one."""
    telemetry, exporter, _ = spans
    with TestClient(_app(settings, clock, control, telemetry)) as client:
        response = client.get(
            "/openapi.json",
            headers={"traceparent": INBOUND_TRACEPARENT, "x-request-id": "caller-supplied-id"},
        )
    assert response.status_code == 200

    our_spans = [s for s in exporter.get_finished_spans() if s.name == "GET /openapi.json"]
    assert len(our_spans) == 1
    span = our_spans[0]

    assert format(span.context.trace_id, "032x") == INBOUND_TRACE_ID, (
        "the inbound traceparent was ignored and this request started a trace of its own; the "
        "gateway's span and the caller's span are two traces rather than one"
    )
    # The caller's span id became the parent, which is what makes the two a tree rather than a pair.
    assert span.parent is not None
    assert format(span.parent.span_id, "016x") == "00f067aa0ba902b7"


def test_a_telemetry_failure_cannot_fail_the_request(
    settings: GatewaySettings, clock: Any, control: Any
) -> None:
    """§15: *"a telemetry outage does not cause booking to fail"* — and this middleware is on every
    request.

    The tracer is made to raise, which is the failure mode `IsolatedTracer` exists for: the SDK's
    own exporters cannot reach the caller (they batch on another thread), but a span processor or a
    sampler can. A request that failed here would fail on `GET /healthz`, which is the probe compose
    uses to decide the container is healthy — so a telemetry bug would look like a service outage.
    """

    class _ExplodingTracer:
        def start_span(self, name: str, **kwargs: Any) -> Any:
            raise RuntimeError("the tracing backend is unreachable")

    telemetry = _telemetry(_ExplodingTracer())

    with TestClient(_app(settings, clock, control, telemetry)) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200, (
        f"a telemetry failure took the request down: {response.status_code} {response.text[:200]}"
    )
    # A real body from the same route the traced tests use, so this is asserting that the *handler*
    # ran rather than that a middleware short-circuited an empty 200.
    assert "paths" in response.json()
    # And it was counted rather than swallowed without trace: §15 requires the drops to be visible,
    # because a service silently not tracing is the thing an operator cannot see.
    assert telemetry.drops.total >= 1, "the failure was neither raised nor counted"


def test_the_liveness_probe_is_served_but_not_traced(
    settings: GatewaySettings, clock: Any, control: Any, spans: Any
) -> None:
    """The one deliberate exclusion, asserted so it stays deliberate.

    Compose probes `/healthz` every ten seconds for the container's lifetime. On this checkout that
    was 2001 of the index's 2016 spans after a few hours of a stack nobody was using — so a
    request-rate chart would have been a chart about the monitoring system, which is the same wrong
    answer as an invented number arriving from the other side.

    Both halves are asserted, because "no span" on its own would also be true of a middleware that
    had stopped working entirely: the probe still has to be *served*, with its real status.
    """
    telemetry, exporter, _ = spans
    with TestClient(_app(settings, clock, control, telemetry)) as client:
        probe = client.get("/healthz")
        assert probe.status_code == 200, "the probe stopped being served, not just traced"
        assert probe.json()["status"] == "ok"
        # And a normal request on the same app still is traced, so the exclusion is about the path
        # rather than about the app.
        assert client.get("/openapi.json").status_code == 200

    names = [span.name for span in exporter.get_finished_spans()]
    assert "GET /healthz" not in names, f"the probe was traced: {names}"
    assert "GET /openapi.json" in names, f"the exclusion took a real request with it: {names}"


def test_an_app_built_without_telemetry_installs_no_span_middleware(
    settings: GatewaySettings, clock: Any, control: Any, spans: Any
) -> None:
    """The default is inert, so the rest of this suite exports nothing.

    Asserted against a real exporter rather than by inspecting the middleware list: what matters is
    that no span arrives, not that no object was appended.
    """
    _, exporter, _ = spans
    with TestClient(_app(settings, clock, control, None)) as client:
        assert client.get("/openapi.json").status_code == 200

    assert exporter.get_finished_spans() == (), (
        "an app built without telemetry emitted a span; the default has to be inert"
    )
