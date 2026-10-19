"""Masking: PII and secrets are gone before anything leaves the process (§15).

The test the phase brief asks for is here and is the first one: a **planted value** that must not
appear in the exported payload. Planted means the literal is written into the test and then searched
for in what the exporter produced — not "the field is redacted", which only proves the field was
named, but "this exact string is nowhere in the bytes we are about to send".

``test_pipeline_live.py`` repeats the same planted-value assertion against a document read back out
of OpenSearch, which additionally covers the collector's own masking layer. The two are not
redundant: this one fails fast and locally when the application layer breaks; that one proves the
whole path.

**The class with the worst history here is** :class:`TestMaskingExporterActuallyWrites`. The first
version of :class:`~referraleasy_telemetry.masking.MaskingExporter` mutated
``span.attributes``, which is a property returning a fresh ``MappingProxyType`` on every access — so
every write raised ``TypeError`` into an ``except TypeError`` that was meant for exotic third-party
spans. The count incremented, the wrapped exporter received the raw values, and nothing failed.
These tests exist so that the next version of that mistake fails loudly.
"""

from __future__ import annotations

import json

import pytest

from referraleasy_telemetry.export import encode_attributes, encode_log_records, encode_spans
from referraleasy_telemetry.masking import (
    DEFAULT_POLICY,
    REDACTED,
    MaskingExporter,
    MaskingPolicy,
    mask_log_record,
    mask_span_attributes,
    pseudonymise,
)

#: A value planted in a field nobody would think to list, so the *pattern* layer is what catches it.
PLANTED_EMAIL = "jane.doe@example.org"
#: A credential planted under a name that is not in any key list.
PLANTED_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.abcdefghijklmnop"
#: An identifier under a listed key, so stable pseudonymisation is what handles it.
PLANTED_IDENTIFIER = "MRN-0000001234"
#: A 32-character lowercase hex string that is *not* an id.
PLANTED_HEX_ID_SHAPED = "c0ffee1234567890abcdef1234567890"
#: A longer hex run — past the 40-character threshold at which a value is treated as a suspect
#: opaque token rather than as an id.
PLANTED_HEX_BLOB = "c0ffee1234567890abcdef1234567890fedcba9876543210"


class TestPlantedValueNeverLeaves:
    """The requirement, as a search for a literal string."""

    def test_a_planted_email_does_not_appear_in_the_masked_map(self) -> None:
        masked = mask_span_attributes(
            {"note": f"patient wrote in at {PLANTED_EMAIL}", "agent.role": "care_coordinator"}
        )
        assert PLANTED_EMAIL not in json.dumps(masked)
        assert REDACTED in masked["note"]
        # And the harmless value beside it is untouched, so the masking is not a blunt clear().
        assert masked["agent.role"] == "care_coordinator"

    def test_a_planted_token_under_an_unexpected_key_is_caught(self) -> None:
        masked = mask_span_attributes({"upstream_response": f"token={PLANTED_TOKEN}"})
        assert PLANTED_TOKEN not in json.dumps(masked)

    def test_a_planted_phone_number_is_caught(self) -> None:
        masked = mask_span_attributes({"notes": "call back on 555-123-4567"})
        assert "555-123-4567" not in json.dumps(masked)

    def test_the_exported_span_payload_contains_no_planted_value(self, span_factory) -> None:
        """The strongest form this file can offer: assert on the bytes the exporter would send."""
        span = span_factory(
            attributes={
                "authorization": f"Bearer {PLANTED_TOKEN}",
                "patient.email": PLANTED_EMAIL,
                "patient.identifier": PLANTED_IDENTIFIER,
                "note": f"reached {PLANTED_EMAIL} at 555-123-4567",
            }
        )
        payload = json.dumps(encode_spans([span]))

        assert PLANTED_EMAIL not in payload
        assert PLANTED_TOKEN not in payload
        assert PLANTED_IDENTIFIER not in payload
        assert "555-123-4567" not in payload
        assert REDACTED in payload

    def test_the_exported_log_payload_contains_no_planted_value(self, log_record_factory) -> None:
        record = log_record_factory(
            body=f"guardrail blocked a message containing {PLANTED_EMAIL}",
            attributes={"api_key": PLANTED_TOKEN, "detail": PLANTED_EMAIL},
        )
        payload = json.dumps(encode_log_records([record]))

        assert PLANTED_EMAIL not in payload
        assert PLANTED_TOKEN not in payload
        assert REDACTED in payload

    def test_masking_an_exported_span_does_not_disturb_the_span_object(self, span_factory) -> None:
        """``encode_span`` masks the copy it encodes, so the span a *second* exporter sees is
        unchanged. Two exporters attached to one provider must not fight over one object."""
        span = span_factory(attributes={"patient.email": PLANTED_EMAIL})
        encode_spans([span])
        assert span.attributes["patient.email"] == PLANTED_EMAIL

    def test_masking_is_idempotent(self) -> None:
        """A retry, or two exporters, must not hash a hash — or destroy the pseudonym.

        This failed when it was first written, and the direction of the failure is the interesting
        part: the second pass did not double-hash, it replaced the pseudonym with ``[REDACTED]``,
        because a 64-character lowercase hex digest matches the "long hex blob" scrub rule. Two
        masking layers would therefore have silently deleted every patient pseudonym, and the
        export would still have looked correct.
        """
        once = mask_span_attributes({"patient.identifier": PLANTED_IDENTIFIER})
        twice = mask_span_attributes(once)
        assert once == twice
        assert once["patient.identifier_hash"] != REDACTED

    def test_a_whole_span_survives_being_masked_twice(self, span_factory) -> None:
        """The same property one level up, because the write-through wrapper really can double
        apply: ``MaskingExporter`` wraps an exporter that may mask again as it encodes."""
        span = span_factory(attributes={"patient.mrn": PLANTED_IDENTIFIER, "k": "v"})
        first = mask_span_attributes(span.attributes)
        assert mask_span_attributes(first) == first


