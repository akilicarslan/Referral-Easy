# ruff: noqa: RUF003  # Turkish quoted from §15 of ReferralEasy_Architecture_v3.md is kept verbatim
"""The telemetry vocabulary, taken verbatim from §15 of `ReferralEasy_Architecture_v3.md`.

Everything in this module is a *name*. It exists so that a service emitting telemetry and an admin
query filtering it cannot disagree about what a field is called, because they import the same
constant instead of both typing a string.

Three rules are encoded here rather than left to discipline, because §15 states each of them and
each is easy to get subtly wrong:

1. **`empty` is a valid result and is not an error.** `OutcomeClass.success_empty` is grouped with
   the successes, and ``OutcomeClass.error`` is the *only* class that reports ``is_error``. A
   `try/except` that folds an empty result into the failure branch is the bug this prevents.
2. **Timeouts are not removed from the latency distribution.** A latency measurement is emitted
   with the same ``LatencyKind`` whether the attempt succeeded or timed out; only ``outcome.class``
   differs. There is deliberately no helper that computes a latency from a success-only path.
3. **`trace_id` is never a metric label.** ``DIMENSION_ATTRIBUTES`` is the closed set of attributes
   a query may group by, and neither ``trace_id`` nor any per-request identifier is in it.
   Correlation identifiers travel as *fields* (``identity.py``), which is a different thing from a
   dimension: a field is a pointer to one trace, a dimension is a bucket that many traces share.

Field naming follows what Data Prepper actually writes into OpenSearch, which was read off a live
document rather than assumed (see `docs/adr/ADR-0032-telemetry-opensearch-analytics.md`): an OTLP
attribute named ``patient.email`` arrives as the flat field ``span.attributes.patient@email``.
``field_name`` performs that translation so no query module has to remember it.
"""

from __future__ import annotations

from enum import StrEnum

# ---------------------------------------------------------------------------
# Section reference
# ---------------------------------------------------------------------------

#: The § this module implements. Quoted in emitted resource attributes so a document in OpenSearch
#: can be traced back to the requirement that produced it.
SECTION = "§15"

#: Instrumentation scope name; appears as `instrumentationScope.name` on every span and event.
SCOPE_NAME = "referraleasy.telemetry"

#: Resource attribute identifying which §15 pipeline produced a document.
RESOURCE_SPEC_SECTION = "referraleasy.spec.section"


# ---------------------------------------------------------------------------
# §15 vocabulary — the closed sets
# ---------------------------------------------------------------------------

#: The 16 workflow states.
#:
#: The authoritative definition is the ``workflow_state`` table created by
#: ``migrations/014_align_domain_vocabulary.sql``; the domain layer reads it from PostgreSQL and
#: never mirrors it. This tuple is a *telemetry-side* mirror used for one purpose only: refusing to
#: emit or filter on a state name that does not exist, so a typo becomes a loud failure at emit time
#: instead of an empty chart nobody notices. It is not consulted for any workflow decision.
WORKFLOW_STATES: tuple[str, ...] = (
    "INTAKE",
    "COVERAGE_CHECK",
    "PROVIDER_MATCHING",
    "AWAITING_HOSPITAL_CHOICE",
    "AWAITING_DOCTOR_CHOICE",
    "READINESS_CHECK",
    "AWAITING_SLOT_CHOICE",
    "AWAITING_FINAL_APPROVAL",
    "BOOKING_IN_FLIGHT",
    "BOOKED",
    "BOOKING_UNVERIFIED",
    "RECOVERY",
    "HUMAN_HANDOFF",
    "ABANDONED",
    "CANCELLED",
    "FAILED",
)

#: The 7 agent roles that own a state or a tool (§4).
AGENT_ROLES: tuple[str, ...] = (
    "care_coordinator",
    "referral_intake",
    "coverage_network",
    "provider_matching",
    "referral_readiness",
    "scheduling",
    "recovery_handoff",
)

#: The 2 non-agent principals. Kept separate from ``AGENT_ROLES`` because §15's agent filters and
#: §12's "Agents & Versions" page are about agents, and an `operator` counted as an agent would
#: silently inflate every per-agent chart.
PRINCIPALS: tuple[str, ...] = ("operator", "system")

