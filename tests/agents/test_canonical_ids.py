"""Canonical JSON, payload hashing and identifier unwrapping.

Two things are pinned here, and both are the kind that fails silently otherwise:

1. **The hash rule.** An approval binds to one exact payload by hash, so the digest a specialist
   computes must be the digest the side that minted the approval computed. The rule is checked
   against a fixed vector — a literal payload, its literal canonical text and its literal digest —
   rather than by inspecting `json.dumps`'s keyword arguments and agreeing with them. A change to
   the rule that nothing recomputes is exactly the failure this guards.
2. **`id_text`, not `str()`.** Every contract identifier is a pydantic `RootModel`, whose `__str__`
   is its repr. `str(EvidenceId(...))` is not an id, and a request built from it asks for an object
   that does not exist. The test asserts both halves.
"""

from __future__ import annotations

import hashlib

import pytest

from referraleasy_agents.canonical import (
    canonical_json,
    deterministic_uuid,
    id_text,
    id_texts,
    payload_hash,
)
from referraleasy_contracts.generated.common import EvidenceId, HospitalId, Uuid

pytestmark = pytest.mark.contract

#: The fixed vector. `c` before `b` in the source is the point: the canonical form sorts the keys.
VECTOR_PAYLOAD: dict[str, object] = {
    "patient_id": "b0000000-0000-4000-8000-000000000001",
    "slot_id": "40000000-0000-4000-8000-000000000029",
    "visit_type": "first_visit",
    "notes": "café — synthetic",
    "tags": ["cardiology", "in-network"],
}

#: The one spelling: keys sorted, no whitespace, non-ASCII left as itself.
VECTOR_TEXT = (
    '{"notes":"café — synthetic","patient_id":"b0000000-0000-4000-8000-000000000001",'
    '"slot_id":"40000000-0000-4000-8000-000000000029","tags":["cardiology","in-network"],'
    '"visit_type":"first_visit"}'
)

#: The digest of `VECTOR_TEXT`, byte for byte.
VECTOR_DIGEST = "93d3967817ab44f6307a40b3c1da7b28aa37b2d1eb99b7a3119033bbdd2875f3"


def test_the_canonical_text_of_the_vector_is_the_expected_one() -> None:
    assert canonical_json(VECTOR_PAYLOAD) == VECTOR_TEXT


def test_the_vector_s_digest_is_the_expected_one() -> None:
    assert payload_hash(VECTOR_PAYLOAD) == VECTOR_DIGEST


def test_the_expected_digest_is_the_sha256_of_the_expected_text() -> None:
    """The two literal halves of the vector agree with each other."""
    assert hashlib.sha256(VECTOR_TEXT.encode("utf-8")).hexdigest() == VECTOR_DIGEST


def test_key_order_does_not_change_the_digest() -> None:
    reordered = {key: VECTOR_PAYLOAD[key] for key in sorted(VECTOR_PAYLOAD, reverse=True)}
    assert payload_hash(reordered) == VECTOR_DIGEST


def test_one_character_changes_the_digest() -> None:
    """`approval_payload_changed` is only detectable because this holds."""
    changed = {**VECTOR_PAYLOAD, "notes": "café — synthetic."}
    assert payload_hash(changed) != VECTOR_DIGEST


def test_array_order_is_part_of_the_payload() -> None:
    reversed_tags = {**VECTOR_PAYLOAD, "tags": list(reversed(VECTOR_PAYLOAD["tags"]))}  # type: ignore[call-overload]
    assert payload_hash(reversed_tags) != VECTOR_DIGEST


def test_non_ascii_is_not_escaped() -> None:
    assert "café" in canonical_json({"notes": "café"})


def test_id_text_unwraps_a_nested_root_model() -> None:
    raw = "50000000-0000-4000-8000-000000000022"
    assert id_text(EvidenceId(raw)) == raw
    assert id_text(Uuid(raw)) == raw
    assert id_text(HospitalId(raw)) == raw


def test_str_of_a_root_model_is_not_an_id() -> None:
    """The reason `id_text` exists, pinned so the shortcut cannot come back."""
    raw = "50000000-0000-4000-8000-000000000022"
    rendered = str(EvidenceId(raw))
    assert rendered != raw
    assert "root=" in rendered
    with pytest.raises(Exception):  # noqa: B017 - the point is that it refuses at all
        EvidenceId(rendered)


def test_id_text_passes_a_plain_string_through() -> None:
    assert id_text("not-a-uuid") == "not-a-uuid"


def test_id_texts_keeps_order_and_repetition() -> None:
    first = "50000000-0000-4000-8000-000000000022"
    second = "50000000-0000-4000-8000-000000000023"
    assert id_texts([EvidenceId(first), Uuid(second), EvidenceId(first)]) == [first, second, first]


def test_deterministic_uuid_is_stable_and_input_dependent() -> None:
    first = deterministic_uuid("constraint", "cardiology", "hard")
    assert first == deterministic_uuid("constraint", "cardiology", "hard")
    assert first != deterministic_uuid("constraint", "cardiology", "soft")
    assert first != deterministic_uuid("option", "cardiology", "hard")
