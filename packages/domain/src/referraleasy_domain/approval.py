"""Approval binding: what the patient consented to, and how that is made unambiguous (§9).

An approval is valid only while it is bound to **patient + workflow + state_version + booking
payload hash + expiry**. Change any booking-relevant field and the hash changes, so the consent no
longer matches and the database refuses the booking — the invalidation is structural, not a
convention the application is asked to remember.

The database stores and compares the hash but does **not** compute it (`workflow_booking_payload_hash`
reads `workflow.payload ->> 'booking_payload_hash'`). Computing it is therefore this module's job,
and the canonical encoding below is a cross-boundary contract: if it ever changes, every hash
recorded against an existing approval becomes unmatched. That is the safe direction to fail, but it
is also why the encoding is pinned by tests.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Final

from pydantic import BaseModel

__all__ = [
    "CANONICAL_ENCODING",
    "PAYLOAD_HASH_KEY",
    "PAYLOAD_HASH_PATTERN",
    "canonical_booking_payload_hash",
]

#: Where the hash travels on `workflow.payload`. Mirrors `workflow_booking_payload_hash` in 007.
PAYLOAD_HASH_KEY: Final[str] = "booking_payload_hash"

#: The shape the database's `approval_payload_hash_hex` CHECK enforces.
PAYLOAD_HASH_PATTERN: Final[str] = r"^[a-f0-9]{64}$"

#: Description of the canonical form, for error messages and review.
CANONICAL_ENCODING: Final[str] = (
    "SHA-256 over UTF-8 JSON with sorted keys, no insignificant whitespace, "
    "non-ASCII characters unescaped, and null-valued keys omitted"
)


def _canonical_bytes(payload: BaseModel | Mapping[str, Any]) -> bytes:
    if isinstance(payload, BaseModel):
        # `mode="json"` normalises UUID/datetime/enum to their wire form, so the hash matches what
        # the service boundary actually sent and received rather than a Python repr.
        data: dict[str, Any] = payload.model_dump(mode="json", exclude_none=True)
    else:
        data = _strip_nulls(dict(payload))

    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _strip_nulls(value: Any) -> Any:
    """Drop null-valued keys recursively.

    `{"a": null}` and `{}` mean the same thing here — "this field was not given" — so they must
    hash the same way. Otherwise an explicit null would silently invalidate a consent the patient
    never actually changed.
    """
    if isinstance(value, dict):
        return {k: _strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_nulls(v) for v in value]
    return value


def canonical_booking_payload_hash(payload: BaseModel | Mapping[str, Any]) -> str:
    """Return the consent-binding hash of a booking payload.

    Deterministic and order-independent: two payloads are the same booking if and only if their
    hashes match.

    >>> canonical_booking_payload_hash({"b": 1, "a": 2}) == canonical_booking_payload_hash(
    ...     {"a": 2, "b": 1}
    ... )
    True
    """
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()