class TestTraceIdentitySurvives:
    """The rule that makes masking safe: it must not destroy navigability.

    A 32-character lowercase hex trace id is, to a regular expression, indistinguishable from a
    32-character opaque token. A scrubber with a generic "long string" rule would replace every
    trace id in the index with ``[REDACTED]`` and every record would point at a trace that does not
    exist — silently, because nothing errors.
    """

    def test_trace_id_survives_value_scrubbing(self) -> None:
        trace_id = "5b8efff798038103d269b633813fc60c"
        masked = mask_span_attributes({"trace_id": trace_id, "span_id": "eee19b7ec3c1b174"})
        assert masked["trace_id"] == trace_id
        assert masked["span_id"] == "eee19b7ec3c1b174"

    def test_the_dedup_key_survives(self) -> None:
        event_id = "97a00503-61d6-558d-a1a6-c957518f0b1b"
        assert mask_span_attributes({"event.id": event_id})["event.id"] == event_id

    def test_every_correlation_id_survives(self) -> None:
        from referraleasy_telemetry.conventions import CORRELATION_ATTRIBUTES

        values = {key: f"value-of-{key}-" + "x" * 40 for key in CORRELATION_ATTRIBUTES}
        masked = mask_span_attributes(values)
        for key, value in values.items():
            assert masked[key] == value, f"{key} was altered; navigation depends on it"

    def test_a_metric_name_is_not_mistaken_for_a_token(self) -> None:
        """Dotted names are the shape the JWT pattern looks for."""
        masked = mask_span_attributes({"metric.name": "latency.attempt.ms"})
        assert masked["metric.name"] == "latency.attempt.ms"

    def test_a_long_hex_blob_that_is_not_an_exempt_field_is_still_scrubbed(self) -> None:
        """The exemption is a list of known keys, not a length rule: an unlisted 40+ character hex
        value is still treated as a suspect token."""
        masked = mask_span_attributes({"unexpected_payload": PLANTED_HEX_BLOB})
        assert PLANTED_HEX_BLOB not in masked["unexpected_payload"]
        assert REDACTED in masked["unexpected_payload"]

    def test_a_trace_id_shaped_value_under_an_unknown_key_survives(self) -> None:
        """The accepted cost of the rule above, stated rather than left to be discovered.

        A 32-character lowercase hex value under an *unlisted* key is not scrubbed, because 32 hex
        characters is exactly a ``trace_id`` and the pattern cannot tell the two apart. The hole is
        narrow on purpose and is worth being explicit about: a value made only of ``[0-9a-f]`` and
        exactly as long as a trace id cannot carry a name, an e-mail, a phone number or a token —
        it can only carry something that already looks like a hex id. The alternative rule, a
        blanket "long alphanumeric" match, deletes every ``trace_id`` in the index, which is the
        failure this whole section exists to prevent.
        """
        masked = mask_span_attributes({"unexpected_payload": PLANTED_HEX_ID_SHAPED})
        assert masked["unexpected_payload"] == PLANTED_HEX_ID_SHAPED

    def test_the_same_value_under_a_trace_id_key_is_exempt_for_the_same_reason(self) -> None:
        """The two cases are the same case; only the consequence differs."""
        assert mask_span_attributes({"trace_id": PLANTED_HEX_ID_SHAPED})["trace_id"] == (
            PLANTED_HEX_ID_SHAPED
        )

    def test_a_span_payload_keeps_its_ids_after_export(self, span_factory) -> None:
        """End to end for the exemption: the ids in the encoded payload are the span's own."""
        span = span_factory(attributes={"note": f"wrote to {PLANTED_EMAIL}"})
        payload = json.dumps(encode_spans([span]))
        assert f"{span.context.trace_id:032x}" in payload
        assert f"{span.context.span_id:016x}" in payload


