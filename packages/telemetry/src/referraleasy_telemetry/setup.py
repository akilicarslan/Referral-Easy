"""The adoption seam: one call that gives a service spans, events and propagation (§15).

This is the file a service imports, and it is the whole of what a service has to know::

    from referraleasy_telemetry import configure_telemetry

    telemetry = configure_telemetry("gateway", version="0.1.0")
    emitter = telemetry.emitter

    with telemetry.tracer.start_as_current_span("referral.submit") as span:
        span.set_attribute("workflow.state", "INTAKE")
        emitter.agent_trigger(agent_role="care_coordinator", correlation=ids)

    telemetry.shutdown()          # at process exit; flushes what is buffered

**Nothing in this repository calls it yet.** Phase 7.1 builds the package and proves the pipeline;
wiring it into ``services/gateway``, ``services/mock-api``, ``apps/web`` and
``packages/agent-runtime`` belongs to the agents that own those files, and is recorded as not-yet-
adopted in ``docs/adr/ADR-0032-telemetry-opensearch-analytics.md``. What is provided here is the seam and
the contract it makes.

**Everything comes from the environment**, because the same image runs as every agent role
(§18) and a per-service config file would mean the image is no longer role-agnostic:

=========================================  =====================================================
``OTEL_EXPORTER_OTLP_ENDPOINT``            Base URL; ``/v1/traces`` and ``/v1/logs`` are appended.
                                           Matches the compose value ``http://otel-collector:4318``.
``OTEL_SERVICE_NAME``                      Overrides the ``service_name`` argument.
``OTEL_SDK_DISABLED``                      ``true`` turns the whole thing into no-ops.
``APP_ENV``                                Default synthetic/live labelling.
=========================================  =====================================================

**Off by default when nothing is configured.** ``OTEL_EXPORTER_OTLP_ENDPOINT`` unset means the
bundle is inert: real ``TracerProvider`` objects and a real emitter, but no exporter, so a test or a
developer running a service without the stack gets spans that go nowhere and no connection errors.
Making telemetry opt-in is what stops it from being the reason a laptop demo fails to start.

**A2A/HTTP propagation is on by default.** The global propagator is set explicitly to
``TraceContextTextMapPropagator`` + ``BaggagePropagator`` rather than left at whatever the SDK
defaults to in this version: propagation is the one piece of this that cannot fail loudly — a
missing or wrong propagator produces a *new root trace* on the callee, which looks exactly like a
correct trace and is not one.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from opentelemetry import trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from referraleasy_telemetry.conventions import (
    RESOURCE_SPEC_SECTION,
    SCOPE_NAME,
    SECTION,
    Synthetic,
)
from referraleasy_telemetry.events import TelemetryEmitter
from referraleasy_telemetry.export import OTLPHTTPJSONLogExporter, OTLPHTTPJSONSpanExporter
from referraleasy_telemetry.isolation import DropCounter, IsolatedLogger
from referraleasy_telemetry.masking import DEFAULT_POLICY, MaskingPolicy

#: The env var the compose files already set on every service.
ENV_ENDPOINT: Final[str] = "OTEL_EXPORTER_OTLP_ENDPOINT"
ENV_SERVICE_NAME: Final[str] = "OTEL_SERVICE_NAME"
ENV_DISABLED: Final[str] = "OTEL_SDK_DISABLED"

#: Bounded queues. §15 wants a bounded buffer with a visible drop rather than unbounded growth, and
#: the collector is the *second* bound: the SDK's queue is the first, and a full one drops the
#: newest record rather than blocking the thread that emitted it. The numbers are the SDK defaults
#: written down explicitly so they are a decision rather than an inherited one.
SPAN_QUEUE_SIZE: Final[int] = 2048
SPAN_MAX_EXPORT_BATCH: Final[int] = 512
LOG_QUEUE_SIZE: Final[int] = 4096
LOG_MAX_EXPORT_BATCH: Final[int] = 512

#: How long a shutdown may take to flush before the process stops waiting. A demo must not hang on
#: exit because the collector went away, and the alternative to a bounded flush is a `SIGKILL`.
SHUTDOWN_TIMEOUT_MS: Final[int] = 3000


@dataclass(slots=True)
class TelemetryConfig:
    """What ``configure_telemetry`` needs. Built from the environment by :func:`from_env`."""

    service_name: str
    service_version: str = "0.0.0"
    endpoint: str | None = None
    enabled: bool = True
    synthetic: Synthetic | str = Synthetic.LIVE_DEMO
    deployment_annotation: str | None = None
    policy: MaskingPolicy = field(default_factory=lambda: DEFAULT_POLICY)
    resource_attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        """True when records would actually leave the process."""
        return self.enabled and bool(self.endpoint)


def from_env(
    service_name: str, *, version: str = "0.0.0", env: Mapping[str, str] | None = None
) -> TelemetryConfig:
    """Build a config from the environment. Reading env is done once, here."""
    env = os.environ if env is None else env
    disabled = (env.get(ENV_DISABLED) or "").strip().lower() in {"1", "true", "yes"}
    app_env = (env.get("APP_ENV") or "").strip().lower()
    synthetic = Synthetic.SYNTHETIC if app_env in {"test", "ci", "offline"} else Synthetic.LIVE_DEMO
    return TelemetryConfig(
        service_name=(env.get(ENV_SERVICE_NAME) or service_name),
        service_version=version,
        endpoint=(env.get(ENV_ENDPOINT) or "").strip() or None,
        enabled=not disabled,
        synthetic=synthetic,
        deployment_annotation=(env.get("OTEL_DEPLOYMENT_ANNOTATION") or None),
    )


def _exporter_counters(exporter: object) -> dict[str, Any] | None:
    """``exporter.stats.as_dict()``, or ``None`` when there is no exporter to ask.

    Two ``getattr`` calls rather than one attribute chain, because this feeds a health endpoint: an
    inactive bundle has no exporter at all, and an exporter swapped in for a vendor one has no
    ``stats``, and both cases should report ``null`` rather than raise. The old one-liner guarded
    the *exporter* being ``None`` and then reached for ``.as_dict()`` unconditionally, so the second
    case would have been an ``AttributeError`` in the health check. ``isinstance`` on the result
    rather than a cast: the shape is checked, not asserted.
    """
    stats = getattr(exporter, "stats", None)
    as_dict = getattr(stats, "as_dict", None)
    counters = as_dict() if as_dict is not None else None
    return counters if isinstance(counters, dict) else None


@dataclass(slots=True)
class Telemetry:
    """The bundle a service holds. Everything a caller needs, and a bounded ``shutdown``."""

    tracer: Any
    emitter: TelemetryEmitter
    drops: DropCounter
    tracer_provider: TracerProvider | None = None
    logger_provider: LoggerProvider | None = None
    config: TelemetryConfig | None = None
    span_exporter: Any = None
    log_exporter: Any = None

    @property
    def active(self) -> bool:
        """True when this bundle is attached to a live exporter."""
        return self.span_exporter is not None

    def shutdown(self) -> None:
        """Flush and tear down. Safe to call more than once, and safe when inactive."""
        for provider in (self.logger_provider, self.tracer_provider):
            if provider is None:
                continue
            try:
                provider.shutdown()
            except Exception:
                self.drops.record("shutdown")
        for exporter in (self.log_exporter, self.span_exporter):
            closer = getattr(exporter, "shutdown", None)
            if closer is not None:
                try:
                    closer()
                except Exception:
                    self.drops.record("shutdown")

    def export_stats(self) -> dict[str, Any]:
        """Exporter counters, for a health endpoint or a test assertion.

        ``None`` per side means "nothing is being exported" or "the exporter has no counters",
        which is not the same as zero records sent — the counters themselves say that.
        """
        return {
            "spans": _exporter_counters(self.span_exporter),
            "logs": _exporter_counters(self.log_exporter),
        }


def configure_telemetry(
    service_name: str,
    *,
    version: str = "0.0.0",
    config: TelemetryConfig | None = None,
    set_global: bool = True,
) -> Telemetry:
    """Wire spans, events and propagation for ``service_name``. See the module docstring.

    ``set_global=False`` builds the providers without installing them as the process-global ones.
    Tests use it so one test's provider cannot leak into the next; a service wants the default.
    """
    cfg = config or from_env(service_name, version=version)

    # Propagation is installed even when export is off: a service that does not export its own
    # spans still has to *continue* the trace a caller handed it, and dropping the propagator would
    # silently start a new trace for every inbound request.
    if set_global:
        set_global_textmap(
            CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
        )

    drops = DropCounter()
    resource = _resource(cfg)
    tracer_provider = TracerProvider(resource=resource, sampler=_sampler(cfg))
    logger_provider = LoggerProvider(resource=resource)

    span_exporter = None
    log_exporter = None
    if cfg.is_active:
        span_exporter = OTLPHTTPJSONSpanExporter(cfg.endpoint or "", policy=cfg.policy)
        log_exporter = OTLPHTTPJSONLogExporter(cfg.endpoint or "", policy=cfg.policy)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                span_exporter,
                max_queue_size=SPAN_QUEUE_SIZE,
                max_export_batch_size=SPAN_MAX_EXPORT_BATCH,
            )
        )
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                log_exporter,
                max_queue_size=LOG_QUEUE_SIZE,
                max_export_batch_size=LOG_MAX_EXPORT_BATCH,
            )
        )

    if set_global:
        trace.set_tracer_provider(tracer_provider)

    tracer = tracer_provider.get_tracer(SCOPE_NAME, cfg.service_version)
    # The SDK logger is wrapped, not used directly: this is the line that makes "a telemetry outage
    # does not cause booking to fail" true for every emitter created from this bundle.
    logger = IsolatedLogger(logger_provider.get_logger(SCOPE_NAME, cfg.service_version), drops)

    return Telemetry(
        tracer=tracer,
        emitter=TelemetryEmitter(
            logger,
            deployment_annotation=cfg.deployment_annotation,
            # The deployment's label, so a service's records carry it without every call site
            # repeating it. The gateway and the eval CLI still pass their own explicitly — an
            # explicit argument wins — but a service that says nothing now gets the right answer
            # instead of a record that no synthetic-filtered chart can see.
            synthetic=cfg.synthetic,
        ),
        drops=drops,
        tracer_provider=tracer_provider,
        logger_provider=logger_provider,
        config=cfg,
        span_exporter=span_exporter,
        log_exporter=log_exporter,
    )


def _resource(cfg: TelemetryConfig) -> Resource:
    """The OTel ``Resource``. ``service.name`` is what Data Prepper writes as ``serviceName``."""
    attributes: dict[str, Any] = {
        "service.name": cfg.service_name,
        "service.version": cfg.service_version,
        RESOURCE_SPEC_SECTION: SECTION,
        "synthetic": str(cfg.synthetic),
    }
    if cfg.deployment_annotation:
        attributes["deployment.annotation"] = cfg.deployment_annotation
    attributes.update(cfg.resource_attributes)
    return Resource.create(attributes)


def _sampler(cfg: TelemetryConfig) -> Any:
    """Always-on when exporting.

    Not a rate-limited sampler, and that is a deliberate §15 consequence: the p95 and the outcome
    counts are only true if every attempt is recorded, and a sampler that drops 90% of spans makes
    a p95 of the remaining 10% a different number wearing the same name. The bound that protects
    the demo is the collector's ``memory_limiter``, which drops *visibly* — a counter on
    ``:8888/metrics`` — rather than a sampler that drops invisibly.
    """
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON

    return ALWAYS_ON
