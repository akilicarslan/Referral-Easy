"""Authority: structured records outrank retrieved text, and the conflict stays visible.

Two rules, both of which exist because the alternative is a fluent, sourced, wrong answer:

**A structured record cannot be overridden by retrieved text.** If the coverage API says a
provider is out of network for a plan, no directory blurb makes them in network. The blurb is
prose written by whoever edited the directory; the coverage result is a record with evidence
ids, a freshness state and a disclaimer key. When they disagree the record wins — and the
disagreement is returned as a :class:`Conflict` rather than resolved silently, because "the API
says no and the directory says yes" is exactly the situation a human needs to see.

The fixture corpus contains this case on purpose: the Des Plaines Valley General Hospital blurb
asserts it "is in-network with every major plan", while the structured coverage result for that
plan, provider and hospital is `unknown` with `conflict: true`.

**The directory never asserts availability or booking confirmation.** Blurbs do it anyway
("confirmed by the scheduling desk the same business day"), because that is how directory copy
is written. Rather than editing the corpus to be well-behaved, retrieval strips assertions of
availability and booking out of grounding text and reports what it removed. The directory
describes; the booking API decides.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from referraleasy_rag.contracts import Row, load_coverage_results

#: Statuses that mean "the plan covers this", and their negation, as the contract spells them.
POSITIVE_STATUSES = frozenset({"in_network"})
NEGATIVE_STATUSES = frozenset({"out_of_network"})

#: Claims about network status, as prose writes them.
_NETWORK_CLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "negative",
        re.compile(
            r"\b(?:not\s+in[\s-]network|out[\s-]of[\s-]network|not\s+covered|"
            r"does\s+not\s+accept|no\s+longer\s+(?:in[\s-]network|accepts?|takes?)|"
            r"not\s+accepted)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "positive",
        re.compile(
            r"\b(?:in[\s-]network(?:\s+with)?|accepts?\s+(?:the\s+|every\s+|all\s+)?"
            r"(?:major\s+)?(?:insurance|plan|plans|coverage)|accepts?\s+insurance)\b",
            re.IGNORECASE,
        ),
    ),
)

#: Claims that a booking would be confirmed, or that a slot exists. The directory may describe
#: how a request is processed; it may not promise the outcome.
_AVAILABILITY_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:same[\s-]day|instant(?:ly)?\s+confirm\w*|confirmed\s+(?:by|within|the\s+same)|"
        r"confirms?\s+the\s+(?:appointment|booking|request)|we\s+can\s+confirm|"
        r"appointments?\s+(?:are|is)\s+confirmed|book\s+immediately|"
        r"slots?\s+(?:are\s+)?available|walk[\s-]ins?\s+(?:are\s+)?(?:welcome|accepted)|"
        r"no\s+wait(?:ing)?\s+list|guaranteed\s+(?:slot|appointment))\b",
        re.IGNORECASE,
    ),
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class TextClaim:
    """An assertion found in retrieved text, kept with the sentence it came from."""

    kind: str
    polarity: str
    text: str
    chunk_id: str
    entity_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "polarity": self.polarity,
            "text": self.text,
            "chunk_id": self.chunk_id,
            "entity_id": self.entity_id,
        }


@dataclass(frozen=True, slots=True)
class Conflict:
    """A structured value and a retrieved claim that disagree. Both are kept; the record wins."""

    field_name: str
    structured_value: Any
    claimed_value: str
    claim: TextClaim

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field_name,
            "structured_value": self.structured_value,
            "claimed_value": self.claimed_value,
            "claim": self.claim.to_dict(),
            "resolution": "structured record wins; both sides reported",
        }


@dataclass(frozen=True, slots=True)
class CoverageResolution:
    """The authoritative coverage facts for one (plan, provider, hospital), plus the conflicts."""

    plan_id: str | None
    provider_id: str | None
    hospital_id: str | None
    authoritative: Mapping[str, Any] = field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()
    conflicting_evidence_ids: tuple[str, ...] = ()
    freshness_state: str = "unknown"
    disclaimer_key: str = ""
    conflicts: tuple[Conflict, ...] = ()
    claims: tuple[TextClaim, ...] = ()

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts)

    @property
    def status(self) -> str:
        value = self.authoritative.get("status")
        return str(value) if value is not None else "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "provider_id": self.provider_id,
            "hospital_id": self.hospital_id,
            "authoritative": dict(self.authoritative),
            "evidence_ids": list(self.evidence_ids),
            "freshness_state": self.freshness_state,
            "disclaimer_key": self.disclaimer_key,
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
        }


@dataclass(frozen=True, slots=True)
class ClaimScan:
    """Grounding text with availability assertions removed, and a record of what was removed."""

    text: str
    removed: tuple[TextClaim, ...] = ()

    @property
    def had_claims(self) -> bool:
        return bool(self.removed)


class CoverageLookup:
    """Coverage results by (plan, provider, hospital). The structured record this package trusts."""

    def __init__(self, results: Sequence[Row]) -> None:
        self._results = {self._key(row): row for row in results}
        self._by_provider: dict[str, list[Row]] = {}
        for row in results:
            provider_id = _string(row.get("provider_id"))
            if provider_id:
                self._by_provider.setdefault(provider_id, []).append(row)

    @staticmethod
    def _key(row: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
        return (
            _string(row.get("plan_id")),
            _string(row.get("provider_id")),
            _string(row.get("hospital_id")),
        )

    @classmethod
    def from_fixtures(cls, fixtures_dir: Path) -> CoverageLookup:
        return cls(load_coverage_results(fixtures_dir))

    def get(
        self, *, plan_id: str | None, provider_id: str | None, hospital_id: str | None
    ) -> Row | None:
        """Look the triple up, then relax the hospital. Never guesses a different provider."""
        if provider_id is None:
            return None
        for key in ((plan_id, provider_id, hospital_id), (plan_id, provider_id, None)):
            found = self._results.get(key)
            if found is not None:
                return found
        return None

    def for_provider(self, provider_id: str) -> list[Row]:
        return list(self._by_provider.get(provider_id, ()))

    def __len__(self) -> int:
        return len(self._results)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def scan_text_claims(text: str, *, chunk_id: str = "", entity_id: str = "") -> list[TextClaim]:
    """Find network-status assertions in one piece of retrieved text."""
    claims: list[TextClaim] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        for polarity, pattern in _NETWORK_CLAIM_PATTERNS:
            if pattern.search(sentence):
                claims.append(
                    TextClaim(
                        kind="network_status",
                        polarity=polarity,
                        text=sentence.strip(),
                        chunk_id=chunk_id,
                        entity_id=entity_id,
                    )
                )
                break
    return claims


def strip_availability_claims(text: str, *, chunk_id: str = "") -> ClaimScan:
    """Remove sentences that assert availability or a confirmed booking.

    Applied to grounding text only, never to the index: the corpus is indexed as written, and the
    sanitising happens at the moment text would be handed to a model as evidence.
    """
    kept: list[str] = []
    removed: list[TextClaim] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        if any(pattern.search(sentence) for pattern in _AVAILABILITY_CLAIM_PATTERNS):
            removed.append(
                TextClaim(
                    kind="availability_or_booking",
                    polarity="asserted",
                    text=sentence.strip(),
                    chunk_id=chunk_id,
                )
            )
            continue
        kept.append(sentence)
    return ClaimScan(text=" ".join(kept).strip(), removed=tuple(removed))


def resolve_coverage(
    record: Mapping[str, Any] | None,
    retrieved: Iterable[tuple[str, str, str]] = (),
) -> CoverageResolution:
    """Merge a structured coverage record with retrieved text, structured side winning.

    ``retrieved`` is an iterable of ``(chunk_id, entity_id, text)``.
    """
    if record is None:
        return CoverageResolution(
            plan_id=None,
            provider_id=None,
            hospital_id=None,
            authoritative={},
            claims=(),
            conflicts=(),
        )

    authoritative: dict[str, Any] = {}
    for name in (
        "status",
        "in_network_for_plan",
        "accepts_insurance",
        "referral_required",
        "prior_auth_required",
        "conflict",
    ):
        if name in record:
            authoritative[name] = record[name]

    freshness = record.get("freshness")
    freshness_state = "unknown"
    if isinstance(freshness, Mapping):
        state = freshness.get("state")
        if isinstance(state, str):
            freshness_state = state

    claims: list[TextClaim] = []
    conflicts: list[Conflict] = []
    for chunk_id, entity_id, text in retrieved:
        for claim in scan_text_claims(text, chunk_id=chunk_id, entity_id=entity_id):
            claims.append(claim)
            conflict = _conflict_for(record, claim)
            if conflict is not None:
                conflicts.append(conflict)

    return CoverageResolution(
        plan_id=_string(record.get("plan_id")),
        provider_id=_string(record.get("provider_id")),
        hospital_id=_string(record.get("hospital_id")),
        authoritative=authoritative,
        evidence_ids=tuple(_string_list(record.get("evidence_ids"))),
        conflicting_evidence_ids=tuple(_string_list(record.get("conflicting_evidence_ids"))),
        freshness_state=freshness_state,
        disclaimer_key=str(record.get("disclaimer_key", "")),
        conflicts=tuple(conflicts),
        claims=tuple(claims),
    )


def _conflict_for(record: Mapping[str, Any], claim: TextClaim) -> Conflict | None:
    """Is this claim contradicted by the structured record?

    A claim conflicts when the record says the opposite *or* when the record does not support
    it — an `unknown` status means the platform does not know, so a directory blurb that asserts
    network membership is asserting more than the platform can back.
    """
    if claim.kind != "network_status":
        return None
    status = record.get("status")
    if not isinstance(status, str):
        status = "unknown"
    if claim.polarity == "positive":
        supported = status in POSITIVE_STATUSES
        claimed = "in_network"
    else:
        supported = status in NEGATIVE_STATUSES
        claimed = "out_of_network"
    if supported:
        return None
    return Conflict(
        field_name="status",
        structured_value=status,
        claimed_value=claimed,
        claim=claim,
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


__all__ = [
    "NEGATIVE_STATUSES",
    "POSITIVE_STATUSES",
    "ClaimScan",
    "Conflict",
    "CoverageLookup",
    "CoverageResolution",
    "TextClaim",
    "resolve_coverage",
    "scan_text_claims",
    "strip_availability_claims",
]
