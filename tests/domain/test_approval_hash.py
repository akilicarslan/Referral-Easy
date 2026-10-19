"""The canonical booking-payload hash — the value patient consent is bound to (§9).

This hash is a cross-boundary contract: the application computes it, the database stores and
compares it, and an approval is valid only while the two agree. If the encoding changes, every
existing approval stops matching. Failing closed is the right outcome for consent, but it is also
why the encoding is pinned here rather than left to whoever edits the function next.
"""

from __future__ import annotations

import re

import pytest

from referraleasy_contracts.generated.booking import BookingPayload
from referraleasy_domain import PAYLOAD_HASH_PATTERN, canonical_booking_payload_hash

pytestmark = pytest.mark.contract

PATIENT = "11111111-1111-4111-8111-111111111111"
PROVIDER = "22222222-2222-4222-8222-222222222222"
HOSPITAL = "33333333-3333-4333-8333-333333333333"
SLOT = "44444444-4444-4444-8444-444444444444"

PAYLOAD: dict[str, object] = {
    "patient_id": PATIENT,
    "provider_id": PROVIDER,
    "hospital_id": HOSPITAL,
    "slot_id": SLOT,
    "visit_type": "first_visit",
}


def test_hash_matches_the_shape_the_database_enforces() -> None:
    """`approval_payload_hash_hex` is a CHECK constraint; a non-matching shape is refused."""
    assert re.fullmatch(PAYLOAD_HASH_PATTERN, canonical_booking_payload_hash(PAYLOAD))


def test_is_deterministic() -> None:
    assert canonical_booking_payload_hash(PAYLOAD) == canonical_booking_payload_hash(PAYLOAD)


def test_key_order_does_not_matter() -> None:
    shuffled = {k: PAYLOAD[k] for k in reversed(list(PAYLOAD))}
    assert canonical_booking_payload_hash(PAYLOAD) == canonical_booking_payload_hash(shuffled)


def test_absent_and_explicit_null_hash_the_same() -> None:
    """`{"phone": null}` and omitting it mean the same thing.

    If they hashed differently, a service echoing back an explicit null would silently invalidate a
    consent the patient never actually changed — the worst kind of false alarm, because it looks
    like a real payload change.
    """
    with_null = {**PAYLOAD, "contact_phone": None}
    assert canonical_booking_payload_hash(PAYLOAD) == canonical_booking_payload_hash(with_null)


def test_nested_nulls_are_stripped_too() -> None:
    left = {**PAYLOAD, "documents": [{"id": SLOT, "note": None}]}
    right = {**PAYLOAD, "documents": [{"id": SLOT}]}
    assert canonical_booking_payload_hash(left) == canonical_booking_payload_hash(right)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("patient_id", "55555555-5555-4555-8555-555555555555"),
        ("provider_id", "55555555-5555-4555-8555-555555555555"),
        ("hospital_id", "55555555-5555-4555-8555-555555555555"),
        ("slot_id", "55555555-5555-4555-8555-555555555555"),
        ("visit_type", "return_visit"),
    ],
)
def test_any_booking_relevant_change_changes_the_hash(field: str, value: str) -> None:
    """Precisely the invalidation §9 asks for: change the booking, lose the consent."""
    assert canonical_booking_payload_hash(
        {**PAYLOAD, field: value}
    ) != canonical_booking_payload_hash(PAYLOAD)


def test_adding_a_field_changes_the_hash() -> None:
    changed = {**PAYLOAD, "contact_phone": "+13125550142"}
    assert canonical_booking_payload_hash(changed) != canonical_booking_payload_hash(PAYLOAD)


def test_accepts_a_contract_model_and_agrees_with_its_dict_form() -> None:
    """The wire form is what matters, so a model must hash identically to its JSON dump."""
    model = BookingPayload.model_validate(PAYLOAD)
    assert canonical_booking_payload_hash(model) == canonical_booking_payload_hash(
        model.model_dump(mode="json", exclude_none=True)
    )


def test_non_ascii_is_not_escaped() -> None:
    """`ensure_ascii=False`: escaping would change the bytes and therefore the hash."""
    turkish = {**PAYLOAD, "note": "İstanbul"}
    assert canonical_booking_payload_hash(turkish) == canonical_booking_payload_hash(
        {**PAYLOAD, "note": "İstanbul"}
    )
    # ...and the escaped form is a genuinely different input.
    assert canonical_booking_payload_hash(turkish) != canonical_booking_payload_hash(
        {**PAYLOAD, "note": "\\u0130stanbul"}
    )


def test_rejects_values_that_cannot_be_canonicalised() -> None:
    """NaN/Infinity are not valid JSON; silently hashing them would bind consent to a non-payload."""
    with pytest.raises(ValueError):
        canonical_booking_payload_hash({**PAYLOAD, "weird": float("nan")})