#: Every actor: the 7 agent roles plus the 2 principals.
ACTORS: tuple[str, ...] = AGENT_ROLES + PRINCIPALS


class OutcomeClass(StrEnum):
    """The six outcome classes of §15.

    ``success_empty`` is a *success*. §15 says so in as many words ("Empty geçerli sonuç error
    değildir"), and the reason it has to be said is that the natural shape of error handling —
    raise on anything unusual, catch, record a failure — turns "the payer network has no
    in-network specialist for this ZIP" into a false alarm. A genuinely empty result is the
    correct answer to a question that had a correct answer.

    ``blocked`` and ``fallback`` are deliberately neither successes nor errors. A guardrail
    refusal and a degraded-but-functional fallback are their own outcomes; folding either into
    "error" would make the failure rate disagree with the number of things that actually failed,
    and folding either into "success" would hide that the platform did not do what was asked.
    Neither is asserted here — the classes are reported separately, and any roll-up is a decision
    made by whoever writes the alert rule (§16), not by this module.
    """

    SUCCESS_FULL = "success_full"
    SUCCESS_EMPTY = "success_empty"
    SUCCESS_PARTIAL = "success_partial"
    ERROR = "error"
    BLOCKED = "blocked"
    FALLBACK = "fallback"

    @property
    def is_success(self) -> bool:
        """True for the three classes that mean the platform answered the question."""
        return self in _SUCCESS_CLASSES

    @property
    def is_error(self) -> bool:
        """True only for ``error`` — the single class that means something went wrong.

        ``success_empty`` is excluded on purpose and that exclusion is the point of §15.
        ``blocked`` and ``fallback`` are also excluded: they are outcomes to be counted in their
        own right, and calling them errors here would make ``is_error`` disagree with the class a
        reader sees on the chart.
        """
        return self is OutcomeClass.ERROR


_SUCCESS_CLASSES = frozenset(
    {OutcomeClass.SUCCESS_FULL, OutcomeClass.SUCCESS_EMPTY, OutcomeClass.SUCCESS_PARTIAL}
)


class LatencyKind(StrEnum):
    """The three latency distributions §15 asks p50/p95/p99 for.

    ``ATTEMPT`` is one try, ``INVOCATION`` is one agent invocation (all its attempts plus its
    tool work), ``TURN`` is one conversational turn. They are separate distributions rather than
    three views of one, because an attempt p95 that is fine while the turn p95 is not is exactly
    the signal that points at retries rather than at a slow dependency.
    """

    ATTEMPT = "attempt"
    INVOCATION = "invocation"
    TURN = "turn"


class EventName(StrEnum):
    """The logical operation an event record describes (§15's measurement list).

    One ``EventName`` may carry several measurements: an attempt produces an ``ATTEMPT`` count, an
    ``OUTCOME`` class and a ``latency.attempt.ms`` value. Each of those is a separate record with
    its own ``event_id``, because they aggregate differently — a count is a count, a percentile
    needs the numbers, and a class breakdown needs the label.
    """

    AGENT_TRIGGER = "agent_trigger"
    ATTEMPT = "attempt"
    RETRY = "retry"
    OUTCOME = "outcome"
    API_FAILURE = "api_failure"
    LATENCY = "latency"
    MODEL_USAGE = "model_usage"
    HITL_WAIT = "hitl_wait"
    HITL_RESOLVE = "hitl_resolve"
    GUARDRAIL_DECISION = "guardrail_decision"
    EVAL_SCORE = "eval_score"
    FEEDBACK = "feedback"
    STATE_TRANSITION = "state_transition"


class MetricName(StrEnum):
    """The numeric payload of an event record.

    Every event carries exactly one of these plus a numeric ``metric.value``, which is what makes
    the whole of §15 queryable with the same two aggregations: ``cardinality(event.id)`` to count
    and ``percentiles(metric.value)`` to get a distribution. A pure count uses
    ``EVENT_COUNT`` with value 1 rather than carrying no number at all, so no query has to special
    case "events that are not measurements".
    """

    EVENT_COUNT = "event.count"
    LATENCY_ATTEMPT_MS = "latency.attempt.ms"
    LATENCY_INVOCATION_MS = "latency.invocation.ms"
    LATENCY_TURN_MS = "latency.turn.ms"
    MODEL_TOKENS_INPUT = "model.tokens.input"
    MODEL_TOKENS_OUTPUT = "model.tokens.output"
    MODEL_COST_USD = "model.cost.usd"
    HITL_WAIT_MS = "hitl.wait.ms"
    HITL_RESOLVE_MS = "hitl.resolve.ms"
    EVAL_SCORE = "eval.score"


