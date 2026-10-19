"""Structured operation events — §15's measurements, as queryable documents.

§15 settles an architecture question that is easy to get wrong by assuming:

    OTLP structured operation events flow through Collector → Data Prepper → OpenSearch;
    custom admin histogram/aggregation queries produce counts and percentiles. Native OTel
    metric ingestion into OpenSearch is not assumed unless support is verified in the selected
    version; if needed, the same measurements are sent as numeric log events. ... Authoritative
    analytics are deduplicated by ``event_id``; the same span/log is not counted twice.

The spike (ADR-0008) verified the conditional: OpenSearch 3.9.0 does **not** ingest native OTLP
metrics, and the only OTLP code in the distribution is an exporter for OpenSearch's own internal
telemetry. The documented fallback is therefore adopted, not invented — every measurement below
travels as a numeric log event.

**The shape of one event.** Each record is one measurement, never a bundle:

===========================================  ==================================================
``event.id``                                 The dedup key. See :func:`derive_event_id`.
``event.name``                               Which logical operation it describes.
``metric.name``                              What the number means.
``metric.value``                             The number.
``metric.unit``                              ``count`` | ``ms`` | ``usd`` | ``score`` | ``tokens``
``outcome.class``                            The §15 outcome, when there is one.
dimensions                                    Bounded attributes a chart may group by.
===========================================  ==================================================

One measurement per record is what lets every §15 question be answered by the same two
aggregations: ``cardinality(event.id)`` to count, ``percentiles(metric.value)`` to get a
distribution, ``terms`` on a dimension to split. An attempt therefore produces three records — the
attempt count, the outcome, and the latency — with three different ids, because they are three
measurements of the same thing and they aggregate differently.

**Deterministic ids, so dedup is structural rather than best-effort.** :func:`derive_event_id`
hashes the *logical identity* of the emission, so a re-export after an at-least-once delivery, a
worker restart that replays a command, or a genuine double-emit all produce the **same** id. The
document is still written twice (OpenSearch is not asked to deduplicate on ingest); the *count* is
what is deduplicated, because the query layer counts ``cardinality(event.id)`` rather than
``doc_count``. A random id would make that impossible, which is why ids are derived and never
generated.

**Three §15 rules that a `try/except` gets wrong by default**, each with a test:

* ``success_empty`` is a success. :class:`conventions.OutcomeClass` reports ``is_error`` for
  ``error`` only.
* A **timeout is still a latency sample**. :meth:`TelemetryEmitter.attempt` records the latency in
  a ``finally`` block, so a timed-out attempt contributes its (long) duration to the distribution
  instead of vanishing from it. Dropping timeouts is the single most effective way to make a p95
  look healthy while users wait.
* **``trace_id`` is never a dimension.** :func:`build_event` refuses any dimension outside
  :data:`conventions.DIMENSION_ATTRIBUTES`, and ``trace_id`` is not in it.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

from opentelemetry._logs import SeverityNumber

from referraleasy_telemetry.conventions import (
    ACTORS,
    ATTR_AGENT_ROLE,
    ATTR_AGENT_VERSION,
    ATTR_API_ENDPOINT,
    ATTR_API_FAILURE_KIND,
    ATTR_API_STATUS_CODE,
    ATTR_ATTEMPT_MAX,
    ATTR_ATTEMPT_NUMBER,
    ATTR_DEPLOYMENT_ANNOTATION,
    ATTR_ERROR_TYPE,
    ATTR_EVAL_JUDGE,
    ATTR_EVAL_METRIC,
    ATTR_EVAL_RUBRIC_VERSION,
    ATTR_EVAL_RUN_ID,
    ATTR_EVAL_STATUS,
    ATTR_EVENT_ACTION,
    ATTR_EVENT_ID,
    ATTR_EVENT_NAME,
    ATTR_FEEDBACK_RESPONSE_ID,
    ATTR_FEEDBACK_VALUE,
    ATTR_GUARDRAIL_DECISION,
    ATTR_GUARDRAIL_RULE_ID,
    ATTR_GUARDRAIL_STAGE,
    ATTR_HITL_TICKET_REF,
    ATTR_LATENCY_KIND,
    ATTR_METRIC_NAME,
    ATTR_METRIC_UNIT,
    ATTR_METRIC_VALUE,
    ATTR_MODEL_NAME,
    ATTR_MODEL_VERSION,
    ATTR_OUTCOME_CLASS,
    ATTR_RETRY_REASON,
    ATTR_SYNTHETIC,
    ATTR_WORKFLOW_STATE,
    DIMENSION_ATTRIBUTES,
    WORKFLOW_STATES,
    EventName,
    LatencyKind,
    MetricName,
    OutcomeClass,
    Synthetic,
)
from referraleasy_telemetry.identity import CorrelationIds, current_ids

#: Fixed namespace for derived event ids. A constant, and not a random one, because the id has to
#: be the same in two processes for the dedup to work at all.
EVENT_ID_NAMESPACE: Final[uuid.UUID] = uuid.UUID("6f1e6f2a-6b1f-5c3d-9a0e-2b7c4d8e1f30")

#: Separator for the canonical identity string. Chosen because none of the components can contain
#: it: ids are hex or UUIDs, event/metric names are dotted lowercase identifiers.
_ID_SEPARATOR: Final[str] = "\x1f"

#: Units, as a closed set, so ``metric.unit`` is a usable dimension.
UNIT_COUNT: Final[str] = "count"
UNIT_MS: Final[str] = "ms"
UNIT_USD: Final[str] = "usd"
UNIT_SCORE: Final[str] = "score"
UNIT_TOKENS: Final[str] = "tokens"

#: Metric -> its unit. A total mapping, so a new metric cannot be added without deciding its unit.
METRIC_UNITS: Final[dict[str, str]] = {
    MetricName.EVENT_COUNT: UNIT_COUNT,
    MetricName.LATENCY_ATTEMPT_MS: UNIT_MS,
    MetricName.LATENCY_INVOCATION_MS: UNIT_MS,
    MetricName.LATENCY_TURN_MS: UNIT_MS,
    MetricName.MODEL_TOKENS_INPUT: UNIT_TOKENS,
    MetricName.MODEL_TOKENS_OUTPUT: UNIT_TOKENS,
    MetricName.MODEL_COST_USD: UNIT_USD,
    MetricName.HITL_WAIT_MS: UNIT_MS,
    MetricName.HITL_RESOLVE_MS: UNIT_MS,
    MetricName.EVAL_SCORE: UNIT_SCORE,
}


class MissingEventIdentityError(ValueError):
    """An event id could not be derived because the emission had no identity at all."""


class UnknownDimensionError(ValueError):
    """A dimension was not one of :data:`conventions.DIMENSION_ATTRIBUTES`."""


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def derive_event_id(
    *,
    event_name: str,
    metric_name: str,
    trace_id: str | None = None,
    span_id: str | None = None,
    discriminator: object | None = None,
) -> str:
    """The deterministic dedup key for one measurement.

    Built from the logical identity of the emission — the trace, the span, which operation, which
    measurement, and an optional discriminator for the cases where one span produces several
    instances of the same measurement (attempt 1 and attempt 2, for instance).

    Deterministic rather than random, and that is the entire mechanism: the same logical emission
    always hashes to the same id, so counting *distinct ids* counts the event once no matter how
    many copies of the document were written. The copies are not prevented — OpenSearch is not
    asked to enforce uniqueness — they are made harmless.

    Raises:
        MissingEventIdentityError: when there is neither a span nor a discriminator. Two emissions
            from outside any span, of the same measurement, with no discriminator, are genuinely
            indistinguishable, and hashing them to the same id would silently under-count. Refusing
            is the behaviour that makes the caller supply the missing identity.
    """
    if not (span_id or trace_id) and discriminator is None:
        raise MissingEventIdentityError(
            f"cannot derive an event id for {event_name}/{metric_name}: the emission is outside "
            f"any span and no discriminator was given. Two such emissions would be "
            f"indistinguishable, so counting distinct event ids would quietly under-count. Pass "
            f"`discriminator=` (an attempt number, a state name, a command id) or emit inside a "
            f"span."
        )
    canonical = _ID_SEPARATOR.join(
        (
            str(trace_id or ""),
            str(span_id or ""),
            event_name,
            metric_name,
            "" if discriminator is None else str(discriminator),
        )
    )
    return str(uuid.uuid5(EVENT_ID_NAMESPACE, canonical))


# ---------------------------------------------------------------------------
# The event record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One measurement, ready to be written as an OTel log record.

    Constructed through :func:`build_event`, which validates; the dataclass itself is a value
    object and does no checking.
    """

    event_id: str
    event_name: str
    metric_name: str
    metric_value: float
    metric_unit: str
    attributes: Mapping[str, Any] = field(default_factory=dict)
    severity: SeverityNumber = SeverityNumber.INFO
    body: str = ""

    @property
    def outcome(self) -> OutcomeClass | None:
        """The outcome class this record carries, if any."""
        raw = self.attributes.get(ATTR_OUTCOME_CLASS)
        return OutcomeClass(raw) if raw else None