class TestKeyPolicy:
    """Named keys, and what happens to the ones nobody listed."""

    def test_an_authorization_header_keeps_its_key(self) -> None:
        """The presence of the key is a signal — "there was a header and it was empty" and "there
        was no header" are different facts when debugging a 401."""
        assert mask_span_attributes({"authorization": "Bearer xyz"}) == {"authorization": REDACTED}

    def test_pseudonymisation_is_stable_and_renames_the_key(self) -> None:
        first = mask_span_attributes({"patient.mrn": PLANTED_IDENTIFIER})
        second = mask_span_attributes({"patient.mrn": PLANTED_IDENTIFIER})
        assert first == second
        assert first["patient.mrn_hash"] == pseudonymise(PLANTED_IDENTIFIER)

    def test_the_pseudonym_matches_the_collectors_sha256(self) -> None:
        """The collector's OTTL ``SHA256`` produces plain lowercase hex. If the two disagreed, the
        same patient would have two hashes depending on which layer saw the field first."""
        import hashlib

        assert pseudonymise("abc") == hashlib.sha256(b"abc").hexdigest()
        assert len(pseudonymise("abc")) == 64

    def test_a_pseudonym_is_not_the_original(self) -> None:
        masked = mask_span_attributes({"patient.identifier": PLANTED_IDENTIFIER})
        assert PLANTED_IDENTIFIER not in json.dumps(masked)

    def test_a_non_scalar_under_a_pseudonym_key_falls_back_to_redaction(self) -> None:
        """Hashing ``repr()`` of a structure and calling it a pseudonym would be a lie."""
        masked = mask_span_attributes({"patient.identifier": {"nested": 1}})
        assert masked["patient.identifier"] == REDACTED

    def test_a_numeric_pseudonym_value_is_accepted(self) -> None:
        masked = mask_span_attributes({"patient.mrn": 1234567})
        assert masked["patient.mrn_hash"] == pseudonymise("1234567")

    def test_nested_structures_are_scrubbed_recursively(self) -> None:
        masked = mask_span_attributes(
            {"payload": {"contact": PLANTED_EMAIL}, "list": [PLANTED_EMAIL]}
        )
        assert PLANTED_EMAIL not in json.dumps(masked)
        assert REDACTED in masked["payload"]["contact"]
        assert REDACTED in masked["list"][0]

    def test_numbers_are_not_stringified(self) -> None:
        """A numeric attribute stays numeric so it lands in a mapped numeric field."""
        masked = mask_span_attributes({"metric.value": 1234.5, "api.status_code": 503})
        assert masked["metric.value"] == 1234.5
        assert masked["api.status_code"] == 503

    def test_a_prompt_body_is_redacted_wholesale(self) -> None:
        """Free text is where an identifier that was never put in a named field ends up, and §15
        asks for neither prompt nor reasoning capture."""
        masked = mask_span_attributes({"gen_ai.prompt": "Summarise the referral for Jane Doe"})
        assert masked["gen_ai.prompt"] == REDACTED