class Synthetic(StrEnum):
    """Whether a record came from fixture traffic or from a real (demo) run.

    §12 requires a synthetic-vs-live filter and §14 requires synthetic traffic to be labelled. The
    label is an explicit enum so "unlabelled" is not a third silent state: a record either says
    ``synthetic``, says ``live_demo``, or does not exist.
    """

    SYNTHETIC = "synthetic"
    LIVE_DEMO = "live_demo"


# ---------------------------------------------------------------------------
# Attribute keys
# ---------------------------------------------------------------------------

#: ``agent.role`` — one of ``AGENT_ROLES`` or ``PRINCIPALS``.
ATTR_AGENT_ROLE = "agent.role"
#: ``agent.version`` — the agent's own version, from its Agent Card.
ATTR_AGENT_VERSION = "agent.version"
#: ``agent.release`` — the release manifest id the running code came from (§14).
ATTR_AGENT_RELEASE = "agent.release"
#: ``model.name`` — the pinned model id used for a call (§8).
ATTR_MODEL_NAME = "model.name"
#: ``model.version`` — the model version/revision, for the release/model filter of §12.
ATTR_MODEL_VERSION = "model.version"

#: ``outcome.class`` — one of ``OutcomeClass``. Keyword, so it can be grouped and filtered.
ATTR_OUTCOME_CLASS = "outcome.class"
#: ``workflow.state`` — one of ``WORKFLOW_STATES``.
ATTR_WORKFLOW_STATE = "workflow.state"

#: ``event.id`` — the dedup key. See `events.derive_event_id`.
ATTR_EVENT_ID = "event.id"
#: ``event.name`` — one of ``EventName``.
ATTR_EVENT_NAME = "event.name"
#: ``event.action`` — the tool/action ref (``owner:action``, §4) being attempted or recorded.
ATTR_EVENT_ACTION = "event.action"

#: ``metric.name`` — one of ``MetricName``.
ATTR_METRIC_NAME = "metric.name"
#: ``metric.value`` — the number. Mapped as a double in OpenSearch.
ATTR_METRIC_VALUE = "metric.value"
#: ``metric.unit`` — ``count`` | ``ms`` | ``usd`` | ``score`` | ``tokens``.
ATTR_METRIC_UNIT = "metric.unit"

#: ``latency.kind`` — one of ``LatencyKind``; present only on latency measurements.
ATTR_LATENCY_KIND = "latency.kind"

#: ``attempt.number`` — 1-based attempt index within an invocation.
ATTR_ATTEMPT_NUMBER = "attempt.number"
#: ``attempt.max`` — the configured ceiling (§19 ``AGENT_MAX_ATTEMPTS``).
ATTR_ATTEMPT_MAX = "attempt.max"
#: ``retry.reason`` — why a retry happened: ``timeout`` | ``http_5xx`` | ``rate_limited`` | ...
ATTR_RETRY_REASON = "retry.reason"

#: ``error.type`` — a coarse, bounded classification (``timeout``, ``schema``, ``http_503``).
#: Never a free-text message: a message would be unbounded and can carry PII.
ATTR_ERROR_TYPE = "error.type"

#: ``api.endpoint`` — the logical endpoint, not the URL: ``mock-api:/appointments`` and
#: ``/appointments/{id}`` are different labels, and query strings are never included because they
#: can carry identifiers.
ATTR_API_ENDPOINT = "api.endpoint"
#: ``api.status_code`` — the HTTP status.
ATTR_API_STATUS_CODE = "api.status_code"
#: ``api.failure_kind`` — ``transport`` | ``http_error`` | ``schema_mismatch`` | ``timeout``.
ATTR_API_FAILURE_KIND = "api.failure_kind"

