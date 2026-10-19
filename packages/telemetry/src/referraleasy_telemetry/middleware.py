"""The server-span middleware, in one place because two services need the same one (§15).

The gateway and the seven agent services are all ASGI apps that have to do the same four things per
request, and every one of them is a thing that fails *quietly* when it is got wrong:

* **Extract the inbound ``traceparent``.** A service that installs the propagator and never asks it
  starts a fresh root trace per request. A fresh root trace is indistinguishable from a correct one
  when you look at it — the trace renders, the spans nest, and the only symptom is that a caller's
  span and the callee's are not in the same trace. It is the failure this package's own docstring
  calls out as the one that "cannot fail loudly".
* **End the span even when the handler raises.** A span left open is a span that never exports, so an
  error path is exactly where tracing silently stops working.
* **Never let a telemetry failure reach the caller.** §15: *"telemetry kesintisi booking'i düşürmez"*.
  The span comes from :class:`~referraleasy_telemetry.isolation.IsolatedTracer`.
* **Record the route, not the payload.** The path is an endpoint name; the body, the query string and
  the headers are where a member id ends up, and the masking policy applies to what the *emitter*
  sends rather than to what a middleware holds.

This lives here rather than being written twice for the same reason the guardrail layers do: two
copies of a behavioural rule drift, and the copy that drifts is the one nobody reads. It is
framework-shaped but framework-free — ``app.middleware("http")`` is the only thing it asks of the app,
so this module imports no web framework and the package keeps its dependency list.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final, Protocol

from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind, Status, StatusCode, use_span

from referraleasy_telemetry.isolation import IsolatedTracer
from referraleasy_telemetry.setup import Telemetry

__all__ = ["WORKFLOW_PATH_SEGMENT", "MiddlewareHost", "install_server_spans"]

#: The caller's request and its continuation, as Starlette passes them.
Dispatch = Callable[[Any, Callable[[Any], Awaitable[Any]]], Awaitable[Any]]


class MiddlewareHost(Protocol):
    """The one thing this module asks of the app: ``app.middleware("http")``.

    A protocol rather than ``Any`` because an untyped decorator makes the function it decorates
    untyped, and mypy says so — the dispatch below would lose its whole signature, including the
    ``Awaitable`` that makes `await call_next(request)` checkable. Naming the seam also states what
    this module is allowed to depend on: ``middleware`` and nothing else, which is what keeps the
    package free of a web-framework import.
    """

    def middleware(self, kind: str) -> Callable[[Dispatch], Dispatch]: ...


#: Where a workflow id sits in a path like ``/v1/workflows/{id}/bookings``. Declared as data so the
#: rule is one thing rather than a slice arithmetic repeated at each call site.
WORKFLOW_PATH_SEGMENT: Final = 3


#: The liveness paths both services expose, excluded from tracing by default.
#:
#: This is a deliberate exclusion and it is worth arguing, because "trace everything" is the safer
#: default in most systems. Compose probes `/healthz` every ten seconds, forever — measured on this
#: checkout, that was **2001 of the index's 2016 spans** after a few hours of a stack nobody was
#: using. §15 puts a sample count on every chart precisely so a viewer can tell a quiet period from a
#: busy one, and a request-rate chart that is 99% the monitoring system is a chart about the
#: monitoring system. That is the same failure §18's "no fabricated demo charts" rule is written
#: against, arriving from the other direction: not an invented number, but a real number about
#: something nobody asked about.
#:
#: What is lost is small and is recorded elsewhere: a container that dies and restarts is visible in
#: `docker compose ps`, in the healthcheck status and in the container's own logs. What a probe span
#: would add is a second copy of a fact the orchestrator already keeps.
DEFAULT_PROBE_PATHS: Final[frozenset[str]] = frozenset({"/healthz"})


def install_server_spans(
    app: MiddlewareHost,
    telemetry: Telemetry | None,
    *,
    probe_paths: frozenset[str] = DEFAULT_PROBE_PATHS,
) -> None:
    """Install one SERVER span per request on ``app``. A no-op when ``telemetry`` is ``None``.

    ``probe_paths`` are served but not traced — see :data:`DEFAULT_PROBE_PATHS` for why that is a
    decision rather than an oversight. Pass ``frozenset()`` to trace everything.

    ``None`` is the default a test gets, and it installs nothing at all rather than configuring
    itself from the environment: a second place that reads ``OTEL_*`` is a second answer to "is
    telemetry on", and the one that wins is whichever ran last.

    The middleware is appended, so it wraps whatever the caller installed *before* it. A service that
    wants a correlation id on the span should install that first — this reads
    ``request.state.request_id`` if it is there and does not require it.
    """

    if telemetry is None:
        return

    isolated = IsolatedTracer(telemetry.tracer, telemetry.drops)

    @app.middleware("http")
    async def _server_span(request: Any, call_next: Any) -> Any:
        if request.url.path in probe_paths:
            # Served, never traced. The request still runs and still comes back with its status; only
            # the span is skipped, so a probe cannot be the reason a chart is wrong.
            return await call_next(request)
        # Extraction first: this is what joins the caller's trace rather than starting one.
        parent = extract(dict(request.headers))
        span = isolated.start_span(
            f"{request.method} {request.url.path}", kind=SpanKind.SERVER, context=parent
        )
        # `use_span` makes the span current for anything downstream — including an outbound call that
        # injects `traceparent` — and ends it on the way out, exception or not.
        with use_span(span, end_on_exit=True):
            span.set_attribute("http.request.method", request.method)
            span.set_attribute("url.path", request.url.path)
            parts = request.url.path.split("/")
            if len(parts) > WORKFLOW_PATH_SEGMENT and parts[WORKFLOW_PATH_SEGMENT]:
                # §15 correlates a request to the workflow it acted on. The id is one this service
                # minted and joins a span to a row; it is not a patient identifier.
                span.set_attribute("workflow.id", parts[WORKFLOW_PATH_SEGMENT])
            try:
                response = await call_next(request)
            # Broad on purpose and re-raised: this is the only place that can *record* a failure the
            # error handlers will later turn into an envelope, and swallowing it here would turn a
            # recorded 500 into an unrecorded one.
            except Exception as error:
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR))
                raise
            request_id = getattr(request.state, "request_id", None)
            if request_id:
                span.set_attribute("referraleasy.request_id", request_id)
            span.set_attribute("http.response.status_code", response.status_code)
            if response.status_code >= 500:
                span.set_status(Status(StatusCode.ERROR))
            return response
