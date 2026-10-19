"""End-to-end identity: W3C trace context, the correlation id space, and span links (§15).

§15 draws a line that the rest of the platform can only be correct if it honours:

    The shared end-to-end identity is the W3C ``trace_id``; ``span_id`` identifies a sub-step.
    A2A ``task_id``/``context_id``/``message_id`` are added to the trace as attributes;
    they are not equated. ``conversation_id``, ``workflow_id``, ``command_id``, and
    ``booking_request_id`` are kept for separate purposes.

Two claims are load-bearing and are enforced here rather than documented:

**The four workflow ids and the three A2A ids are never the same value.** They look
interchangeable — all seven are strings, all seven are "the id of this thing" — and the tempting
shortcut is to pass one wherever another is wanted, at which point every chart that groups by
"workflow" is really grouping by "conversation" and the two can no longer be told apart. So
:class:`CorrelationIds` refuses to be constructed with a duplicate. An id that is genuinely not
known yet is ``None``: absent is not the same as equal, and the check skips absent ids for exactly
that reason.

**After a long asynchronous wait, the work resumes in a NEW trace that LINKS to the old one.**
A workflow can sit in ``AWAITING_HOSPITAL_CHOICE`` for a day. Modelling that as one trace would
produce a trace whose duration is the patient's lunch break, which breaks every latency
percentile in §15 — the p95 would be dominated by human thinking time rather than by anything the
platform did. :func:`resume_after_wait` therefore starts a fresh root span and attaches a *link*
to the span that parked the work, so the waterfall shows both halves and the latency distribution
shows only work.

Propagation is W3C `traceparent`/`tracestate`, injected and extracted by the OTel propagator, and
it travels over two transports:

* plain HTTP, as headers;
* A2A, whose messages are protobuf ``Struct`` metadata — see :func:`a2a_carrier_from_metadata`,
  which is the bridge that lets the same injector serve both.

The transport behaviour is verified against real HTTP and a real A2A message, not a mocked
carrier: see ``tests/telemetry/test_propagation_integration.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.trace import Link, Span, SpanContext, use_span
from opentelemetry.trace.span import NonRecordingSpan, format_span_id, format_trace_id

#: The W3C header carrying the trace context. Spelled out because the A2A carrier has to agree
#: with it: the same key travels as an HTTP header and as a metadata field, and if those two ever
#: drifted, propagation over A2A would fail silently with a *new* root trace rather than an error.
TRACEPARENT: Final[str] = "traceparent"
TRACESTATE: Final[str] = "tracestate"

#: Keys the A2A message metadata may carry the context under. ``traceparent`` is the canonical one;
#: none of the others are used by this package, but they are *reserved* so a caller cannot smuggle
#: a different context past the extractor by using a second name.
A2A_METADATA_KEYS: Final[tuple[str, ...]] = (TRACEPARENT, TRACESTATE)


class IdentityConflictError(ValueError):
    """Two ids that §15 requires to stay distinct were given the same value."""


@dataclass(frozen=True, slots=True)
class CorrelationIds:
    """The seven correlatable identifiers, kept apart on purpose (§15).

    ``conversation_id``
        The patient-facing conversation. Survives across turns; a conversation may carry several
        referrals.
    ``workflow_id``
        One referral's journey through the 16 states. One conversation can start more than one.
    ``command_id``
        The dedup key of a single command, so a replayed request is recognised as the same command
        (``packages/domain`` owns the ledger; this only carries the value onto the trace).
    ``booking_request_id``
        One booking attempt's idempotency key. A retried booking is the *same* request id, which is
        precisely why it cannot double as a ``command_id``.
    ``task_id`` / ``context_id`` / ``message_id``
        A2A's. ``task_id`` is the A2A task, ``context_id`` groups related A2A tasks, ``message_id``
        identifies one message. They bracket a single A2A exchange and are not the workflow's ids
        even when, in a one-shot demo, they happen to look like a 1:1 mapping.

    Every field is optional: a service that has not yet seen a value records ``None`` rather than
    inventing one, and *absent is not equal* — the distinctness check compares only the values that
    are actually present.
    """

    conversation_id: str | None = None
    workflow_id: str | None = None
    command_id: str | None = None
    booking_request_id: str | None = None
    task_id: str | None = None
    context_id: str | None = None
    message_id: str | None = None

    def __post_init__(self) -> None:
        seen: dict[str, str] = {}
        for name, value in self.as_dict().items():
            if value is None:
                continue
            if not value:
                raise IdentityConflictError(
                    f"{name} is present but empty; use None for 'not known yet', so that an "
                    f"empty string is never mistaken for an identity ( §15)"
                )
            if value in seen:
                raise IdentityConflictError(
                    f"{name} and {seen[value]} both carry {value!r}. §15 keeps these ids for "
                    f"separate purposes and states they are never equated; "
                    f"sharing a value makes every per-workflow chart silently a per-conversation "
                    f"chart. Pass a distinct value, or None if it is not known yet."
                )
            seen[value] = name

    def as_dict(self) -> dict[str, str | None]:
        """The ids as a plain mapping, in declaration order."""
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def attributes(self) -> dict[str, str]:
        """The ids as span/event attributes, omitting the ones that are not known.

        The four workflow ids keep their bare names. The three A2A ids are namespaced ``a2a.``
        because §15 calls them out as belonging to the A2A transport: a reader seeing
        ``a2a.task_id`` knows which protocol produced it, and a filter on ``workflow_id`` cannot
        accidentally match an A2A task.
        """
        out: dict[str, str] = {}
        for name, value in self.as_dict().items():
            if value is None:
                continue
            key = f"a2a.{name}" if name in _A2A_ID_FIELDS else name
            out[key] = value
        return out


#: Which of the seven ids belong to the A2A transport, and so get the ``a2a.`` prefix.
_A2A_ID_FIELDS: Final[frozenset[str]] = frozenset({"task_id", "context_id", "message_id"})


# ---------------------------------------------------------------------------
# Cross-process propagation
# ---------------------------------------------------------------------------


def inject_context(carrier: MutableMapping[str, Any] | None = None) -> MutableMapping[str, Any]:
    """Serialise the active context into ``carrier`` (a ``traceparent``-bearing mapping).

    Thin wrapper over the globally configured propagator so that every caller in this repo uses
    one entry point, and so ``tests`` can assert what a service would actually put on the wire.

    The return type is ``MutableMapping``, not ``dict``, because the mapping that comes back *is*
    the one that went in: pass an ``httpx.Headers`` and that object is returned with the context
    written into it. The signature used to accept any mutable mapping and promise a ``dict`` back,
    which is a promise the function cannot keep for a non-``dict`` input.
    """
    carrier = {} if carrier is None else carrier
    propagate.inject(carrier)
    return carrier


def extract_context(carrier: Mapping[str, Any]) -> Context:
    """Rebuild a :class:`Context` from a ``traceparent``-bearing mapping.

    Returns the active context unchanged when the carrier holds nothing usable — which is what the
    W3C propagator does, and is the right behaviour for a service that is also reachable by a
    client which does not propagate: the trace starts here instead of the request failing.
    """
    return propagate.extract(dict(carrier))


def extract_span_context(carrier: Mapping[str, Any]) -> SpanContext | None:
    """The remote :class:`SpanContext` in ``carrier``, or ``None`` if there is no valid one.

    Used by callers that want to *link* to a remote trace rather than continue it.

    **The validity check is the function.** This read ``...get_span_context() or None``, and that
    fallback was dead both ways round: ``get_span_context()`` is declared to return a
    ``SpanContext`` and never ``None``, and the value it does return for a carrier with no context
    — a non-recording span's all-zero ``SpanContext`` — is not falsy either, so ``or None`` could
    not fire and the docstring's "or ``None``" was not true of any input. A caller writing
    ``if ctx is None`` would have taken the non-``None`` branch and linked to the invalid span
    ``00000000000000000000000000000000``. It now tests ``is_valid``, the same predicate
    :func:`current_ids` uses. Pinned by
    :class:`tests.telemetry.test_identity.TestExtractSpanContext`.
    """
    span_context = trace.get_current_span(extract_context(carrier)).get_span_context()
    return span_context if span_context.is_valid else None


def a2a_carrier_from_metadata(metadata: Mapping[str, Any] | None) -> dict[str, str]:
    """Pull the W3C context out of an A2A message's metadata into an extractable carrier.

    A2A carries application data in a protobuf ``Struct`` (verified against the installed ``a2a``
    package: ``Message.metadata`` is a ``google.protobuf.Struct``). The injector's flat
    ``{"traceparent": ...}`` lands as ``metadata["traceparent"]`` and comes back out unchanged.

    Only :data:`A2A_METADATA_KEYS` are read. Values that are not strings are dropped rather than
    coerced: ``metadata`` is attacker-adjacent input in the general case, and a non-string
    ``traceparent`` is a malformed header, not something to be made to fit.

    **Read by membership and indexing, never by ``.get()``.** ``Struct`` is registered as a
    ``collections.abc.Mapping`` — so ``isinstance(metadata, Mapping)`` is ``True`` and the type
    annotation looks satisfied — but that registration is virtual, so the ABC's mixin methods are
    *not* provided and ``metadata.get(key)`` raises ``AttributeError``. The receiver path therefore
    crashed on a real message while every test passed, because every test handed this function a
    plain ``dict``: the tests were checking a shape the A2A runtime does not produce. Measured on
    protobuf 7.36.2; ``metadata[key]`` on a *missing* key raises ``ValueError`` rather than
    ``KeyError``, which is the other half of the same surprise.
    """
    if not metadata:
        return {}
    carrier: dict[str, str] = {}
    for key in A2A_METADATA_KEYS:
        if key not in metadata:
            continue
        try:
            value = metadata[key]
        except ValueError:  # pragma: no cover - protobuf's missing-key error, see above
            continue
        if isinstance(value, str) and value:
            carrier[key] = value
    return carrier


def a2a_metadata_with_context(existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Metadata to attach to an outgoing A2A message so the callee joins this trace.

    Existing keys are preserved: ``metadata`` belongs to the A2A application, and the trace context
    is a passenger in it, not its owner. **All** of them are preserved, including the non-string
    ones — an earlier version returned only the string-valued keys, which silently deleted the
    application's own nested metadata on the way out. Nothing about injecting a traceparent requires
    dropping somebody else's field.

    Accepts a ``dict`` or a live ``Struct`` (see :func:`a2a_carrier_from_metadata` for why the
    ``Mapping`` annotation is not enough on its own); ``keys()`` and ``[]`` are implemented by both,
    ``dict(struct)`` is not a faithful copy — it boxes nested values in protobuf wrappers.
    """
    metadata: dict[str, Any] = {}
    if existing:
        for key in list(existing.keys()):
            metadata[key] = existing[key]
    propagate.inject(metadata)
    return metadata