def build_event(
    *,
    event_name: EventName | str,
    metric_name: MetricName | str,
    metric_value: float | int,
    dimensions: Mapping[str, Any] | None = None,
    outcome: OutcomeClass | str | None = None,
    correlation: CorrelationIds | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    discriminator: object | None = None,
    event_id: str | None = None,
    severity: SeverityNumber | None = None,
    body: str | None = None,
    extra_fields: Mapping[str, Any] | None = None,
) -> EventRecord:
    """Validate and assemble one event.

    Every refusal here names the §15 rule it enforces, because each of these is a mistake that
    produces a plausible-looking chart rather than an error:

    * a ``dimensions`` entry outside :data:`conventions.DIMENSION_ATTRIBUTES` — including
      ``trace_id``, which §15 forbids as a label outright;
    * a dimension whose value is not in its closed vocabulary (``agent.role``, ``outcome.class``,
      ``workflow.state``, ``synthetic``);
    * a ``trace_id`` passed as a dimension rather than as ``trace_id=``.

    ``extra_fields`` is the escape hatch for *identifiers*: values that must be attached to a
    single record for correlation but must never be grouped by. Splitting the two parameters is
    what makes the trace-id rule checkable instead of aspirational.
    """
    name = str(event_name)
    metric = str(metric_name)
    dims: dict[str, Any] = dict(dimensions or {})

    if outcome is not None:
        cls = OutcomeClass(str(outcome))
        if ATTR_OUTCOME_CLASS in dims and dims[ATTR_OUTCOME_CLASS] not in (cls, cls.value):
            raise ValueError(
                f"outcome={cls.value!r} contradicts dimensions[{ATTR_OUTCOME_CLASS!r}]="
                f"{dims[ATTR_OUTCOME_CLASS]!r}; §15's outcome classes are a partition, so a record "
                f"in two classes at once would be counted twice"
            )
        dims[ATTR_OUTCOME_CLASS] = cls.value

    validate_dimensions(dims)

    attributes: dict[str, Any] = dict(dims)
    attributes[ATTR_EVENT_NAME] = name
    attributes[ATTR_METRIC_NAME] = metric
    attributes[ATTR_METRIC_VALUE] = float(metric_value)
    attributes[ATTR_METRIC_UNIT] = METRIC_UNITS.get(metric, UNIT_COUNT)

    if correlation is not None:
        attributes.update(correlation.attributes())
    if extra_fields:
        attributes.update(extra_fields)

    resolved_trace_id, resolved_span_id = trace_id, span_id
    if resolved_span_id is None and resolved_trace_id is None:
        resolved_trace_id, resolved_span_id = current_ids()
        resolved_trace_id = resolved_trace_id or None
        resolved_span_id = resolved_span_id or None
    if resolved_trace_id:
        attributes["trace_id"] = resolved_trace_id
    if resolved_span_id:
        attributes["span_id"] = resolved_span_id

    resolved_id = event_id or derive_event_id(
        event_name=name,
        metric_name=metric,
        trace_id=resolved_trace_id,
        span_id=resolved_span_id,
        discriminator=discriminator,
    )
    attributes[ATTR_EVENT_ID] = resolved_id

    return EventRecord(
        event_id=resolved_id,
        event_name=name,
        metric_name=metric,
        metric_value=float(metric_value),
        metric_unit=str(attributes[ATTR_METRIC_UNIT]),
        attributes=attributes,
        severity=severity or _severity_for(dims.get(ATTR_OUTCOME_CLASS)),
        body=body if body is not None else f"{name}: {metric}={metric_value}",
    )