class TestLogBodiesAndExportValues:
    def test_a_log_body_is_scrubbed(self) -> None:
        _, body = mask_log_record({}, f"contacted {PLANTED_EMAIL}")
        assert body is not None
        assert PLANTED_EMAIL not in body
        assert REDACTED in body

    def test_a_none_body_stays_none(self) -> None:
        _, body = mask_log_record({}, None)
        assert body is None

    def test_a_log_body_that_is_a_planted_token_is_scrubbed(self) -> None:
        _, body = mask_log_record({}, f"upstream said Bearer {PLANTED_TOKEN}")
        assert PLANTED_TOKEN not in (body or "")

    def test_encoded_attributes_are_key_value_pairs(self) -> None:
        assert encode_attributes({"a": 1}) == [{"key": "a", "value": {"intValue": "1"}}]


class TestMaskingExporterActuallyWrites:
    """The wrapper, against a real ``ReadableSpan`` from the installed SDK.

    Every test here would have passed against the broken version, which is the point of writing them
    against the real object rather than a fake: the broken version counted the record and reported
    success. So the assertions are made on what the *wrapped* exporter received.
    """

    @staticmethod
    def _inner() -> tuple[list[list[object]], MaskingExporter]:
        """A stand-in inner exporter, and the wrapper around it."""
        received: list[list[object]] = []

        class Inner:
            def export(self, spans):
                received.append(list(spans))
                return "inner-result"

            def shutdown(self):
                received.append(["shutdown"])

            def force_flush(self, timeout_millis: int = 30_000) -> bool:
                return True

        return received, MaskingExporter(Inner())

    def test_the_wrapped_exporter_receives_the_masked_value(self, span_factory) -> None:
        received, wrapper = self._inner()
        wrapper.export([span_factory(attributes={"patient.email": PLANTED_EMAIL, "k": "v"})])

        (handed,) = received
        attributes = dict(handed[0].attributes)  # type: ignore[attr-defined]
        assert attributes["patient.email"] == REDACTED
        assert attributes["k"] == "v", "unrelated attributes must survive"

    def test_the_raw_value_is_gone_from_the_span_object(self, span_factory) -> None:
        _received, wrapper = self._inner()
        span = span_factory(attributes={"note": f"wrote to {PLANTED_EMAIL}"})
        wrapper.export([span])

        assert PLANTED_EMAIL not in json.dumps(dict(span.attributes))
        assert REDACTED in span.attributes["note"]

    def test_a_redacted_value_that_changes_nothing_is_not_counted(self, span_factory) -> None:
        _received, wrapper = self._inner()
        wrapper.export([span_factory(attributes={"agent.role": "care_coordinator"})])
        assert wrapper.masked_records == 0
        assert wrapper.unmasked_records == 0

    def test_nothing_is_reported_unmasked_on_the_normal_path(self, span_factory) -> None:
        """``unmasked_records`` is the counter that says raw attributes escaped. It must be zero."""
        _, wrapper = self._inner()
        wrapper.export([span_factory(attributes={"patient.email": PLANTED_EMAIL})])
        assert wrapper.unmasked_records == 0
        assert wrapper.masked_records == 1

    def test_a_span_whose_attributes_cannot_be_reached_is_counted_not_swallowed(self) -> None:
        """The guard against the original bug: a span that cannot be written to must raise the
        counter rather than let the value through in silence.

        The stand-in is modelled on what ``ReadableSpan`` actually is — a read-only view over a
        store this package does not know the name of — so both of the wrapper's attempts miss.
        """
        from types import MappingProxyType

        received, wrapper = self._inner()

        class Opaque:
            @property
            def attributes(self):
                return MappingProxyType({"patient.email": PLANTED_EMAIL})

        wrapper.export([Opaque()])
        assert wrapper.unmasked_records == 1
        assert wrapper.masked_records == 0
        # And the raw value did reach the wrapped exporter, which is exactly why the counter has to
        # be non-zero and the log line has to be a warning.
        assert PLANTED_EMAIL in json.dumps(dict(received[0][0].attributes))  # type: ignore[attr-defined]

    def test_a_third_party_span_with_a_mutable_attribute_map_is_masked(self) -> None:
        """The second attempt exists for a span type this package does not own: one whose
        ``attributes`` really is a plain mutable mapping."""
        _received, wrapper = self._inner()

        class Foreign:
            def __init__(self) -> None:
                self.attributes = {"patient.email": PLANTED_EMAIL, "k": "v"}

        span = Foreign()
        wrapper.export([span])
        assert span.attributes["patient.email"] == REDACTED
        assert span.attributes["k"] == "v"
        assert wrapper.masked_records == 1
        assert wrapper.unmasked_records == 0

    def test_the_result_of_the_inner_exporter_is_returned(self, span_factory) -> None:
        _, wrapper = self._inner()
        assert wrapper.export([span_factory()]) == "inner-result"

    def test_shutdown_and_force_flush_are_forwarded(self) -> None:
        received, wrapper = self._inner()
        assert wrapper.force_flush() is True
        wrapper.shutdown()
        assert ["shutdown"] in received

    def test_a_span_with_no_attributes_is_skipped(self, span_factory) -> None:
        _, wrapper = self._inner()
        wrapper.export([span_factory()])
        assert (wrapper.masked_records, wrapper.unmasked_records) == (0, 0)