# ---------------------------------------------------------------------------
# Span links across an asynchronous wait
# ---------------------------------------------------------------------------


def remote_span_context(
    trace_id: str | int, span_id: str | int, *, is_remote: bool = True
) -> SpanContext:
    """Build a :class:`SpanContext` from hex-or-int ids, for use as a link target.

    Accepts either form because the ids arrive from two places with two spellings: an in-process
    :class:`SpanContext` carries ints, and a value read back from OpenSearch carries the 32- and
    16-character lowercase hex that OTLP puts on the wire.
    """
    return SpanContext(
        trace_id=_as_int(trace_id, bits=128, field="trace_id"),
        span_id=_as_int(span_id, bits=64, field="span_id"),
        is_remote=is_remote,
        trace_flags=trace.TraceFlags(0x01),
    )


def _as_int(value: str | int, *, bits: int, field: str) -> int:
    if isinstance(value, int):
        parsed = value
    else:
        text = value.strip().lower()
        if len(text) != bits // 4 or any(c not in "0123456789abcdef" for c in text):
            raise ValueError(
                f"{field} must be {bits // 4} hex characters (W3C), got {value!r}; an id of the "
                f"wrong width would produce a link that resolves to nothing"
            )
        parsed = int(text, 16)
    if parsed == 0 or parsed.bit_length() > bits:
        raise ValueError(f"{field} must be a non-zero {bits}-bit value, got {value!r}")
    return parsed


