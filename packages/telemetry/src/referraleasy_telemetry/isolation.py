# ruff: noqa: RUF002  # Turkish quoted from §15 of ReferralEasy_Architecture_v3.md is kept verbatim
"""Failure isolation: a telemetry outage must not take a booking down (§15).

§15 states the requirement in one line:

    Collector bounded buffer/drop metriği gösterir; **telemetry kesintisi booking'i düşürmez.**
    Gerekli kritik iş audit'i PostgreSQL'de kalır.

Three separate mechanisms are needed to make that true, because there are three ways telemetry
reaches back into the application it is supposed to be observing:

1. **The emitter raising.** ``TelemetryEmitter`` validates what it is given and refuses nonsense,
   which is right for a programming error and wrong for production traffic. :class:`IsolatedLogger`
   wraps the SDK logger so that whatever happens below it — a validation error, a bug in an
   encoder, a processor that throws — is counted and swallowed. This is the last mile, and it is
   the one that matters, because every other mechanism below it could be correct and this one could
   still propagate.
2. **The network.** The OTLP exporters have a bounded timeout (see ``export.DEFAULT_TIMEOUT``) and
   never raise. A collector that is down costs one connection refusal, not a stalled request.
3. **The queue growing.** The SDK's batch processors are bounded
   (``OTEL_BSP_MAX_QUEUE_SIZE``); a full queue drops rather than blocking the producer thread.

**The audit trail is not the telemetry path.** A dropped telemetry record loses a chart point; a
dropped audit record loses the legal record of what happened to a patient. §15 resolves this by
keeping the authoritative record in PostgreSQL, and this module enforces the visible half of that
rule: :func:`assert_not_audit_path` refuses to be pointed at the audit tables, so a caller cannot
mistake this package for a durable store. The booking's audit row is written by
``packages/domain`` against PostgreSQL and is unaffected by anything here.

**Drops are counted, never silent.** :class:`DropCounter` is what a health endpoint reports and what
``test_isolation.py`` asserts on. §15 asks for the collector's own drop metric to be visible; that
one lives on the collector (``:8888/metrics``) and is read by :func:`read_collector_drops`, while
:class:`DropCounter` covers the drops that happen before a record ever reaches it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

logger = logging.getLogger(__name__)

#: The default collector metrics endpoint. It is the same port the compose service publishes, and
#: the same one the collector config documents for ``otelcol_processor_memory_limiter_refused_*``.
DEFAULT_COLLECTOR_METRICS_ENDPOINT: Final[str] = "http://localhost:8888"

#: Names of the counters that mean "telemetry was lost". Kept as the measured names rather than the
#: names the config file *expected*: the config records that on collector 0.162.0 the short
#: ``otelcol_processor_refused_spans`` does not exist and the real one is the prefixed form below.
REFUSAL_METRICS: Final[tuple[str, ...]] = (
    "otelcol_processor_memory_limiter_refused_spans",
    "otelcol_processor_memory_limiter_refused_log_records",
    "otelcol_receiver_refused_spans",
    "otelcol_receiver_refused_log_records",
    "otelcol_exporter_send_failed_spans",
    "otelcol_exporter_send_failed_log_records",
)


@dataclass(slots=True)
class DropCounter:
    """Counts of telemetry records this process could not hand over, by reason.

    A dict of reasons rather than a single number because the reasons have different fixes: an
    ``encode`` failure is a bug in this package, a ``validation`` failure is a bug in the caller,
    and an ``export`` failure is the collector being unavailable. Collapsing them would make the
    counter useless for deciding what to do.
    """

    reasons: dict[str, int] = field(default_factory=dict)
    last_error: str | None = None

    def record(self, reason: str, exc: BaseException | None = None) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1
        if exc is not None:
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]

    @property
    def total(self) -> int:
        return sum(self.reasons.values())

    def as_dict(self) -> dict[str, Any]:
        return {"total": self.total, "reasons": dict(self.reasons), "last_error": self.last_error}


class IsolatedLogger:
    """Wraps any logger exposing ``emit(**kwargs)`` so that it can never raise into the caller.

    Used by ``setup.configure_telemetry`` to wrap the SDK logger, which means every
    ``TelemetryEmitter`` in a configured process is already isolated and nothing a service writes
    has to remember to be careful.

    The wrapper mirrors the SDK's ``emit`` signature (keyword-only ``body``, ``attributes``,
    ``severity_number``, ``timestamp``, ``context``) and passes through only what the caller gave,
    so a partial call stays partial.
    """

    def __init__(self, inner: Any, drops: DropCounter | None = None) -> None:
        self._inner = inner
        self.drops = drops if drops is not None else DropCounter()

    def emit(self, **kwargs: Any) -> None:
        """Emit, swallowing every failure. Returns nothing in either case."""
        try:
            self._inner.emit(**{key: value for key, value in kwargs.items() if value is not None})
        except Exception as exc:
            reason = _classify(exc)
            self.drops.record(reason, exc)
            logger.debug("telemetry emission dropped (%s)", reason, exc_info=True)


def _classify(exc: BaseException) -> str:
    """A stable reason string for a caught exception."""
    return "validation" if isinstance(exc, ValueError) else "emission"


class IsolatedTracer:
    """The span-side equivalent of :class:`IsolatedLogger`.

    OTel span operations are already exception-safe with respect to the *exporter* (the batch
    processor exports on its own thread), but they are not safe with respect to a
    :class:`~opentelemetry.sdk.trace.SpanProcessor` or a custom sampler. A caller that must not
    fail — the booking gate is the one §15 names — uses this around span creation.
    """

    def __init__(self, tracer: Any, drops: DropCounter | None = None) -> None:
        self._tracer = tracer
        self.drops = drops if drops is not None else DropCounter()

    def start_span(self, name: str, **kwargs: Any) -> Any:
        """Start a span, or return a no-op span if the tracer fails."""
        try:
            return self._tracer.start_span(name, **kwargs)
        except Exception as exc:
            self.drops.record("span_start", exc)
            logger.debug("span creation dropped", exc_info=True)
            from opentelemetry.trace import INVALID_SPAN

            return INVALID_SPAN


def guard_telemetry(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: run ``func``, and if telemetry itself fails, return ``None`` instead of raising.

    For the narrow case where a call *is* the telemetry — emitting a metric inside a booking
    handler. A failure is logged at debug and dropped; the caller's transaction is untouched.
    """

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception:
            logger.debug("guarded telemetry call failed", exc_info=True)
            return None

    wrapper.__name__ = getattr(func, "__name__", "guarded")
    wrapper.__doc__ = func.__doc__
    return wrapper


