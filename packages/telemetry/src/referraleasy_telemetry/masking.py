"""PII and secret masking, applied to a record on its way out of the process (§15).

§15: "PII/secrets in trace/log text are masked before export." The requirement has two
clauses and both matter.

**Before export.** Masking happens in the exporter, at the last possible moment, over the exact
attribute map that is about to be serialised. That placement is deliberate and is the strongest
reading of the requirement: an attribute added by a later processor, or by a span that outlives the
call that started it, still passes through this function, because there is no path from a record to
the wire that skips it. Masking earlier — in a processor — would be masking a copy.

**Not just the obvious fields.** Two mechanisms run together, because either alone has a hole:

* :data:`DEFAULT_REDACT_KEYS` names fields whose *meaning* is sensitive regardless of content
  (``authorization``, ``patient.email``, ...). The value is replaced; the presence of the key is
  kept, because "there was an authorization header and it was empty" and "there was no
  authorization header" are different facts when someone is debugging a 401.
* :data:`DEFAULT_SCRUB_PATTERNS` looks at *values* under names nobody anticipated — an e-mail that
  arrives as ``note``, a bearer token that arrives as ``upstream_response``. Key lists only ever
  cover the fields someone thought of.

This is a second layer, not the only one: ``infra/otel/otel-collector-config.yaml`` runs
``transform/mask_pii`` and ``redaction/scrub`` on everything that reaches the collector. Two
independent layers is the intent — the application one is the one that can be tested here, and the
collector one still holds if a service forgets to configure this package.
**What must NOT be masked.** A pseudonym is stable: the same patient identifier always hashes to
the same value, so a referral can still be followed across spans without the raw identifier being
stored. And the identifiers that make the telemetry navigable — ``trace_id``, ``span_id``,
``event.id`` — are exempt from value scrubbing, because a 32-character lowercase hex trace id
looks exactly like a 32-character opaque token to a regular expression. Masking one would leave
every record in the index pointing at a trace that does not exist, and the failure would be silent.
This is tested, not assumed: see ``test_masking.py::test_trace_id_survives_value_scrubbing``.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Final

logger = logging.getLogger(__name__)

#: What a redacted value becomes. A single sentinel rather than ``""`` or ``None`` so a reader can
#: tell "removed by the masker" from "the producer sent an empty string".
REDACTED: Final[str] = "[REDACTED]"

#: Suffix appended to a key whose value was replaced by a stable pseudonym.
PSEUDONYM_SUFFIX: Final[str] = "_hash"


def pseudonymise(value: str) -> str:
    """The stable, non-reversible pseudonym of ``value`` — SHA-256, lowercase hex.

    Saltless and therefore brute-forceable for low-entropy inputs; that is an accepted property
    here and is worth stating rather than hiding. The identifier being hashed is a synthetic
    fixture value in a demo, the salt would have to live somewhere in a repo whose whole premise is
    that it needs no credentials, and the alternative — dropping the identifier entirely — would
    break the correlation this pseudonym exists to preserve. The function is deliberately *not*
    presented as anonymisation.

    Hand-written rather than delegated so that it agrees byte-for-byte with the OTTL ``SHA256``
    the collector applies to the same fields: both produce plain lowercase hex, so a record that
    was hashed at the application and seen again at the collector is hashed to the same value
    rather than to a second one.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


#: Attribute keys whose value is always replaced outright. Matched on the flattened key, so
#: ``http.request.header.authorization`` is what is listed — the same spelling an OpenSearch
#: document shows after Data Prepper flattens it.
DEFAULT_REDACT_KEYS: Final[frozenset[str]] = frozenset(
    {
        # Credentials in any casing a framework might use.
        "authorization",
        "http.request.header.authorization",
        "http.response.header.set-cookie",
        "set-cookie",
        "cookie",
        "http.request.header.cookie",
        "x-api-key",
        "http.request.header.x-api-key",
        "api_key",
        "apikey",
        "auth.token",
        "service_token",
        # Direct identifiers.
        "patient.name",
        "patient.email",
        "patient.phone",
        "patient.address",
        "patient.dob",
        "patient.ssn",
        "patient.identifier",
        "patient.mrn",
        # Prompt and response bodies: a model prompt is free text and free text is where an
        # identifier that was never put in a named field ends up. §15 does not ask for prompt
        # capture and does not ask for reasoning capture, so the safe default is not to ship either.
        "gen_ai.prompt",
        "gen_ai.completion",
        "llm.prompt",
        "llm.response",
    }
)