def resume_after_wait(
    parked: SpanContext | None,
    name: str,
    *,
    tracer: Any = None,
    links: tuple[Link, ...] = (),
    **span_kwargs: Any,
) -> AbstractContextManager[Span]:
    """Start a NEW root span that LINKS to ``parked``, for work resuming after an async wait.

    §15: "Async workers/HITL create a new trace + span link after a long wait." The returned
    context manager yields the new span; the new trace's ``trace_id`` differs from ``parked``'s,
    and ``links`` records the relationship.

    **The new span is forced to be a root** by starting it from an empty :class:`Context`. Reading
    the ambient context instead would make the result depend on what happened to be current when
    the worker woke up: if the parked span were still active the span would become a *child* in the
    old trace, which is exactly the outcome this function exists to prevent, and it would happen
    only sometimes.

    ``tracer`` should be the one from :func:`~referraleasy_telemetry.setup.configure_telemetry`.
    There is a default — the global tracer — but passing it explicitly is the intended use: a
    process whose provider was configured with ``set_global=False`` (as the tests do) would
    otherwise get a non-recording no-op span and no error, which is the quietest possible failure.

    ``parked`` may be ``None`` when the parked span's ids were not recorded. The new trace is then
    a plain root: losing the link is recoverable, inventing a link to a non-existent span is not.
    """
    link_list = list(links)
    if parked is not None:
        link_list.insert(0, Link(parked, attributes={"link.reason": "resumed_after_wait"}))
    active_tracer = tracer if tracer is not None else trace.get_tracer(_RESUMING_SCOPE)
    return active_tracer.start_as_current_span(
        name, context=Context(), links=tuple(link_list), **span_kwargs
    )