def validate_dimensions(dimensions: Mapping[str, Any]) -> None:
    """Refuse a dimension that is not bounded, or that §15 forbids as a label.

    Exposed rather than private because the query layer applies the same check to a ``group_by``
    the admin app sends, and both call sites must agree.
    """
    for key, value in dimensions.items():
        if key == "trace_id":
            raise UnknownDimensionError(
                "'trace_id' is not a dimension (§15: trace IDs are not used as metric labels). A terms "
                "aggregation on it produces one bucket per request and answers nothing. Attach it "
                "with `trace_id=` — a field on the record — instead."
            )
        if key not in DIMENSION_ATTRIBUTES:
            raise UnknownDimensionError(
                f"{key!r} is not a dimension. Allowed dimensions are the bounded, closed-vocabulary "
                f"attributes listed in conventions.DIMENSION_ATTRIBUTES "
                f"({len(DIMENSION_ATTRIBUTES)} of them); per-request identifiers belong in "
                f"`extra_fields`."
            )
        _validate_dimension_value(key, value)


#: Closed vocabularies a dimension value must belong to, where one exists.
_VALUE_VOCABULARIES: Final[dict[str, tuple[object, ...]]] = {
    ATTR_AGENT_ROLE: ACTORS,
    ATTR_OUTCOME_CLASS: tuple(OutcomeClass),
    ATTR_WORKFLOW_STATE: WORKFLOW_STATES,
    ATTR_SYNTHETIC: tuple(Synthetic),
    ATTR_LATENCY_KIND: tuple(LatencyKind),
    ATTR_EVENT_NAME: tuple(EventName),
    ATTR_METRIC_NAME: tuple(MetricName),
}