#: Attribute keys whose value is replaced by a stable pseudonym under ``<key>_hash`` instead of
#: being dropped. These are the identifiers a reader still needs to *follow* a referral with.
DEFAULT_PSEUDONYM_KEYS: Final[frozenset[str]] = frozenset(
    {
        "patient.identifier",
        "patient.mrn",
        "patient.ref",
    }
)

#: Value patterns scrubbed wherever they appear, under any key. Deliberately conservative: each
#: pattern is something that has no business being in a telemetry value and is unlikely to collide
#: with an id this platform emits. There is no generic "long string" rule, because that is exactly
#: the rule that would eat a trace id.
DEFAULT_SCRUB_PATTERNS: Final[tuple[str, ...]] = (
    # e-mail
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
    # JWT / three-segment opaque token
    r"[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{8,}",
    # US phone number
    r"\b\d{3}[-. ]\d{3}[-. ]\d{4}\b",
    # Authorization scheme followed by a credential: "Bearer <token>", "Basic <b64>"
    r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._\-+/=]{8,}",
    # A named secret in a free-text string: "api_key=...", "token: ..."
    r"\b(?:api[_\-]?key|secret|password|passwd|token)\b\s*[:=]\s*\S+",
    # Long hex or base64 blobs that are not ids this platform emits: 40+ characters. Kept well
    # above the 32 hex characters of a trace id and the 16 of a span id on purpose.
    r"\b[A-Fa-f0-9]{40,}\b",
    r"\b[A-Za-z0-9+/]{48,}={0,2}\b",
)

#: Keys that are never value-scrubbed, whatever they contain.
#:
#: Every entry is an identifier this platform *needs* to stay readable. A trace id is 32 hex
#: characters; a span id is 16; an ``event.id`` is a UUID; a ``metric.name`` contains dotted
#: segments that the JWT pattern would match if the segments were long enough. Scrubbing any of
#: them breaks navigation while protecting nothing, since none of them is PII.
EXEMPT_FROM_VALUE_SCRUBBING: Final[frozenset[str]] = frozenset(
    {
        "trace_id",
        "span_id",
        "parent_span_id",
        "event.id",
        "turn.id",
        "a2a.task_id",
        "a2a.context_id",
        "a2a.message_id",
        "conversation_id",
        "workflow_id",
        "command_id",
        "booking_request_id",
        "metric.name",
        "metric.unit",
        "event.name",
        "event.action",
        "api.endpoint",
    }
)


