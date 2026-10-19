"""Propagation over two real transports, read back out of OpenSearch (§15).

§15 asks for one ``trace_id`` end to end, with ``span_id`` for the sub-step. ``test_identity.py``
checks ``inject``/``extract`` inside one process, which is a check of the propagator and not of the
transport — the same dictionary is handed back to the same code. These tests put the header on a
wire and take it off again:

* **Plain HTTP** — a real ``ThreadingHTTPServer`` on a real port, a real ``httpx`` request in
  between, and the header serialised and parsed by an HTTP implementation on both sides. That is the
  part a dict round trip cannot exercise.
* **A2A** — a real ``a2a.types.Message`` whose ``metadata`` is a ``google.protobuf.Struct``,
  serialised to protobuf bytes and parsed back on the receiving side.

Both then assert on the **stored spans** in OpenSearch: one trace, two spans, the child's
``parentSpanId`` pointing at the parent. §15's claim is about what the collector wrote, so a check
that stops at the SDK would be checking the wrong half.

**The honest boundary.** The callee runs in the same process as the caller, so the two halves share
one provider. This proves the transport and the lineage; it does not prove two deployed services with
two ``configure_telemetry`` calls. That is what the adopting services will do, and it is not
verified here.

Spans go out through the ``live_pipeline`` fixture — real collector, real Data Prepper, real index.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest

from referraleasy_telemetry.identity import (
    TRACEPARENT,
    a2a_carrier_from_metadata,
    a2a_metadata_with_context,
    current_ids,
    extract_context,
    inject_context,
)

pytestmark = pytest.mark.integration


class _CalleeHandler(BaseHTTPRequestHandler):
    """A minimal service: take the trace context off the request, do the work, report what it saw.

    It answers with the ids it extracted, so a client-side assertion can be made without the
    pipeline and the pipeline assertion can be a separate, stronger one. When propagation breaks,
    the two failures say different things: "the header did not survive the socket" and "the two spans
    are not one trace in the index".
    """

    #: Set by the fixture, because a handler class cannot take an argument.
    tracer: ClassVar[Any] = None
    received: ClassVar[dict[str, Any]] = {}
    carrier: ClassVar[dict[str, str]] = {}

    def do_GET(self) -> None:
        headers = {key.lower(): value for key, value in self.headers.items()}
        type(self).carrier = {
            key: value
            for key, value in headers.items()
            if key in {"traceparent", "tracestate", "baggage"}
        }
        # Starting the span *with the extracted context* is the line that turns this into a
        # continuation. The server has no ambient span, so the context has to be passed in
        # explicitly rather than inherited from a current span.
        with self.tracer.start_as_current_span(
            "callee.http", context=extract_context(headers)
        ) as span:
            trace_id, span_id = current_ids()
            parent = span.parent
            type(self).received = {
                "trace_id": trace_id,
                "span_id": span_id,
                "parent_span_id": format(parent.span_id, "016x") if parent else None,
                "parent_is_remote": bool(parent and parent.is_remote),
            }
        body = json.dumps(type(self).received).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """Silence the default stderr access log; the assertions are the record."""


@pytest.fixture
def callee(live_pipeline) -> Iterator[str]:
    """A real HTTP server on an ephemeral port, yielding its base URL."""
    _CalleeHandler.tracer = live_pipeline.tracer
    _CalleeHandler.received = {}
    _CalleeHandler.carrier = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CalleeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


class TestPropagationOverPlainHTTP:
    def test_the_header_survives_a_real_socket(self, live_pipeline, callee: str) -> None:
        """The carriage itself, before the pipeline is involved."""
        import httpx

        with live_pipeline.span("caller.http"):
            caller_ids = current_ids()
            carrier = inject_context({})
            assert carrier.get(TRACEPARENT), "nothing to send"
            response = httpx.get(f"{callee}/work", headers=carrier, timeout=5.0)

        assert response.status_code == 200
        received = response.json()
        assert received["trace_id"] == caller_ids[0]
        assert received["parent_span_id"] == caller_ids[1], "the server's span is a child of ours"
        assert received["parent_is_remote"] is True, "arrived from outside this process boundary"

    def test_the_callee_names_its_own_span_in_the_caller_s_trace(
        self, live_pipeline, callee: str
    ) -> None:
        """A continuation, not a copy: same trace, its own span id."""
        import httpx

        with live_pipeline.span("caller.http"):
            caller_ids = current_ids()
            response = httpx.get(f"{callee}/work", headers=inject_context({}), timeout=5.0)

        received = response.json()
        assert received["trace_id"] == caller_ids[0]
        assert received["span_id"] != caller_ids[1]

    def test_both_halves_are_one_trace_in_opensearch(self, live_pipeline, callee: str) -> None:
        """The end-to-end claim: one trace id, two spans, one parent link — read from the index.

        The assertion is on ``parentSpanId`` in the *stored* document, so what is verified is that
        the collector wrote the relationship, not that the SDK computed it.
        """
        import httpx

        with live_pipeline.span("caller.http"):
            caller_ids = current_ids()
            httpx.get(f"{callee}/work", headers=inject_context({}), timeout=5.0)

        live_pipeline.flush()
        spans = {
            document["name"]: document for document in live_pipeline.trace(caller_ids[0], count=2)
        }

        assert set(spans) == {"caller.http", "callee.http"}
        assert spans["caller.http"]["traceId"] == caller_ids[0]
        assert spans["callee.http"]["traceId"] == caller_ids[0], "same trace, across the socket"
        assert spans["callee.http"]["parentSpanId"] == caller_ids[1]
        assert spans["caller.http"]["parentSpanId"] in ("", None)


class TestPropagationInsideAnA2AMessage:
    """A2A carries the context in ``Message.metadata``, a protobuf ``Struct``.

    Checked against the installed ``a2a`` package rather than by inspection: a real ``Message`` is
    built, the context is injected into its ``metadata``, and the message is serialised to protobuf
    bytes and parsed back — which is what crosses the wire between two agents.
    """

    def test_the_context_survives_protobuf_serialisation(self, live_pipeline) -> None:
        a2a_types = pytest.importorskip("a2a.types", reason="the a2a package is not installed")

        with live_pipeline.span("caller.a2a"):
            caller_ids = current_ids()
            message = a2a_types.Message(
                message_id="propagation-test", role=a2a_types.Role.ROLE_USER, parts=[]
            )
            # The application's own metadata, which injecting a trace context must not disturb.
            message.metadata["workflow_id"] = "wf-1"
            for key, value in a2a_metadata_with_context().items():
                message.metadata[key] = value
            payload = message.SerializeToString()

        received = a2a_types.Message()
        received.ParseFromString(payload)

        assert received.message_id == "propagation-test"
        assert received.metadata["workflow_id"] == "wf-1", "the trace context displaced the payload"
        carrier = a2a_carrier_from_metadata(received.metadata)
        assert carrier.get(TRACEPARENT), "the Struct did not carry a traceparent"
        assert carrier[TRACEPARENT].split("-")[1] == caller_ids[0]

    def test_a_malformed_traceparent_in_the_struct_is_dropped_on_arrival(
        self, live_pipeline
    ) -> None:
        """A real ``Struct``, a value that is not a string, and no coercion on the way out.

        A ``Struct`` can hold anything, so a message from a peer that is not propagating correctly
        can put a number, a list, or a nested object under the reserved key. The receiver must read
        only the reserved keys and only keep strings.
        """
        a2a_types = pytest.importorskip("a2a.types", reason="the a2a package is not installed")
        message = a2a_types.Message(message_id="m", role=a2a_types.Role.ROLE_USER, parts=[])
        message.metadata[TRACEPARENT] = 12345.0
        message.metadata["tracestate"] = "vendor=1"
        payload = message.SerializeToString()

        received = a2a_types.Message()
        received.ParseFromString(payload)
        carrier = a2a_carrier_from_metadata(received.metadata)

        assert TRACEPARENT not in carrier, "a float is not a traceparent"
        assert carrier["tracestate"] == "vendor=1"

    def test_the_callees_span_continues_the_message_s_trace(self, live_pipeline) -> None:
        """The receiving agent's span, and the lineage in the index.

        Two spans, one trace: the sender's and the receiver's, with the receiver's
        ``parentSpanId`` equal to the sender's ``spanId``. That is the A2A half of "end-to-end
        trace_id", read from OpenSearch rather than from the SDK.
        """
        a2a_types = pytest.importorskip("a2a.types", reason="the a2a package is not installed")

        with live_pipeline.span("caller.a2a"):
            caller_ids = current_ids()
            message = a2a_types.Message(message_id="m2", role=a2a_types.Role.ROLE_USER, parts=[])
            for key, value in a2a_metadata_with_context().items():
                message.metadata[key] = value
            payload = message.SerializeToString()

        received = a2a_types.Message()
        received.ParseFromString(payload)
        carrier = a2a_carrier_from_metadata(received.metadata)
        with live_pipeline.tracer.start_as_current_span(
            "callee.a2a", context=extract_context(carrier)
        ):
            pass

        live_pipeline.flush()
        spans = {
            document["name"]: document for document in live_pipeline.trace(caller_ids[0], count=2)
        }

        assert set(spans) == {"caller.a2a", "callee.a2a"}
        assert spans["callee.a2a"]["traceId"] == caller_ids[0]
        assert spans["callee.a2a"]["parentSpanId"] == caller_ids[1]

    def test_an_a2a_message_without_a_context_starts_a_trace_of_its_own(
        self, live_pipeline
    ) -> None:
        """The degraded case, which is the common one in development: a peer that does not
        propagate must not break the receiving agent. The span is valid, it is simply a root."""
        a2a_types = pytest.importorskip("a2a.types", reason="the a2a package is not installed")
        message = a2a_types.Message(message_id="m3", role=a2a_types.Role.ROLE_USER, parts=[])

        assert a2a_carrier_from_metadata(message.metadata) == {}
        context = extract_context(a2a_carrier_from_metadata(message.metadata))
        with live_pipeline.tracer.start_as_current_span(
            "callee.no_context", context=context
        ) as span:
            assert span.get_span_context().is_valid
            assert span.parent is None, "a root span, not a child of nothing"
