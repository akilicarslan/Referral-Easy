"""Unit tests for canonical request hashing.

No database and no driver. The hash is a cross-boundary contract: it is what makes "the same
command, retried" recognisable, and once a hash is recorded against a `command_id`, anything that
changes the encoding silently turns every in-flight retry into a false conflict.
"""

from __future__ import annotations

import re
from uuid import uuid4

import pytest
from pydantic import BaseModel

from referraleasy_domain import (
    REQUEST_HASH_PATTERN,
    canonical_booking_payload_hash,
    canonical_request_hash,
)


class BookingBody(BaseModel):
    slot_id: str
    note: str | None = None
    patient_ref: str | None = None

    model_config = {"extra": "forbid"}


def test_the_hash_matches_the_shape_the_column_enforces() -> None:
    # `command_dedup_request_hash_hex` accepts nothing else, so producing anything else would mean
    # a caller had to know to fix the hash up before storing it.
    assert re.fullmatch(REQUEST_HASH_PATTERN, canonical_request_hash({"a": 1}))


def test_key_order_does_not_change_the_hash() -> None:
    assert canonical_request_hash({"b": 1, "a": 2}) == canonical_request_hash({"a": 2, "b": 1})


def test_different_bodies_hash_differently() -> None:
    assert canonical_request_hash({"a": 1}) != canonical_request_hash({"a": 2})


def test_an_explicit_null_matches_an_absent_key() -> None:
    """`{"a": 1, "b": None}` and `{"a": 1}` are the same request, so they must hash the same.

    Otherwise a client that sends an explicit `null` for an omitted optional field would be told it
    was making a *different* request, and a genuine retry would be refused as a conflict.
    """
    assert canonical_request_hash({"a": 1, "b": None}) == canonical_request_hash({"a": 1})


def test_nulls_are_dropped_recursively() -> None:
    left = {"outer": {"keep": 1, "drop": None}, "list": [{"a": None, "b": 2}]}
    right = {"outer": {"keep": 1}, "list": [{"b": 2}]}
    assert canonical_request_hash(left) == canonical_request_hash(right)


def test_a_model_is_hashed_as_its_wire_form() -> None:
    # The hash must match what the service boundary actually sent and received, not a Python repr.
    assert canonical_request_hash(BookingBody(slot_id="S-1")) == canonical_request_hash(
        {"slot_id": "S-1"}
    )


def test_it_uses_the_same_encoder_as_the_booking_payload_hash() -> None:
    """The drift guard.

    Two encoders would be two chances to disagree, and a disagreement shows up as a false conflict
    on a retry that was in fact identical. This asserts the two functions are one encoding under two
    names; if someone ever gives `canonical_request_hash` its own encoder, this fails.
    """
    payload = {"slot_id": "S-1", "note": None, "when": "2026-01-01T00:00:00Z"}
    assert canonical_request_hash(payload) == canonical_booking_payload_hash(payload)


def test_non_ascii_is_not_escaped_but_is_stable() -> None:
    assert canonical_request_hash({"name": "Ayşe"}) == canonical_request_hash({"name": "Ayşe"})
    assert canonical_request_hash({"name": "Ayşe"}) != canonical_request_hash({"name": "Ayse"})


def test_nan_is_refused_rather_than_encoded() -> None:
    # NaN has no JSON representation; allow_nan=False makes it an error instead of a payload that
    # hashes and then fails to parse on the other side.
    with pytest.raises(ValueError):
        canonical_request_hash({"amount": float("nan")})


def test_a_raw_dict_with_a_non_json_value_is_refused_not_coerced() -> None:
    """A bare dict is passed through, so it must already be JSON-shaped.

    `model_dump(mode="json")` normalises UUIDs and datetimes for models; a raw mapping gets no such
    treatment, so a UUID inside one fails loudly here rather than hashing something surprising.
    Callers passing structured data should pass a model.
    """
    with pytest.raises(TypeError):
        canonical_request_hash({"id": uuid4()})
