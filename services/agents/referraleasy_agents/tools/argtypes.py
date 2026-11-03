"""Constrained, classified types for every model-filled tool argument (WS2 S1, ADR-0044).

A tool parameter is something a model chooses. Two questions must have an answer for each one, and
both are answered here, as types, rather than in each handler:

* **What shape may it have?** Every string has a pattern, an enum or a length cap; every list has
  an item cap. ADK 2.12 emits these into the function declaration (`JSON_SCHEMA_FOR_FUNC_DECL` is
  on by default), so the model is shown `format: uuid`, `pattern`, `maxLength` and `maxItems`, and
  `ToolSpec.args_model()` enforces the same types on the way in. A path segment like
  `"../appointments/x/cancel"` is not a UUID and never reaches a URL.
* **Whose is it?** Every identifier carries an `IdClass`:

  - `OWNED` — names a record that belongs to someone (a patient's document). It must be resolved
    against the turn's actor before use; `resolver` names the resolver that does it (WS2 S3).
  - `PUBLIC` — reference data any caller may read (a hospital, a provider, a plan).
  - `VOCABULARY` — a closed set, enforced by the schema itself (a role, a workflow state).
  - `SERVICE_SUPPLIED_PENDING` — a value the model should not be choosing at all and that WS2 S4
    removes from the declaration (a booking id, an idempotency key). Classified so the gate can
    pass today, and named so the removal cannot be forgotten.

`tests/agents/test_tool_arg_contracts.py` walks every declaration and fails on an id-shaped
parameter with no class, an `OWNED` one with no registered resolver, or a free `str`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import Field

__all__ = [
    "RESOLVERS",
    "UUID_PATTERN",
    "Classified",
    "DateTimeText",
    "FailedRole",
    "HandoffReason",
    "IdClass",
    "IdempotencyKey",
    "LanguageTag",
    "LongText",
    "OwnedConstraintIds",
    "OwnedDocumentId",
    "OwnedDocumentIds",
    "OwnedEvidenceIds",
    "PostalCode",
    "PublicHospitalId",
    "PublicHospitalIds",
    "PublicPlanId",
    "PublicProviderId",
    "ReturnState",
    "ServiceBookingRequestId",
    "ServiceSlotId",
    "ShortText",
    "SpecialistRole",
    "Specialty",
    "VisitType",
    "Weekday",
    "Weekdays",
    "classification",
]


class IdClass(StrEnum):
    OWNED = "owned"
    PUBLIC = "public"
    VOCABULARY = "vocabulary"
    SERVICE_SUPPLIED_PENDING = "service_supplied_pending"


@dataclass(frozen=True, slots=True)
class Classified:
    """`Annotated` metadata: the parameter's id class and, for `OWNED`, its resolver."""

    id_class: IdClass
    resolver: str | None = None


#: The ownership resolvers WS2 S3 implements. An `OWNED` parameter must name one of these.
RESOLVERS: Final = frozenset({"document", "envelope_refs", "chosen_hospital"})

#: Lower-case canonical UUID text, which is what every id in the contracts serialises to.
UUID_PATTERN: Final = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: The most ids one call may name. A document package or a candidate shortlist is small; a list of
#: hundreds is an enumeration attempt.
MAX_IDS: Final = 20

_Uuid = Annotated[
    str, Field(pattern=UUID_PATTERN, max_length=36, json_schema_extra={"format": "uuid"})
]


_UuidList = Annotated[list[_Uuid], Field(max_length=MAX_IDS)]


# ── identifiers ─────────────────────────────────────────────────────────────────────────────

OwnedDocumentId = Annotated[_Uuid, Classified(IdClass.OWNED, "document")]
OwnedDocumentIds = Annotated[_UuidList, Classified(IdClass.OWNED, "document")]
OwnedEvidenceIds = Annotated[_UuidList, Classified(IdClass.OWNED, "envelope_refs")]
OwnedConstraintIds = Annotated[_UuidList, Classified(IdClass.OWNED, "envelope_refs")]

PublicHospitalId = Annotated[_Uuid, Classified(IdClass.PUBLIC)]
PublicHospitalIds = Annotated[_UuidList, Classified(IdClass.PUBLIC)]
PublicProviderId = Annotated[_Uuid, Classified(IdClass.PUBLIC)]
PublicPlanId = Annotated[_Uuid, Classified(IdClass.PUBLIC)]

#: WS2 S4 removes these from the declarations: the booking is the workflow's own, the slot is the
#: patient's recorded choice, and the key is the turn's.
ServiceBookingRequestId = Annotated[_Uuid, Classified(IdClass.SERVICE_SUPPLIED_PENDING)]
ServiceSlotId = Annotated[_Uuid, Classified(IdClass.SERVICE_SUPPLIED_PENDING)]
IdempotencyKey = Annotated[
    str, Field(min_length=8, max_length=128), Classified(IdClass.SERVICE_SUPPLIED_PENDING)
]

# ── closed vocabularies ─────────────────────────────────────────────────────────────────────

#: The six roles a coordinator may delegate to (never itself).
SpecialistRole = Annotated[
    Literal[
        "referral_intake",
        "coverage_network",
        "provider_matching",
        "referral_readiness",
        "scheduling",
        "recovery_handoff",
    ],
    Classified(IdClass.VOCABULARY),
]
FailedRole = Annotated[
    Literal[
        "care_coordinator",
        "referral_intake",
        "coverage_network",
        "provider_matching",
        "referral_readiness",
        "scheduling",
        "recovery_handoff",
    ],
    Classified(IdClass.VOCABULARY),
]
HandoffReason = Literal[
    "technical_outage",
    "no_match",
    "missing_document",
    "conflicting_sources",
    "prolonged_unknown_booking",
    "patient_requested",
    "no_reliable_recovery",
]
ReturnState = Annotated[
    str, Field(pattern=r"^[A-Z][A-Z_]{1,40}$", max_length=41), Classified(IdClass.VOCABULARY)
]
VisitType = Literal["first_visit", "return_visit"]
Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
Weekdays = Annotated[list[Weekday], Field(max_length=7)]

# ── bounded text ────────────────────────────────────────────────────────────────────────────

ShortText = Annotated[str, Field(min_length=1, max_length=500)]
#: A referral as received, which may be a JSON record serialised as text.
LongText = Annotated[str, Field(min_length=1, max_length=20_000)]
Specialty = Annotated[str, Field(min_length=1, max_length=80)]
PostalCode = Annotated[str, Field(pattern=r"^\d{5}$")]
LanguageTag = Annotated[str, Field(pattern=r"^[a-z]{2}(-[A-Za-z0-9]{2,8})*$", max_length=35)]
DateTimeText = Annotated[str, Field(max_length=40, json_schema_extra={"format": "date-time"})]


def classification(annotation: Any) -> Classified | None:
    """The `Classified` marker on `annotation`, looking through `Optional` and `Annotated` layers."""
    from typing import get_args, get_origin

    seen: list[Any] = [annotation]
    while seen:
        current = seen.pop()
        for meta in getattr(current, "__metadata__", ()):
            if isinstance(meta, Classified):
                return meta
        origin = get_origin(current)
        if origin is not None and origin is not Literal:
            seen.extend(get_args(current))
    return None
