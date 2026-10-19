"""Canonical JSON and payload hashing, matching the mock API byte for byte.

An approval binds to *this* payload and no other, and the binding is a hash. That
only holds if the two sides compute the same digest over the same bytes, so this
module implements the same rule as `services/mock-api/src/domain/ids.ts`:

* object keys sorted, with `undefined` (absent) keys dropped;
* `,` and `:` with no whitespace;
* strings escaped by the platform's own JSON writer, non-ASCII left as-is;
* arrays in order — an array's order is part of the payload's meaning.

Python's `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`
agrees with that rule for every JSON value the contracts can carry, which
`tests/agents/test_booking_approval.py` checks against a fixed vector rather than
by inspection.

Note the asymmetry the hashes exist for: the same payload hashes the same
regardless of key order, and **a different payload hashes differently even when
the change is one character**. That second property is what makes
"approval_payload_changed" detectable at all.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any
from uuid import NAMESPACE_URL, uuid5

__all__ = [
    "canonical_json",
    "deterministic_uuid",
    "id_text",
    "id_texts",
    "payload_hash",
]


def deterministic_uuid(kind: str, *parts: object) -> str:
    """A stable UUID for ``kind`` and ``parts``, on the URL namespace.

    The same input yields the same id in every process and every turn, which is
    what lets a constraint or an option keep one identity across the turns that
    discuss it. A random id would make a decision's citation point at a different
    object the next time the same preference was restated, which is worse than
    having no citation at all.

    This mirrors `deterministicUuid` in `services/mock-api/src/domain/ids.ts` in
    intent (stability), not in algorithm — the two id spaces are separate and
    nothing compares an id across them.
    """
    seed = ":".join(["referraleasy", kind, *(str(part) for part in parts)])
    return str(uuid5(NAMESPACE_URL, seed))


def id_text(value: Any) -> str:
    """The plain text of a contract identifier. **Never** use ``str()`` for one.

    Every id in `referraleasy_contracts.generated.common` is a pydantic
    ``RootModel`` (`EvidenceId`, `HospitalId`, `PlanId`, `ActionRef`, and the bare
    `Uuid` they all wrap), and a `RootModel`'s ``__str__`` is its repr:
    ``str(EvidenceId("5000…01"))`` is the literal text
    ``"root=Uuid(root=UUID('5000…01'))"``. That string is not an id. Passing it to
    `EvidenceId(...)` raises, and passing it to a URL builds a request for an
    object that does not exist — a failure that is silent until the wrong bytes
    reach a route.

    So the unwrapping is done once, here, and every call site that converts an id
    goes through this function. It walks ``.root`` until the value is no longer a
    wrapper (a `Uuid` inside an `EvidenceId` needs two steps) and returns the
    ``str`` of what is left — which for a `UUID` and for a plain string is the text
    a Fastify route expects.

    `tests/agents/test_canonical_ids.py` pins the behaviour, including the
    ``str()``-is-wrong case, so a future edit cannot quietly restore it.
    """
    inner = value
    while hasattr(inner, "root"):
        inner = inner.root
    return str(inner)


def id_texts(values: Any) -> list[str]:
    """:func:`id_text` over an iterable, as a list — for a field that carries many ids."""
    return [id_text(item) for item in values]


def canonical_json(payload: Any) -> str:
    """The one spelling of a payload, as JSON text."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def payload_hash(payload: Mapping[str, Any]) -> str:
    """The lowercase SHA-256 hex digest of :func:`canonical_json`."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
