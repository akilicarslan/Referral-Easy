"""ReferralEasy telemetry — §15's OTel/OpenSearch analytics, as a package.

The architecture section this implements is §15 of ``ReferralEasy_Architecture_v3.md``. Its shape is
one pipeline and one rule:

    Apps (gateway / A2A / mock API / worker) -> OTel Collector -> Data Prepper -> OpenSearch

Two things about this package are worth knowing before reading it, because they are decisions rather
than accidents.

**Measurements travel as log events, not as OTel metrics.** The Phase 0 spike (ADR-0008) verified
against a live OpenSearch 3.9.0 node that native OTLP *metrics* are not ingestible — the only OTLP
code in the distribution is an exporter for OpenSearch's own telemetry. §15 anticipates exactly this
and prescribes the fallback ("if needed, send the same measurements as numeric log events"), which
is
what :mod:`~referraleasy_telemetry.events` implements: every measurement is one numeric event with a
deterministic ``event.id``, counted with ``cardinality`` and distributed with ``percentiles``.

**The pipeline is verified against the live stack, not against a mock.** A test with a fake exporter
proves the serialisation. The tests in ``tests/telemetry/test_pipeline_live.py`` emit through the
real collector, wait for Data Prepper to flush, and read the document back out of OpenSearch — which
is the only evidence that the pipeline works at all.

Modules
-------
``conventions``  The §15 vocabulary: outcome classes, roles, states, attribute and metric names,
                 index families and retention. The one place a name is defined.
``identity``     W3C trace context, the seven correlation ids that §15 keeps distinct, span links
                 across an asynchronous wait, and the A2A metadata carrier.
``masking``      PII/secret masking, applied to the exact payload that is about to be exported.
``events``       The operation-event model, deterministic ``event_id`` derivation, and the emitter
                 that turns each §15 measurement into a record.
``export``       An OTLP/HTTP JSON exporter, because the vendor exporter is not installed here —
                 see that module's docstring for what that costs.
``setup``        The adoption seam: ``configure_telemetry`` wires providers, the propagator and an
                 isolated emitter from the environment.
``isolation``    Why a telemetry outage cannot fail a booking, and where the drop counts live.
``query``        The typed OpenSearch query layer the admin app reads §15 through.
``feedback``     The last-active-thumbs rule and the three feedback numbers.

Adoption
--------
**Nothing in this repository calls ``configure_telemetry`` yet.** Phase 7.1 builds the package and
proves the pipeline; wiring it into the gateway, the agents, the mock API and the workers belongs to
the phases that own those files, and is recorded as not-yet-adopted in
``docs/adr/ADR-0032-telemetry-opensearch-analytics.md``. See :func:`~referraleasy_telemetry.setup.configure_telemetry`
for the four lines a service needs.
"""