#: ``hitl.ticket_ref`` — the operator queue ticket (§11).
ATTR_HITL_TICKET_REF = "hitl.ticket_ref"

#: ``guardrail.stage`` — which stage of the three-stage pipeline decided (§7/GUARDRAIL_MODE).
ATTR_GUARDRAIL_STAGE = "guardrail.stage"
#: ``guardrail.decision`` — ``allow`` | ``block`` | ``redact`` | ``flag``.
ATTR_GUARDRAIL_DECISION = "guardrail.decision"
#: ``guardrail.rule_id`` — the rule that fired, when one did.
ATTR_GUARDRAIL_RULE_ID = "guardrail.rule_id"

#: ``eval.run_id`` — the idempotency key of one evaluation run (§14).
ATTR_EVAL_RUN_ID = "eval.run_id"
#: ``eval.metric`` — correctness | groundedness | completeness | ...
ATTR_EVAL_METRIC = "eval.metric"
#: ``eval.rubric_version`` / ``eval.judge`` — §14 requires results to name both.
ATTR_EVAL_RUBRIC_VERSION = "eval.rubric_version"
ATTR_EVAL_JUDGE = "eval.judge"
#: ``eval.status`` — ``scored`` | ``not_applicable`` | ``eval_error``. §14: N/A and eval_error are
#: not scored as zero, so they must be distinguishable from a real 0.0 in the stored document.
ATTR_EVAL_STATUS = "eval.status"

#: ``feedback.response_id`` — the id of the response being rated. The dedup key for feedback.
ATTR_FEEDBACK_RESPONSE_ID = "feedback.response_id"
#: ``feedback.value`` — ``up`` | ``down``.
ATTR_FEEDBACK_VALUE = "feedback.value"

#: ``synthetic`` — ``Synthetic``. Kept as a short name because it also exists as a top-level
#: mapped field in both index templates; the query layer reads the mapped field.
ATTR_SYNTHETIC = "synthetic"
#: ``deployment.annotation`` — a demo/release marker shown on charts (§12).
ATTR_DEPLOYMENT_ANNOTATION = "deployment.annotation"

#: ``turn.id`` — identifies one conversational turn, so turn-scoped measurements can be joined.
ATTR_TURN_ID = "turn.id"


#: The attributes a query is allowed to group by.
#:
#: Every entry is bounded by construction: a closed enum, a version string from a manifest, or an
#: endpoint ref. ``trace_id``, ``span_id``, ``event.id``, ``turn.id`` and the four workflow
#: correlation ids are absent, and their absence is the §15 rule "trace ID metric label yapılmaz"
#: expressed as data rather than as a comment. A terms aggregation on a per-trace identifier is
#: how a demo OpenSearch node runs out of heap, and it answers nothing: every bucket has one
#: document.
DIMENSION_ATTRIBUTES: tuple[str, ...] = (
    ATTR_AGENT_ROLE,
    ATTR_AGENT_VERSION,
    ATTR_AGENT_RELEASE,
    ATTR_MODEL_NAME,
    ATTR_MODEL_VERSION,
    ATTR_OUTCOME_CLASS,
    ATTR_WORKFLOW_STATE,
    ATTR_EVENT_NAME,
    ATTR_EVENT_ACTION,
    ATTR_METRIC_NAME,
    ATTR_METRIC_UNIT,
    ATTR_LATENCY_KIND,
    # Bounded by configuration rather than by an enum: a demo runs with AGENT_MAX_ATTEMPTS=3, so
    # the number of distinct buckets is at most that. These are the dimensions behind §15's
    # "attempt/retry count" — an attempt count split by attempt number is what shows whether a
    # retry policy is converging, and a count without it only shows that retries happened.
    ATTR_ATTEMPT_NUMBER,
    ATTR_ATTEMPT_MAX,
    ATTR_RETRY_REASON,
    ATTR_ERROR_TYPE,
    ATTR_API_ENDPOINT,
    # HTTP status codes: a closed set in practice, and the whole point of §15's "API fail
    # endpoint/status" is to group by it.
    ATTR_API_STATUS_CODE,
    ATTR_API_FAILURE_KIND,
    ATTR_GUARDRAIL_STAGE,
    ATTR_GUARDRAIL_DECISION,
    ATTR_GUARDRAIL_RULE_ID,
    ATTR_EVAL_METRIC,
    ATTR_EVAL_RUBRIC_VERSION,
    ATTR_EVAL_JUDGE,
    ATTR_EVAL_STATUS,
    ATTR_FEEDBACK_VALUE,
    ATTR_DEPLOYMENT_ANNOTATION,
    ATTR_SYNTHETIC,
)