class TestPolicyIsConfigurable:
    def test_a_custom_policy_replaces_the_default_key_lists(self) -> None:
        policy = MaskingPolicy(redact_keys=frozenset({"internal.note"}))
        masked = policy.mask_mapping({"internal.note": "secret", "patient.email": "not-listed"})
        assert masked["internal.note"] == REDACTED
        # The custom policy replaced the defaults, so the shipped key list no longer applies. That
        # is why the shipped policy is a module-level constant rather than a per-call default.
        assert masked["patient.email"] == "not-listed"

    def test_a_policy_with_no_patterns_scrubs_no_values(self) -> None:
        policy = MaskingPolicy(scrub_patterns=())
        assert policy.mask_mapping({"note": PLANTED_EMAIL})["note"] == PLANTED_EMAIL

    def test_patterns_are_compiled_once_and_shared(self) -> None:
        assert DEFAULT_POLICY.patterns is DEFAULT_POLICY.patterns
        assert all(hasattr(pattern, "sub") for pattern in DEFAULT_POLICY.patterns)

    def test_the_shipped_policy_covers_every_default_key(self) -> None:
        from referraleasy_telemetry.masking import DEFAULT_REDACT_KEYS

        assert DEFAULT_POLICY.redact_keys == DEFAULT_REDACT_KEYS
        assert DEFAULT_REDACT_KEYS, "an empty deny-list would make this package a no-op"

    def test_an_exporter_masks_by_its_own_policy(self, span_factory) -> None:
        """A custom policy must reach the exporter, not just the helper functions."""
        received, _ = TestMaskingExporterActuallyWrites._inner()
        policy = MaskingPolicy(redact_keys=frozenset({"patient.email"}))
        wrapper = MaskingExporter(_RecordIt(received), policy)
        wrapper.export([span_factory(attributes={"patient.email": PLANTED_EMAIL})])
        assert dict(received[0][0].attributes)["patient.email"] == REDACTED  # type: ignore[attr-defined]


class _RecordIt:
    """A minimal inner exporter that keeps what it was handed."""

    def __init__(self, sink: list[list[object]]) -> None:
        self._sink = sink

    def export(self, spans: object) -> None:
        self._sink.append(list(spans))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        "Bearer abcdefghijklmnop",
        "api_key=abcdefghijklmnop",
        "token: abcdefghijklmnop",
        "password=hunter2hunter2",
    ],
)
def test_named_secrets_in_free_text_are_scrubbed(value: str) -> None:
    """Free text is where a secret that was never put in a named field ends up."""
    assert REDACTED in mask_span_attributes({"detail": value})["detail"]