from referraleasy_telemetry.conventions import (
    ACTORS,
    AGENT_ROLES,
    ATTR_AGENT_ROLE,
    ATTR_EVENT_ID,
    ATTR_METRIC_NAME,
    ATTR_METRIC_VALUE,
    ATTR_OUTCOME_CLASS,
    ATTR_SYNTHETIC,
    ATTR_WORKFLOW_STATE,
    DIMENSION_ATTRIBUTES,
    EVALUATION_INDEX_PATTERN,
    EVENT_INDEX_PATTERN,
    PRINCIPALS,
    SECTION,
    TELEMETRY_RETENTION_DAYS,
    TRACE_INDEX_PATTERN,
    WORKFLOW_STATES,
    EventName,
    LatencyKind,
    MetricName,
    OutcomeClass,
    Synthetic,
    field_name,
)
from referraleasy_telemetry.events import (
    EventIdDedup,
    EventRecord,
    MissingEventIdentityError,
    TelemetryEmitter,
    UnknownDimensionError,
    build_event,
    derive_event_id,
    validate_dimensions,
)
from referraleasy_telemetry.export import (
    ExportStats,
    OTLPHTTPJSONLogExporter,
    OTLPHTTPJSONSpanExporter,
)
from referraleasy_telemetry.feedback import (
    FeedbackSummary,
    Vote,
    last_active,
    summarise_feedback,
)
from referraleasy_telemetry.identity import (
    A2A_METADATA_KEYS,
    TRACEPARENT,
    CorrelationIds,
    IdentityConflictError,
    a2a_carrier_from_metadata,
    a2a_metadata_with_context,
    current_ids,
    current_trace_id,
    extract_context,
    inject_context,
    remote_span_context,
    resume_after_wait,
)
from referraleasy_telemetry.isolation import (
    CollectorDrops,
    DropCounter,
    IsolatedLogger,
    assert_not_audit_path,
    guard_telemetry,
    health_report,
    outage_guard,
    read_collector_drops,
)
from referraleasy_telemetry.masking import (
    DEFAULT_POLICY,
    REDACTED,
    MaskingExporter,
    MaskingPolicy,
    mask_log_record,
    mask_span_attributes,
    pseudonymise,
)
from referraleasy_telemetry.middleware import install_server_spans
from referraleasy_telemetry.query import (
    Filters,
    Percentiles,
    Sample,
    TelemetryQuery,
    TelemetryQueryError,
    TimeRange,
    TimeSeries,
    UnindexablePatternError,
    keyword_field,
    numeric_field,
    source_field,
)
from referraleasy_telemetry.setup import (
    Telemetry,
    TelemetryConfig,
    configure_telemetry,
    from_env,
)

__version__ = "0.1.0"

__all__ = [
    "A2A_METADATA_KEYS",
    "ACTORS",
    "AGENT_ROLES",
    "ATTR_AGENT_ROLE",
    "ATTR_EVENT_ID",
    "ATTR_METRIC_NAME",
    "ATTR_METRIC_VALUE",
    "ATTR_OUTCOME_CLASS",
    "ATTR_SYNTHETIC",
    "ATTR_WORKFLOW_STATE",
    "DEFAULT_POLICY",
    "DIMENSION_ATTRIBUTES",
    "EVALUATION_INDEX_PATTERN",
    "EVENT_INDEX_PATTERN",
    "PRINCIPALS",
    "REDACTED",
    "SECTION",
    "TELEMETRY_RETENTION_DAYS",
    "TRACEPARENT",
    "TRACE_INDEX_PATTERN",
    "WORKFLOW_STATES",
    "CollectorDrops",
    "CorrelationIds",
    "DropCounter",
    "EventIdDedup",
    "EventName",
    "EventRecord",
    "ExportStats",
    "FeedbackSummary",
    "Filters",
    "IdentityConflictError",
    "IsolatedLogger",
    "LatencyKind",
    "MaskingExporter",
    "MaskingPolicy",
    "MetricName",
    "MissingEventIdentityError",
    "OTLPHTTPJSONLogExporter",
    "OTLPHTTPJSONSpanExporter",
    "OutcomeClass",
    "Percentiles",
    "Sample",
    "Synthetic",
    "Telemetry",
    "TelemetryConfig",
    "TelemetryEmitter",
    "TelemetryQuery",
    "TelemetryQueryError",
    "TimeRange",
    "TimeSeries",
    "UnindexablePatternError",
    "UnknownDimensionError",
    "Vote",
    "__version__",
    "a2a_carrier_from_metadata",
    "a2a_metadata_with_context",
    "assert_not_audit_path",
    "build_event",
    "configure_telemetry",
    "current_ids",
    "current_trace_id",
    "derive_event_id",
    "extract_context",
    "field_name",
    "from_env",
    "guard_telemetry",
    "health_report",
    "inject_context",
    "install_server_spans",
    "keyword_field",
    "last_active",
    "mask_log_record",
    "mask_span_attributes",
    "numeric_field",
    "outage_guard",
    "pseudonymise",
    "read_collector_drops",
    "remote_span_context",
    "resume_after_wait",
    "source_field",
    "summarise_feedback",
    "validate_dimensions",
]