#: Attributes that identify a single request or record. Emitted as fields, never used as labels.
#: Listed here so `events.build_event` can refuse them with a message that names the rule rather
#: than silently dropping them.
CORRELATION_ATTRIBUTES: tuple[str, ...] = (
    "trace_id",
    "span_id",
    ATTR_EVENT_ID,
    ATTR_TURN_ID,
    ATTR_HITL_TICKET_REF,
    ATTR_EVAL_RUN_ID,
    ATTR_FEEDBACK_RESPONSE_ID,
)


# ---------------------------------------------------------------------------
# OpenSearch field naming
# ---------------------------------------------------------------------------

#: What Data Prepper substitutes for a ``.`` inside an OTLP attribute key when it flattens the
#: attribute map into OpenSearch fields. Read off a live document: the OTLP attribute
#: ``http.request.header.authorization`` is stored as
#: ``span.attributes.http@request@header@authorization``. Every dot is replaced, not just the
#: first one, which is why the substitution is a ``str.replace`` and not a ``split``.
FLATTENED_DOT = "@"

#: The three attribute families Data Prepper writes. ``span`` and ``resource`` appear on trace
#: documents, ``log`` and ``resource`` on event documents.
ATTRIBUTE_FAMILIES: tuple[str, ...] = ("span", "log", "resource")


def field_name(attribute: str, *, family: str = "log") -> str:
    """Return the OpenSearch field that holds ``attribute`` for the given family.

    ``field_name("metric.name")`` -> ``"log.attributes.metric@name"``.

    This is the single place the flattening is encoded. A query module that spelled the field out
    by hand would keep working until someone renamed an attribute, and would then silently return
    zero hits — the failure mode this whole module exists to remove.

    Raises:
        ValueError: if ``family`` is not one of ``ATTRIBUTE_FAMILIES``. A wrong family produces a
            query that matches nothing, so it is refused rather than sent.
    """
    if family not in ATTRIBUTE_FAMILIES:
        raise ValueError(
            f"unknown attribute family {family!r}; expected one of {ATTRIBUTE_FAMILIES}"
        )
    return f"{family}.attributes.{attribute.replace('.', FLATTENED_DOT)}"


# ---------------------------------------------------------------------------
# Index families and retention
# ---------------------------------------------------------------------------

#: Trace indices, written by the Data Prepper trace pipeline. 7 day retention.
TRACE_INDEX_PATTERN = "otel-traces-*"
#: Event indices, written by the Data Prepper event pipeline. 7 day retention.
EVENT_INDEX_PATTERN = "otel-events-*"
#: Evaluation indices. 30 day retention — §15 keeps evaluation longer than telemetry.
EVALUATION_INDEX_PATTERN = "referraleasy-evaluation-*"
#: The directory/vector index family. §15: "Rehber ve telemetri indeksleri ayrılır". This family is
#: named here only so the query layer can *refuse* it; telemetry queries never touch it, because
#: retrieved directory text is untrusted input and must not be able to masquerade as a structured
#: telemetry record.
DIRECTORY_INDEX_PATTERN = "referraleasy-directory-*"

#: Retention, in days, exactly as §15 states it.
TELEMETRY_RETENTION_DAYS = 7
EVALUATION_RETENTION_DAYS = 30

#: The index families a telemetry query may read. A caller passing anything else — the directory
#: index in particular — is refused by `query.TelemetryQuery`.
ALLOWED_QUERY_PATTERNS: tuple[str, ...] = (
    TRACE_INDEX_PATTERN,
    EVENT_INDEX_PATTERN,
    EVALUATION_INDEX_PATTERN,
)