#: Tracer name used by :func:`resume_after_wait` when the caller has no tracer of its own.
_RESUMING_SCOPE: Final[str] = "referraleasy.telemetry.resume"


def current_ids() -> tuple[str, str]:
    """``(trace_id_hex, span_id_hex)`` of the active span, or empty strings when there is none.

    Hex, not int, because that is how the ids appear on a ``traceparent`` header, in an OpenSearch
    document, and in a trace waterfall URL — so this value can be compared with what a reader sees
    without a conversion step in between.
    """
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return "", ""
    return format_trace_id(ctx.trace_id), format_span_id(ctx.span_id)


def current_trace_id() -> str | None:
    """The active ``trace_id`` as hex, or ``None`` outside a span.

    Returned for *correlation* only. It must never be handed to
    :func:`referraleasy_telemetry.events.build_event` as a dimension; that function refuses it and
    §15 requires it ("trace IDs are not used as metric labels").
    """
    trace_id, _ = current_ids()
    return trace_id or None


def new_span_id() -> str:
    """A fresh random 8-byte span id, hex encoded, for synthesising a standalone marker span."""
    return format_span_id(uuid.uuid4().int & ((1 << 64) - 1))


@contextmanager
def attached(remote: SpanContext) -> Iterator[Span]:
    """Run a block as if ``remote`` were the active span, without emitting anything.

    Used when code needs to *read* a remote context — to stamp its ids onto an event, say — but is
    not itself continuing the trace.
    """
    span: Span = NonRecordingSpan(remote)
    with use_span(span, end_on_exit=False):
        yield span


@dataclass(slots=True)
class TraceIdentity:
    """Convenience bundle: the correlation ids plus the trace/span ids of the current span.

    Purely a read model — it does not create spans. A service builds one at the start of a unit of
    work and passes it down.
    """

    correlation: CorrelationIds = field(default_factory=CorrelationIds)
    trace_id: str | None = None
    span_id: str | None = None

    def attributes(self) -> dict[str, str]:
        """The correlation ids plus ``trace_id``/``span_id`` as *fields*.

        Note the asymmetry, which is the whole of the §15 rule: the ids are attached to the record
        so a reader can navigate to the trace, and they are absent from
        :data:`conventions.DIMENSION_ATTRIBUTES` so no chart can group by them.
        """
        attrs = self.correlation.attributes()
        if self.trace_id:
            attrs["trace_id"] = self.trace_id
        if self.span_id:
            attrs["span_id"] = self.span_id
        return attrs