@contextmanager
def outage_guard(
    drops: DropCounter | None = None, *, reason: str = "guarded_block"
) -> Iterator[None]:
    """Run a block in which a telemetry failure cannot escape.

    The point of the context manager rather than a decorator is that the protected region is
    usually *part* of a function — the emit calls — while the booking write beside them must still
    raise if it fails. Wrapping the whole handler would swallow the wrong exception.
    """
    try:
        yield
    except Exception as exc:
        if drops is not None:
            drops.record(reason, exc)
        logger.debug("telemetry block failed inside an outage guard", exc_info=True)


#: Tables that hold authoritative records. Naming one to this package is a mistake worth refusing.
AUDIT_TABLES: Final[frozenset[str]] = frozenset({"audit", "evidence", "release_event"})


def assert_not_audit_path(target: str) -> None:
    """Refuse a telemetry sink pointed at an audit table (§15: the audit stays in PostgreSQL).

    This is the guard that makes the sentence in §15 structural rather than aspirational. A
    tempting simplification — "why not index the audit rows into OpenSearch too, then one query
    answers everything" — produces a system whose only record of a patient's booking is a document
    in a 7-day-retention index written by a pipeline that is explicitly allowed to drop data when
    the collector is under pressure.
    """
    identifier = target.strip().strip('"').split(".")[-1].strip('"').lower()
    if identifier in AUDIT_TABLES:
        raise ValueError(
            f"{target!r} names an audit table. §15 keeps the authoritative audit in PostgreSQL "
            f"because the telemetry path is allowed to drop data (bounded buffer, drop metric, 7 "
            f"day retention) and an audit record is not. The audit row is written by the domain "
            f"layer; this package only reads telemetry."
        )


# ---------------------------------------------------------------------------
# The collector's own drop metric
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CollectorDrops:
    """The collector's refusal counters, as read from its Prometheus endpoint.

    ``available`` distinguishes "the counters are all zero" from "the endpoint could not be read".
    They are different facts and a dashboard that shows 0 for both is lying: the collector config
    records that these counters **do not exist until the first refusal**, so an idle healthy
    collector and an unreachable one both return no series.
    """

    available: bool
    counters: dict[str, float] = field(default_factory=dict)
    error: str | None = None

    @property
    def total_refused(self) -> float:
        """Sum of the refusal counters. Zero when the endpoint was unreachable — check
        ``available`` before reporting this as a healthy zero."""
        return sum(self.counters.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "total_refused": self.total_refused,
            "counters": dict(self.counters),
            "error": self.error,
        }


def read_collector_drops(
    endpoint: str = DEFAULT_COLLECTOR_METRICS_ENDPOINT,
    *,
    timeout: float = 2.0,
    client: httpx.Client | None = None,
) -> CollectorDrops:
    """Read ``/metrics`` from the collector and extract §15's drop metric.

    Parses the Prometheus text format only far enough to find :data:`REFUSAL_METRICS`. Labelled
    series are summed, because the question being asked is "how much telemetry was lost in total",
    and the per-label breakdown is available from the endpoint itself for anyone who needs it.
    """
    url = f"{endpoint.rstrip('/')}/metrics"
    own_client = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        response = http.get(url)
    except Exception as exc:
        return CollectorDrops(available=False, error=f"{type(exc).__name__}: {exc}"[:200])
    finally:
        if own_client:
            http.close()

    if response.status_code >= 300:
        return CollectorDrops(available=False, error=f"HTTP {response.status_code}")

    counters: dict[str, float] = {}
    for line in response.text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _PROMETHEUS_LINE.match(line)
        if match is None:
            continue
        name, value = match.group("name"), float(match.group("value"))
        if name in REFUSAL_METRICS:
            counters[name] = counters.get(name, 0.0) + value
    return CollectorDrops(available=True, counters=counters)


#: ``metric_name{label="x"} 12.0`` — the labels are captured but not used; the sum is what is
#: reported and the raw endpoint remains the source for a per-label view.
_PROMETHEUS_LINE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+(?P<value>[0-9eE+.\-]+)\s*$"
)


def health_report(
    drops: DropCounter | None = None,
    *,
    collector: CollectorDrops | None = None,
) -> Mapping[str, Any]:
    """The telemetry health of one process, for a ``/health`` handler to return verbatim.

    Deliberately *not* wired into anything's readiness probe by this phase: a readiness probe that
    fails when the collector is down would make a telemetry outage stop serving traffic, which is
    the opposite of what §15 requires. This is a report, and what reads it decides.
    """
    return {
        "process_drops": (drops or DropCounter()).as_dict(),
        "collector": (collector or CollectorDrops(available=False)).as_dict(),
    }