def _validate_dimension_value(key: str, value: Any) -> None:
    vocabulary = _VALUE_VOCABULARIES.get(key)
    if vocabulary is None:
        return
    if value in vocabulary:
        return
    allowed = ", ".join(str(v) for v in vocabulary)
    raise UnknownDimensionError(
        f"{key}={value!r} is not a known value. A dimension with an unbounded or misspelled "
        f"vocabulary splits a chart into buckets nobody can compare, so the value is refused "
        f"rather than stored. Known values: {allowed}"
    )


def _severity_for(outcome: object) -> SeverityNumber:
    """Severity from the outcome class, so a log-based filter and an outcome filter agree.

    ``blocked`` and ``fallback`` are WARNING rather than ERROR: they are outcomes the platform
    handled, and marking them errors would make an error-severity filter return things that did not
    fail.
    """
    if outcome == OutcomeClass.ERROR.value:
        return SeverityNumber.ERROR
    if outcome in (OutcomeClass.BLOCKED.value, OutcomeClass.FALLBACK.value):
        return SeverityNumber.WARN
    return SeverityNumber.INFO


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------


class TelemetryEmitter:
    """Emits §15's measurements. Holds a logger, not a provider — wiring is `setup.py`'s job.

    Every method is a thin, named wrapper around :func:`build_event` so that the *shape* of each
    measurement is decided once, in this package, rather than at each call site. A service that
    calls ``emitter.api_failure(endpoint=..., status=503)`` cannot accidentally leave out the
    endpoint label and produce a chart with one meaningless bucket.
    """

    def __init__(
        self,
        logger: Any,
        *,
        deployment_annotation: str | None = None,
        synthetic: Synthetic | str | None = None,
    ) -> None:
        self._logger = logger
        self._annotation = deployment_annotation
        #: The deployment's label, applied to every record that does not name its own. It is a
        #: baseline dimension rather than a per-call argument because the alternative is what
        #: happened: a service that forgot to pass it wrote records that no synthetic-filtered chart
        #: could see, and nothing about the record looked wrong. `None` still means "no label",
        #: which is what a test's bare logger gets.
        self._synthetic = synthetic

    # -- baseline dimensions ---------------------------------------------------

    def _base(
        self,
        *,
        agent_role: str | None = None,
        agent_version: str | None = None,
        synthetic: Synthetic | str | None = None,
    ) -> dict[str, Any]:
        dims: dict[str, Any] = {}
        if agent_role is not None:
            dims[ATTR_AGENT_ROLE] = agent_role
        if agent_version is not None:
            dims[ATTR_AGENT_VERSION] = agent_version
        label = synthetic if synthetic is not None else self._synthetic
        if label is not None:
            dims[ATTR_SYNTHETIC] = str(label)
        if self._annotation is not None:
            dims[ATTR_DEPLOYMENT_ANNOTATION] = self._annotation
        return dims

    def _write(self, record: EventRecord) -> EventRecord:
        """Hand the record to the logger. Returned so a test can assert without a pipeline.

        ``timestamp`` is passed explicitly. The SDK sets ``observed_timestamp`` itself but leaves
        ``timestamp`` as ``None`` unless told (measured), and a log record without one serialises to
        an unparseable ``timeUnixNano`` that the collector answers with a 400 — taking the whole
        batch with it.
        """
        self._logger.emit(
            timestamp=time.time_ns(),
            severity_number=record.severity,
            body=record.body,
            attributes=dict(record.attributes),
        )
        return record

    # -- §15's measurements ----------------------------------------------------

    def agent_trigger(
        self,
        *,
        agent_role: str,
        agent_version: str | None = None,
        synthetic: Synthetic | str | None = None,
        correlation: CorrelationIds | None = None,
        discriminator: object | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> EventRecord:
        """One **logical** agent trigger — not one attempt.

        §15 asks for "agent logical trigger count", and the word *logical* is the whole
        specification: an invocation that succeeded on its third attempt was triggered once. Making
        this a separate emission from :meth:`attempt` is what keeps the trigger count and the
        attempt count from being two names for the same number, which is exactly the mistake that
        makes a retry storm look like a traffic spike.
        """
        return self._write(
            build_event(
                event_name=EventName.AGENT_TRIGGER,
                metric_name=MetricName.EVENT_COUNT,
                metric_value=1,
                dimensions={
                    **self._base(
                        agent_role=agent_role, agent_version=agent_version, synthetic=synthetic
                    ),
                },
                correlation=correlation,
                discriminator=discriminator,
                extra_fields=extra_fields,
            )
        )

    def attempt(
        self,
        *,
        agent_role: str | None = None,
        duration_ms: float,
        number: int,
        max_attempts: int | None = None,
        outcome: OutcomeClass | str | None = None,
        retry_reason: str | None = None,
        error_type: str | None = None,
        latency_kind: LatencyKind | str = LatencyKind.ATTEMPT,
        agent_version: str | None = None,
        synthetic: Synthetic | str | None = None,
        correlation: CorrelationIds | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> tuple[EventRecord, EventRecord, EventRecord]:
        """Record **one attempt**: its count, its latency, and its outcome. Returns all three.

        The three records are returned rather than hidden so a test can assert on them without a
        collector, and so a caller can add a field to one of them if it must.

        ``duration_ms`` is required and has no default. That is the design: §15 says timeouts are
        not removed from the latency distribution, and the way a timeout gets removed in practice
        is that the successful path measures and the failing path returns early. Here there is one
        call that takes the duration, and :meth:`attempt_span` calls it from a ``finally``.

        ``agent_role`` is optional because §15's three distributions are not all agent-shaped.
        ``latency_kind`` — not this parameter — is what names which one is being recorded, and a
        **turn** is the conversational unit: one command from the patient to its answer, which
        belongs to the platform rather than to any one role. A turn recorded with a role would put
        a fabricated role in every per-agent roll-up that grouped on it.
        """
        base = self._base(agent_role=agent_role, agent_version=agent_version, synthetic=synthetic)
        dims = {
            **base,
            ATTR_ATTEMPT_NUMBER: number,
            ATTR_LATENCY_KIND: str(latency_kind),
        }
        if max_attempts is not None:
            dims[ATTR_ATTEMPT_MAX] = max_attempts
        if retry_reason is not None:
            dims[ATTR_RETRY_REASON] = retry_reason
        if error_type is not None:
            dims[ATTR_ERROR_TYPE] = error_type

        outcome_value = OutcomeClass(str(outcome)) if outcome is not None else None

        counted = build_event(
            event_name=EventName.ATTEMPT,
            metric_name=MetricName.EVENT_COUNT,
            metric_value=1,
            dimensions=dims,
            outcome=outcome_value,
            correlation=correlation,
            discriminator=f"count:{number}",
            extra_fields=extra_fields,
        )
        latency = build_event(
            event_name=EventName.LATENCY,
            metric_name=_latency_metric(latency_kind),
            metric_value=duration_ms,
            dimensions={**dims},
            outcome=outcome_value,
            correlation=correlation,
            discriminator=f"latency:{number}",
            extra_fields=extra_fields,
        )
        recorded_outcome = build_event(
            event_name=EventName.OUTCOME,
            metric_name=MetricName.EVENT_COUNT,
            metric_value=1,
            dimensions={**base},
            outcome=outcome_value,
            correlation=correlation,
            discriminator=f"outcome:{number}",
            extra_fields=extra_fields,
        )
        for record in (counted, latency, recorded_outcome):
            self._write(record)
        return counted, latency, recorded_outcome

    @contextmanager
    def attempt_span(
        self,
        *,
        agent_role: str,
        number: int,
        max_attempts: int | None = None,
        latency_kind: LatencyKind | str = LatencyKind.ATTEMPT,
        agent_version: str | None = None,
        synthetic: Synthetic | str | None = None,
        correlation: CorrelationIds | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Time a block and emit its attempt records **whatever happens inside it**.

        Yields a mutable dict the caller fills in as it learns things, so a failure discovered
        inside the block still reaches the record written from the ``finally``::

            with emitter.attempt_span(agent_role="scheduling", number=n) as info:
                info["outcome"] = OutcomeClass.ERROR
                info["error_type"] = "timeout"
                raise TimeoutError

        A caller that lets an exception escape without setting ``outcome`` gets
        ``OutcomeClass.ERROR`` recorded — an unclassified failure is still a failure, and defaulting
        it to success would be the worse error.
        """
        info: dict[str, Any] = {}
        started = time.perf_counter()
        try:
            yield info
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0
            # `finally`, not `except`: the measurement happens on every path out of the block,
            # which is what keeps timeouts inside the latency distribution (§15).
            self.attempt(
                agent_role=agent_role,
                duration_ms=duration_ms,
                number=number,
                max_attempts=max_attempts,
                outcome=info.get("outcome", OutcomeClass.ERROR),
                retry_reason=info.get("retry_reason"),
                error_type=info.get("error_type"),
                latency_kind=latency_kind,
                agent_version=agent_version,
                synthetic=synthetic,
                correlation=correlation,
                extra_fields=extra_fields,
            )

    def api_failure(
        self,
        *,
        endpoint: str,
        status_code: int | None = None,
        failure_kind: str | None = None,
        agent_role: str | None = None,
        synthetic: Synthetic | str | None = None,
        correlation: CorrelationIds | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> EventRecord:
        """An API failure, by endpoint and status (§15: "API fail endpoint/status").

        ``endpoint`` must be a logical ref — ``mock-api:/appointments`` — never a URL with a query
        string. A URL is unbounded (it contains ids) and would put a per-request value in a keyword
        field used as a dimension. The check is a guard, not a convention: see
        :func:`_require_logical_endpoint`.
        """
        _require_logical_endpoint(endpoint)
        dims = {
            **self._base(agent_role=agent_role, synthetic=synthetic),
            ATTR_API_ENDPOINT: endpoint,
        }
        if status_code is not None:
            dims[ATTR_API_STATUS_CODE] = int(status_code)
        if failure_kind is not None:
            dims[ATTR_API_FAILURE_KIND] = failure_kind
        return self._write(
            build_event(
                event_name=EventName.API_FAILURE,
                metric_name=MetricName.EVENT_COUNT,
                metric_value=1,
                dimensions=dims,
                outcome=OutcomeClass.ERROR,
                severity=SeverityNumber.ERROR,
                correlation=correlation,
                discriminator=endpoint,
                extra_fields=extra_fields,
            )
        )

    def model_usage(
        self,
        *,
        agent_role: str,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        model_version: str | None = None,
        synthetic: Synthetic | str | None = None,
        correlation: CorrelationIds | None = None,
        discriminator: object | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> tuple[EventRecord, EventRecord, EventRecord]:
        """Token and cost accounting (§15: "model token/maliyet"). Three measurements."""
        dims = {
            **self._base(agent_role=agent_role, synthetic=synthetic),
            ATTR_MODEL_NAME: model_name,
        }
        if model_version is not None:
            dims[ATTR_MODEL_VERSION] = model_version

        def write(metric: MetricName, value: float) -> EventRecord:
            """One of the three measurements, with what a metric does not change shared."""
            return self._write(
                build_event(
                    event_name=EventName.MODEL_USAGE,
                    metric_name=metric,
                    metric_value=value,
                    dimensions={**dims},
                    correlation=correlation,
                    discriminator=discriminator,
                    extra_fields=extra_fields,
                )
            )

        # A tuple literal, not ``tuple(generator)``: the declared return type is a *fixed-length*
        # tuple of three, and ``tuple(...)`` is inferred as the variadic ``tuple[EventRecord, ...]``,
        # which is not assignable to it. The old form needed a ``type: ignore[return-value]`` to
        # hide that; three calls in the literal carry the arity in the type instead. Same three
        # events, same order, same ids — ``write`` is a closure, not a second code path.
        return (
            write(MetricName.MODEL_TOKENS_INPUT, input_tokens),
            write(MetricName.MODEL_TOKENS_OUTPUT, output_tokens),
            write(MetricName.MODEL_COST_USD, cost_usd),
        )

    def hitl_wait(
        self,
        *,
        wait_ms: float,
        ticket_ref: str | None = None,
        agent_role: str | None = None,
        workflow_state: str | None = None,
        correlation: CorrelationIds | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> EventRecord:
        """Time a workflow spent waiting for a human (§15: "HITL wait/resolution").

        Deliberately a separate measurement from latency: a human's response time is not the
        platform's, and mixing the two would put human thinking time into the p95 that the alert
        rules of §16 watch.
        """
        dims = {**self._base(agent_role=agent_role)}
        if workflow_state is not None:
            dims[ATTR_WORKFLOW_STATE] = workflow_state
        extra = dict(extra_fields or {})
        if ticket_ref is not None:
            extra[ATTR_HITL_TICKET_REF] = ticket_ref
        return self._write(
            build_event(
                event_name=EventName.HITL_WAIT,
                metric_name=MetricName.HITL_WAIT_MS,
                metric_value=wait_ms,
                dimensions=dims,
                correlation=correlation,
                extra_fields=extra,
            )
        )

    def hitl_resolve(
        self,
        *,
        resolve_ms: float,
        resolution: str | None = None,
        ticket_ref: str | None = None,
        agent_role: str | None = None,
        correlation: CorrelationIds | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> EventRecord:
        """Time from a human picking the ticket up to closing it."""
        dims = {**self._base(agent_role=agent_role)}
        extra = dict(extra_fields or {})
        if ticket_ref is not None:
            extra[ATTR_HITL_TICKET_REF] = ticket_ref
        if resolution is not None:
            # Not a dimension: the resolution vocabulary belongs to the handoff domain, and
            # duplicating it here would create a second source of truth for it.
            extra["hitl.resolution"] = resolution
        return self._write(
            build_event(
                event_name=EventName.HITL_RESOLVE,
                metric_name=MetricName.HITL_RESOLVE_MS,
                metric_value=resolve_ms,
                dimensions=dims,
                correlation=correlation,
                extra_fields=extra,
            )
        )

    def guardrail_decision(
        self,
        *,
        decision: str,
        stage: str | None = None,
        rule_id: str | None = None,
        agent_role: str | None = None,
        synthetic: Synthetic | str | None = None,
        correlation: CorrelationIds | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> EventRecord:
        """One guardrail decision (§15: "guardrail decisions").

        A ``block`` is recorded as ``OutcomeClass.BLOCKED``, which is neither a success nor an
        error. §15 lists ``blocked`` as its own class, and a guardrail doing its job is not a
        platform failure.
        """
        dims = {
            **self._base(agent_role=agent_role, synthetic=synthetic),
            ATTR_GUARDRAIL_DECISION: decision,
        }
        if stage is not None:
            dims[ATTR_GUARDRAIL_STAGE] = stage
        if rule_id is not None:
            dims[ATTR_GUARDRAIL_RULE_ID] = rule_id
        outcome = OutcomeClass.BLOCKED if decision == "block" else None
        return self._write(
            build_event(
                event_name=EventName.GUARDRAIL_DECISION,
                metric_name=MetricName.EVENT_COUNT,
                metric_value=1,
                dimensions=dims,
                outcome=outcome,
                correlation=correlation,
                discriminator=f"{stage or ''}:{decision}",
                extra_fields=extra_fields,
            )
        )

    def eval_score(
        self,
        *,
        run_id: str,
        metric: str,
        score: float | None,
        status: str,
        case_id: str | None = None,
        judge: str | None = None,
        rubric_version: str | None = None,
        agent_role: str | None = None,
        synthetic: Synthetic | str | None = None,
        correlation: CorrelationIds | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> EventRecord:
        """One evaluation score (§15: "evaluation scores").

        ``score`` may be ``None`` and ``status`` is required, because §14 is explicit that ``N/A``
        and ``eval_error`` are **not** zero. A ``None`` score is emitted with
        ``metric.value = 0`` and ``eval.status`` naming why, and the query layer excludes those
        statuses from score aggregations — so an unscoreable case cannot drag an average down while
        still being visible in the sample count.

        ``case_id`` is part of the *identity* of the measurement rather than a decoration on it, for
        the same reason ``judge`` and ``rubric_version`` are: one run scores many cases, and a metric
        is measured once per case. Two cases measured under the same run, metric, judge and rubric
        are two measurements and must hash to two ``event.id``s — without it ``cardinality(event.id)``
        counts the run's cases as one, and §14's sample count reads 1 across a bucket holding ten.
        The stored attribute is still ``eval.case_id`` (via ``extra_fields``); this parameter is what
        makes the id distinct.
        """
        if score is None and status == "scored":
            raise ValueError(
                "eval_score(status='scored') requires a score; use status='not_applicable' or "
                "'eval_error' with score=None so the record says why there is no number"
            )
        if score is not None and status != "scored":
            raise ValueError(
                f"eval_score(status={status!r}) must not carry a score ({score!r}); §14 keeps "
                f"N/A and eval_error out of the score distribution, and a number with a non-scored "
                f"status is how they get back in"
            )
        dims = {
            **self._base(agent_role=agent_role, synthetic=synthetic),
            ATTR_EVAL_METRIC: metric,
            ATTR_EVAL_STATUS: status,
        }
        if judge is not None:
            # §14: different judges and rubrics are never merged into one series.
            dims[ATTR_EVAL_JUDGE] = judge
        if rubric_version is not None:
            dims[ATTR_EVAL_RUBRIC_VERSION] = rubric_version
        # The judge, the rubric *and the case* are the identity of the measurement, not decoration on
        # it. Leaving the judge and rubric out made two judges scoring the same run and metric derive
        # the same ``event.id``, so ``cardinality(event.id)`` counted one of them and the other's
        # score disappeared from every aggregate — silently, with both documents present in the
        # index. Found by ``test_events.py::test_different_judges_are_never_merged``, which is exactly
        # the mistake §14 warns about. Leaving the case out was the same bug one level down: a run of
        # 44 cases across 12 metrics derived 12 ids, so every metric's sample count read 1 and §14's
        # "sample count, eligible count, N/A and error count" chart was wrong on its first column.
        # Found on the running stack, not by a test — the mocked response had no ids to collapse.
        return self._write(
            build_event(
                event_name=EventName.EVAL_SCORE,
                metric_name=MetricName.EVAL_SCORE,
                metric_value=score if score is not None else 0.0,
                dimensions=dims,
                correlation=correlation,
                extra_fields={ATTR_EVAL_RUN_ID: run_id, **(extra_fields or {})},
                discriminator=(
                    f"{run_id}:{metric}:{case_id or ''}:{judge or ''}:{rubric_version or ''}"
                ),
            )
        )

    def feedback(
        self,
        *,
        response_id: str,
        value: str,
        correlation: CorrelationIds | None = None,
        discriminator: object | None = None,
        synthetic: Synthetic | str | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> EventRecord:
        """One thumbs up/down (§15: "feedback").

        Emitted **raw**, one record per rating event, including a rating that supersedes an earlier
        one on the same response. The "last active thumbs per response_id" rule is a *query*
        property — see ``feedback.py`` — because enforcing it here would require this emitter to
        know which rating was last, and it cannot: it is a stateless emitter in a process that may
        not have seen the earlier one.
        """
        if value not in ("up", "down"):
            raise ValueError(f"feedback value must be 'up' or 'down', got {value!r}")
        return self._write(
            build_event(
                event_name=EventName.FEEDBACK,
                metric_name=MetricName.EVENT_COUNT,
                metric_value=1,
                dimensions={
                    **self._base(synthetic=synthetic),
                    ATTR_FEEDBACK_VALUE: value,
                },
                correlation=correlation,
                extra_fields={ATTR_FEEDBACK_RESPONSE_ID: response_id, **(extra_fields or {})},
                discriminator=discriminator,
            )
        )

    def state_transition(
        self,
        *,
        from_state: str,
        to_state: str,
        agent_role: str | None = None,
        synthetic: Synthetic | str | None = None,
        correlation: CorrelationIds | None = None,
        extra_fields: Mapping[str, Any] | None = None,
    ) -> EventRecord:
        """A workflow state change. Counted, and labelled with the state *entered*."""
        dims = {
            **self._base(agent_role=agent_role, synthetic=synthetic),
            ATTR_WORKFLOW_STATE: to_state,
        }
        return self._write(
            build_event(
                event_name=EventName.STATE_TRANSITION,
                metric_name=MetricName.EVENT_COUNT,
                metric_value=1,
                dimensions=dims,
                correlation=correlation,
                discriminator=f"{from_state}->{to_state}",
                extra_fields={
                    ATTR_EVENT_ACTION: f"transition:{from_state}->{to_state}",
                    **(extra_fields or {}),
                },
            )
        )


def _latency_metric(kind: LatencyKind | str) -> MetricName:
    return {
        LatencyKind.ATTEMPT: MetricName.LATENCY_ATTEMPT_MS,
        LatencyKind.INVOCATION: MetricName.LATENCY_INVOCATION_MS,
        LatencyKind.TURN: MetricName.LATENCY_TURN_MS,
    }[LatencyKind(str(kind))]


#: Endpoint refs must look like ``service:/path`` or ``/path``: no scheme, no host, no query.
_QUERY_MARKERS: Final[tuple[str, ...]] = ("?", "#", "&")


def _require_logical_endpoint(endpoint: str) -> None:
    if not endpoint.startswith("/") and ":" not in endpoint:
        raise ValueError(
            f"api.endpoint {endpoint!r} must be a logical ref: either a path ('/appointments') or "
            f"'service:/path' ('mock-api:/appointments')"
        )
    if "://" in endpoint:
        raise ValueError(
            f"api.endpoint {endpoint!r} is a URL. A URL carries a host and often an id in the "
            f"path or query, which makes it an unbounded keyword used as a dimension. Use the "
            f"logical ref instead: 'mock-api:/appointments/{{id}}'."
        )
    if any(marker in endpoint for marker in _QUERY_MARKERS):
        raise ValueError(
            f"api.endpoint {endpoint!r} contains a query string or fragment. Query strings carry "
            f"identifiers and would put a per-request value into a grouped field."
        )


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


class EventIdDedup:
    """A bounded set of the event ids this process has already handed to the exporter.

    Two dedup layers exist and they do different jobs:

    * **here** — drops a *duplicate* emission before it becomes a second document. Bounded and
      process-local, so it is an optimisation and a guard against a double-emit bug, not a
      correctness mechanism.
    * **the query layer** — counts ``cardinality(event.id)``, so even a document that was written
      twice is counted once. This is the layer that makes the number right.

    The bound is not decoration: an unbounded set would grow for the life of the worker, which is
    a slow leak in the one component that must never be the reason a booking fails. Eviction is
    oldest-first, so the window is "the most recent N emissions" — and evicting an id can only
    cause an under-dedup, never a dropped unique event.
    """

    def __init__(self, capacity: int = 8192) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.duplicates = 0

    def seen(self, event_id: str) -> bool:
        """Return True if ``event_id`` was already recorded, else record it and return False."""
        if event_id in self._seen:
            self._seen.move_to_end(event_id)
            self.duplicates += 1
            return True
        self._seen[event_id] = None
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return False

    def __len__(self) -> int:
        return len(self._seen)