@dataclass(frozen=True, slots=True)
class MaskingPolicy:
    """A compiled masking policy. Frozen, so a service can share one across threads safely."""

    redact_keys: frozenset[str] = DEFAULT_REDACT_KEYS
    pseudonym_keys: frozenset[str] = DEFAULT_PSEUDONYM_KEYS
    scrub_patterns: tuple[str, ...] = DEFAULT_SCRUB_PATTERNS
    exempt_keys: frozenset[str] = EXEMPT_FROM_VALUE_SCRUBBING
    _compiled: tuple[re.Pattern[str], ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        # ``frozen=True`` blocks normal assignment, so the compiled cache is planted with
        # ``object.__setattr__``. Compiling once per policy rather than once per value matters: the
        # exporter runs this on every attribute of every record.
        object.__setattr__(self, "_compiled", tuple(re.compile(p) for p in self.scrub_patterns))

    @property
    def patterns(self) -> tuple[re.Pattern[str], ...]:
        """The scrub patterns, compiled."""
        return self._compiled

    def mask_attribute(self, key: str, value: Any) -> tuple[str, Any] | None:
        """Mask one attribute.

        Returns the ``(key, value)`` pair to emit, or ``None`` when the attribute is dropped
        entirely.

        Order matters and is not arbitrary:

        1. A key in ``pseudonym_keys`` becomes ``<key>_hash`` — the pseudonym is computed first so
           the hashed form is never itself scrubbed.
        2. A key in ``redact_keys`` is replaced by :data:`REDACTED`.
        3. A key in ``exempt_keys`` passes through untouched.
        4. Anything else has its *value* scrubbed by pattern, recursing into lists and mappings.
        """
        flat = key
        if flat in self.pseudonym_keys:
            text = _flatten_scalar(value)
            if text is None:
                return key, REDACTED
            return f"{key}{PSEUDONYM_SUFFIX}", pseudonymise(text)

        if flat in self.redact_keys:
            return key, REDACTED

        if flat in self.exempt_keys:
            return key, value

        # A pseudonym that has already been computed passes through unchanged. Without this the
        # masker is not idempotent, and the failure is destructive rather than noisy: a 64-character
        # lowercase hex digest matches the "long hex blob" rule below, so a second pass replaces the
        # pseudonym with `[REDACTED]` and the identifier a reader was following the referral by is
        # gone. Measured — it is what ``test_masking_is_idempotent`` caught.
        #
        # The test is a *shape*, not a key name, so a value under some unrelated `_hash` key that is
        # not 64 lowercase hex characters is still scrubbed. `[A-Fa-f0-9]{40,}` cannot be narrowed
        # to drop the overlap: this module's own pseudonym is deliberately long, and making the
        # fallback pattern short enough to miss it would also stop it catching what it is for.
        if flat.endswith(PSEUDONYM_SUFFIX) and _is_pseudonym(value):
            return key, value

        return key, self.scrub(value)

    def scrub(self, value: Any) -> Any:
        """Replace pattern matches inside ``value``, recursing into containers.

        Non-string scalars pass through: a number cannot carry an e-mail address, and stringifying
        it to check would change its type on the way to a mapped numeric field.
        """
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, Mapping):
            return {k: self.scrub(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub(v) for v in value]
        return value

    def scrub_text(self, text: str) -> str:
        """Replace every pattern match in a string with :data:`REDACTED`."""
        for pattern in self._compiled:
            text = pattern.sub(REDACTED, text)
        return text

    def mask_mapping(self, attributes: Mapping[str, Any]) -> dict[str, Any]:
        """Mask a whole attribute map, the operation an exporter performs per record."""
        masked: dict[str, Any] = {}
        for key, value in attributes.items():
            result = self.mask_attribute(key, value)
            if result is None:
                continue
            masked_key, masked_value = result
            masked[masked_key] = masked_value
        return masked


#: The policy this repo ships. Constructed once; the compiled regexes are shared.
DEFAULT_POLICY: Final[MaskingPolicy] = MaskingPolicy()


def _flatten_scalar(value: Any) -> str | None:
    """``value`` as text when it is a scalar, else ``None``.

    ``None`` means "cannot be pseudonymised as-is" and the caller falls back to redaction. It is
    better to over-mask than to hash ``repr()`` of a structure and call the result a pseudonym.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) and not isinstance(value, bool):
        return str(value)
    return None


def _is_pseudonym(value: Any) -> bool:
    """True when ``value`` is exactly what :func:`pseudonymise` produces.

    A shape test rather than a key test, so a `_hash` key holding something else is still scrubbed.
    """
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


# ---------------------------------------------------------------------------
# Exporter wrapper
# ---------------------------------------------------------------------------


def mask_span_attributes(
    attributes: Mapping[str, Any], policy: MaskingPolicy = DEFAULT_POLICY
) -> dict[str, Any]:
    """Mask one span's attributes. Public so a test can call it without an exporter."""
    return policy.mask_mapping(attributes)


def mask_log_record(
    attributes: Mapping[str, Any],
    body: str | None,
    policy: MaskingPolicy = DEFAULT_POLICY,
) -> tuple[dict[str, Any], str | None]:
    """Mask a log record's attributes *and* its body, returning both.

    The body is masked too because it is a free-text field: ``body`` is where a message that
    interpolated an identifier ends up, and it is the field a reader is most likely to eyeball.
    """
    return policy.mask_mapping(attributes), (policy.scrub_text(body) if body else body)


class MaskingExporter:
    """Wraps a span exporter, masking every span's attributes on the way past.

    Structured as a decorator around whatever exporter is configured rather than as a filter
    *inside* one, so the guarantee is a property of the wiring: there is no exporter in this
    package that can be selected without the masker, and adding a new backend cannot skip it.

    Only the attributes are rewritten. The span's identity, timing, status and links are left
    exactly as the SDK produced them — a masked ``trace_id`` would be worse than a leaked one,
    because the leak is visible in a review and the broken id is not.

    **This wrapper writes, it does not copy, and that is a deliberate trade.** A span handed to a
    ``SimpleSpanProcessor`` is the same object every other exporter on the provider receives, so
    masking here also masks what they see. The alternative — masking a copy — is not available:
    the exporter protocol hands the inner exporter the span object itself, and there is no seam to
    substitute one. Masking someone else's view is the failure that leaks nothing; the failure it
    trades against is a service that wanted the raw value, which is not a thing a telemetry
    exporter is entitled to want. Masking is idempotent, so a second masking exporter downstream
    is a no-op rather than a second hash.

    Note that the OTLP exporters in :mod:`referraleasy_telemetry.export` mask as they encode and
    therefore do *not* rest on this wrapper. This class exists so that a third-party exporter —
    one that someone else writes and this package does not control — can be wrapped without
    editing it.
    """

    def __init__(self, inner: Any, policy: MaskingPolicy = DEFAULT_POLICY) -> None:
        self._inner = inner
        self._policy = policy
        self.masked_records = 0
        #: Spans that needed masking and could not be masked. Non-zero means raw attributes
        #: reached the wrapped exporter; see :meth:`_mask_span`.
        self.unmasked_records = 0

    # -- protocol the SDK calls --------------------------------------------------
    def export(self, spans: Any) -> Any:
        for span in spans:
            self._mask_span(span)
        return self._inner.export(spans)

    def shutdown(self) -> None:
        shutdown = getattr(self._inner, "shutdown", None)
        if shutdown is not None:
            shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        force_flush = getattr(self._inner, "force_flush", None)
        if force_flush is None:
            return True
        return bool(force_flush(timeout_millis))

    # -- internals ---------------------------------------------------------------
    def _mask_span(self, span: Any) -> None:
        attributes = getattr(span, "attributes", None)
        if not attributes:
            return
        masked = mask_span_attributes(attributes, self._policy)
        if masked == dict(attributes):
            return
        if _replace_attributes(span, attributes, masked):
            self.masked_records += 1
        else:
            # Loud, because a masker that passes data through is worse than no masker: without one
            # the gap is a missing feature, with a broken one it is a false assurance. The count is
            # the number of spans that went out raw.
            self.unmasked_records += 1
            logger.warning(
                "could not mask attributes on a %s pointing at %s; the wrapped exporter will "
                "receive them unmasked",
                type(span).__name__,
                getattr(getattr(span, "context", None), "trace_id", None),
            )


def _replace_attributes(span: Any, original: Mapping[str, Any], masked: Mapping[str, Any]) -> bool:
    """Write ``masked`` over ``span``'s attributes in place. Returns whether it worked.

    **``ReadableSpan.attributes`` cannot be assigned to, and the first version of this function
    assumed it could.** It is a property that returns ``MappingProxyType(self._attributes or {})``
    — a *fresh* read-only view built on each access — so ``del span.attributes[key]`` raises
    ``TypeError`` and, worse, the ``except TypeError`` that was meant to handle an exotic third-party
    span caught it on the ordinary path and did nothing observable. ``masked_records`` incremented
    and the inner exporter received the raw values. That is a silent no-op on the privacy path, so
    it is now: the real store is mutated directly, and a failure to do so is counted and logged
    rather than absorbed.

    The store behind the property is ``span._attributes``, the SDK's own ``BoundedAttributes``,
    which is a ``MutableMapping`` and is what the inner exporter reads back through the property.
    Reaching for a private field is not ideal; it is the only seam there is, it is verified against
    the installed SDK (see ``tests/telemetry/test_masking.py``), and the failure mode if a future
    version renames it is the loud one above rather than the quiet one.
    """
    store = getattr(span, "_attributes", None)
    if isinstance(store, MutableMapping):
        for key in list(store):
            if key not in masked:
                del store[key]
        store.update(masked)
        return True

    # Second try: a span type whose `attributes` really is a mutable mapping.
    attributes = getattr(span, "attributes", None)
    if isinstance(attributes, MutableMapping):
        for key in list(original):
            if key not in masked:
                del attributes[key]
        attributes.update(masked)
        return True

    return False
