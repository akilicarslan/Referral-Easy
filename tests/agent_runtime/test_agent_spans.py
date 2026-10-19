"""§15 reaches the seven agent services: every one of them emits a span, in the caller's trace.

The agent half of ADR-0032's "not yet adopted" note. Two of these three claims are about the same
thing — that an A2A task arrives *inside* the coordinator's trace rather than beside it — and that is
the difference between a demo that shows one trace across a multi-agent call and one that shows
eight unrelated traces that happen to be adjacent in a list.

There is no per-role test here for the same reason there is no per-role copy of the middleware: all
seven roles are built by `create_app`, so a test that passes for one is a statement about the
builder. What *is* role-specific is the service name, because the same image runs as all seven and a
trace that said "agent" would not tell an operator which service produced a span — so that one is
asserted for every role.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from agent_runtime_doubles import SPEC_ENV
from fastapi.testclient import TestClient
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from referraleasy_agent_runtime.app import create_app
from referraleasy_agent_runtime.auth import AUTH_TOKEN_ENV
from referraleasy_agent_runtime.roles import AgentRole
from referraleasy_agent_runtime.settings import AgentSettings
from referraleasy_telemetry.events import TelemetryEmitter
from referraleasy_telemetry.isolation import DropCounter
from referraleasy_telemetry.setup import Telemetry, TelemetryConfig, configure_telemetry

#: The service token the role verifier accepts. `/healthz` is the only route that does not need it,
#: and `/healthz` is the one path the middleware deliberately does not trace — so every span
#: assertion here goes through the manifest route with this header.
AUTH = {"Authorization": f"Bearer {SPEC_ENV[AUTH_TOKEN_ENV]}"}

#: The `traceparent` a coordinator would put in an A2A task's metadata. Written out rather than
#: computed, so the expectation is not derived from the library under test.
INBOUND_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
INBOUND_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
INBOUND_PARENT_SPAN = "00f067aa0ba902b7"


class _RecordingLogger:
    """The ``emit(**kwargs)`` shape ``TelemetryEmitter`` wraps."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def emit(self, **kwargs: Any) -> None:
        self.records.append(kwargs)


@pytest.fixture
def spans() -> Iterator[tuple[Telemetry, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "agent-tests"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = Telemetry(
        tracer=provider.get_tracer("referraleasy.agent_runtime", "0.1.0"),
        emitter=TelemetryEmitter(_RecordingLogger()),
        drops=DropCounter(),
        tracer_provider=provider,
        config=None,
    )
    yield telemetry, exporter
    provider.shutdown()


def _client(telemetry: Telemetry | None, role: AgentRole = AgentRole.SCHEDULING) -> TestClient:
    settings = AgentSettings.from_env(SPEC_ENV, role=role)
    return TestClient(create_app(settings, telemetry=telemetry))


def test_a_request_produces_a_server_span_named_for_the_role_that_served_it(spans: Any) -> None:
    """The manifest route, not `/healthz`: the probe path is deliberately untraced (see below)."""
    telemetry, exporter = spans
    with _client(telemetry) as client:
        assert client.get("/manifest", headers=AUTH).status_code == 200

    finished = exporter.get_finished_spans()
    assert len(finished) >= 1, "the agent emitted no span at all"
    ours = [span for span in finished if span.name == "GET /manifest"]
    assert len(ours) == 1, f"expected one span for the request, got {[s.name for s in finished]}"
    attributes = ours[0].attributes or {}
    assert attributes.get("http.request.method") == "GET"
    assert attributes.get("url.path") == "/manifest"
    assert attributes.get("http.response.status_code") == 200


def test_the_liveness_probe_is_served_but_not_traced(spans: Any) -> None:
    """Same exclusion as the gateway's, and the same reason: compose probes it every ten seconds.

    Both halves again — a probe that stopped being served would also produce no span.
    """
    telemetry, exporter = spans
    with _client(telemetry) as client:
        probe = client.get("/healthz")
        assert probe.status_code == 200, "the probe stopped being served, not just traced"
        assert probe.json()["status"] == "ok"
        assert client.get("/manifest", headers=AUTH).status_code == 200

    names = [span.name for span in exporter.get_finished_spans()]
    assert "GET /healthz" not in names, f"the probe was traced: {names}"
    assert "GET /manifest" in names, f"the exclusion took a real request with it: {names}"


@pytest.mark.parametrize("role", tuple(AgentRole))
def test_every_role_is_configured_under_its_own_service_name(role: AgentRole) -> None:
    """Seven services, one image: the resource attribute is what tells them apart in a trace list.

    Asserted through `configure_telemetry` rather than through a request, because that is where the
    service name is decided — the middleware reads the provider it is handed, so a hand-built
    provider in a fixture (as the tests above use) would be asserting the fixture's own name.

    `set_global=False` so this does not install a process-global provider that would outlive the
    test, and no exporter because none is needed to read a resource back.
    """
    settings = AgentSettings.from_env(SPEC_ENV, role=role)
    telemetry = configure_telemetry(
        settings.service_name,
        version="0.1.0",
        set_global=False,
        config=TelemetryConfig(service_name=settings.service_name, enabled=False),
    )
    try:
        assert telemetry.tracer_provider is not None
        attributes = telemetry.tracer_provider.resource.attributes
        assert attributes.get("service.name") == settings.service_name, (
            f"the process serving {role.value} reports service.name="
            f"{attributes.get('service.name')!r}, so seven services would collapse into one series "
            "on every chart"
        )
        # Not the bare word "agent": the image runs as all seven, and a name that does not carry the
        # role cannot answer "which one was slow".
        assert role.value in str(attributes.get("service.name"))
    finally:
        telemetry.shutdown()


def test_an_a2a_call_arrives_inside_the_caller_s_trace(spans: Any) -> None:
    """The whole reason §15 propagates rather than correlates.

    An A2A task carries `traceparent` in its metadata. If the callee does not extract it, the agent's
    span becomes the root of a *new* trace — which renders correctly, nests correctly, and is the
    wrong answer. Nothing about the resulting view tells you it happened, which is why this is
    asserted rather than eyeballed.
    """
    telemetry, exporter = spans
    with _client(telemetry) as client:
        response = client.get("/manifest", headers={**AUTH, "traceparent": INBOUND_TRACEPARENT})
    assert response.status_code == 200

    our_spans = [s for s in exporter.get_finished_spans() if s.name == "GET /manifest"]
    assert len(our_spans) == 1
    span = our_spans[0]

    assert format(span.context.trace_id, "032x") == INBOUND_TRACE_ID, (
        "the agent started a trace of its own, so the coordinator's span and this one are two "
        "traces rather than one multi-agent trace"
    )
    assert span.parent is not None
    assert format(span.parent.span_id, "016x") == INBOUND_PARENT_SPAN


def test_an_agent_built_without_telemetry_installs_nothing(spans: Any) -> None:
    _, exporter = spans
    with _client(None) as client:
        assert client.get("/healthz").status_code == 200
    assert exporter.get_finished_spans() == (), (
        "an agent built without telemetry emitted a span; the default has to be inert, or every "
        "test in this suite would be exporting to a collector that is not there"
    )
