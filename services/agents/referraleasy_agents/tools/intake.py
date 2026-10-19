"""Referral Intake's three tools (§3).

`validate_referral`, `resolve_location`, `patient_preferences` — and the two §3
forbidden capabilities they must not grow:

* **"cannot diagnose"** — nothing here interprets a clinical statement. The draft
  carries the referral's own `clinical_reason_text` verbatim and derives nothing
  from it.
* **"cannot determine a specialty absent from the referral"** — `specialty` is set
  only when the referral states it, in its own words. There is no default, no
  inference from the clinical reason, and no lookup that could supply one:
  :func:`parse_referral_fields` reads fields and never produces one.
* **"cannot invent a field value"** — every draft field is a value found in the
  referral text, and :func:`refuse_invented_field` is the boundary as a call.

"Missing" is a first-class answer here rather than a failure. A referral with no
specialty is not a broken referral; it is a referral with a question attached, and
§3 makes naming that question this agent's decision.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from referraleasy_agents.canonical import deterministic_uuid
from referraleasy_agents.errors import UnknownDataError
from referraleasy_agents.tools.call import ToolCall, require_patient_id, require_workflow_id
from referraleasy_contracts.generated.common import (
    ConstraintField,
    ConstraintKind,
    VisitType,
)
from referraleasy_contracts.generated.patient import (
    Constraint,
    ConstraintSource,
    PatientPreferences,
)

__all__ = [
    "REQUIRED_INTAKE_FIELDS",
    "injection_detected",
    "parse_referral_fields",
    "patient_preferences",
    "refuse_invented_field",
    "resolve_location",
    "validate_referral",
]

#: The fields `ReferralIntake` requires. Named here so "what is missing" is a list
#: of contract fields and not a feeling.
REQUIRED_INTAKE_FIELDS: Final = ("referral_id", "patient_id", "specialty", "issued_at")

#: Spellings a referral uses for the fields above. A synonym table, not an
#: interpretation: every entry maps to a field the contract already has.
_FIELD_ALIASES: Final[Mapping[str, str]] = {
    "referral_id": "referral_id",
    "referralid": "referral_id",
    "referral_number": "referral_id",
    "patient_id": "patient_id",
    "patientid": "patient_id",
    "mrn": "patient_id",
    "specialty": "specialty",
    "speciality": "specialty",
    "referred_by": "referred_by_provider",
    "referring_provider": "referred_by_provider",
    "referred_by_provider": "referred_by_provider",
    "issued_at": "issued_at",
    "issued": "issued_at",
    "date": "issued_at",
    "visit_type": "visit_type",
    "reason": "clinical_reason_text",
    "clinical_reason": "clinical_reason_text",
    "clinical_reason_text": "clinical_reason_text",
    "notes": "clinical_reason_text",
}

#: Text that reads like an instruction to the reader. §9: document and referral
#: text is DATA. Detected and reported, never obeyed — and the detection is
#: deliberately crude, because a clever detector is a second interpretation layer
#: for an attack this service does not defend against by parsing.
_INJECTION_MARKERS: Final = (
    "ignore previous",
    "ignore all previous",
    "disregard the above",
    "system:",
    "assistant:",
    "you must book",
    "override the policy",
    "new instructions",
)

#: Visit types a referral may state. Anything else is reported, not coerced.
_VISIT_TYPES: Final = {member.value for member in VisitType}


def parse_referral_fields(referral_text: str) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Read ``referral_text`` into fields, and report repeated values as conflicts.

    Two shapes are accepted because two shapes exist in practice: a JSON record
    (what an EHR sends) and labelled text (what a fax-and-typing workflow
    produces). Both paths return the same thing — values found under contract
    field names, plus a map of field to every value seen for it, so a disagreement
    is reportable rather than silently resolved to the last one seen.
    """
    found: dict[str, Any] = {}
    seen: dict[str, list[str]] = {}

    def note(field: str, value: Any) -> None:
        text = str(value).strip()
        if not text:
            return
        seen.setdefault(field, []).append(text)
        if field == "clinical_reason_text":
            found.setdefault(field, []).append(text)
            return
        found.setdefault(field, text)

    text = referral_text.strip()
    parsed: Any = None
    if text.startswith(("{", "[")):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
    if isinstance(parsed, Mapping):
        for key, value in parsed.items():
            field = _FIELD_ALIASES.get(str(key).strip().lower().replace(" ", "_"))
            if field is None:
                continue
            if isinstance(value, Sequence) and not isinstance(value, str):
                for item in value:
                    note(field, item)
            else:
                note(field, value)
        return found, seen

    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z_ ]{2,30})\s*[:=]\s*(.+?)\s*$", line)
        if match is None:
            continue
        field = _FIELD_ALIASES.get(match.group(1).strip().lower().replace(" ", "_"))
        if field is None:
            continue
        note(field, match.group(2))
    return found, seen


def injection_detected(*texts: str) -> bool:
    """Whether any text reads like an instruction to the reader (§9)."""
    lowered = " ".join(texts).lower()
    return any(marker in lowered for marker in _INJECTION_MARKERS)


def refuse_invented_field(referral_text: str, *, field: str, value: Any) -> None:
    """Refuse a value for a referral field that the referral does not state (§3).

    §3's third forbidden capability for this agent is "inventing a field value to
    make the intake look complete", and this is that boundary as a call: the
    referral text is the only authority on its own fields, so a value that does not
    occur in the text is refused rather than recorded. ``None`` is not an invention
    — it is the honest answer for a field nobody read.

    This function has **no production caller**, and that is stated rather than
    hidden: no production path substitutes a value, which is the point of it. It
    exists so the boundary is callable — `forbidden.py` and
    `test_forbidden_capabilities.py` call it — because a boundary that lives only in
    a prompt is a preference. The same pattern, for the same reason, is
    `tools.coverage.refuse_unstated_rule`.
    """
    if value is None:
        return
    haystack = " ".join(referral_text.casefold().split())
    needle = " ".join(str(value).casefold().split())
    if needle and needle not in haystack:
        raise UnknownDataError(
            f"the referral does not state {field}={value!r}; supplying it would make the intake look "
            "complete on a fact nobody read. Report the field as missing and ask for it (§3)."
        )


async def validate_referral(
    call: ToolCall,
    *,
    referral_text: str,
    document_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Read a referral, name what is missing, and refuse to fill any of it in."""
    fields, seen = parse_referral_fields(referral_text)
    missing = [field for field in REQUIRED_INTAKE_FIELDS if field not in fields]

    contradictions = [
        {"field": field, "values": sorted(set(values)), "evidence_ids": []}
        for field, values in sorted(seen.items())
        if len(set(values)) > 1
    ]

    visit_type = fields.get("visit_type")
    if visit_type is not None and str(visit_type) not in _VISIT_TYPES:
        contradictions.append(
            {
                "field": "visit_type",
                "values": [str(visit_type)],
                "evidence_ids": [],
            }
        )
        visit_type = None

    draft: dict[str, Any] = {
        "referral_id": fields.get("referral_id"),
        "patient_id": fields.get("patient_id"),
        # Only ever the referral's own word for it. `_parse_referral_fields` cannot
        # produce a specialty from anything else, which is how §3's second forbidden
        # capability is structural rather than promised.
        "specialty": fields.get("specialty"),
        "referred_by_provider": fields.get("referred_by_provider"),
        "issued_at": fields.get("issued_at"),
        "visit_type": visit_type,
        "clinical_reason_text": ("\n".join(fields.get("clinical_reason_text") or []) or None),
        "document_ids": list(document_ids or []) or None,
        "injection_attempt_detected": injection_detected(referral_text),
    }

    completed = None
    if not missing and not contradictions:
        completed = {
            "referral_id": draft["referral_id"],
            "patient_id": draft["patient_id"],
            "specialty": draft["specialty"],
            "referred_by_provider": draft["referred_by_provider"],
            "issued_at": draft["issued_at"],
            "visit_type": draft["visit_type"],
            "clinical_reason_text": draft["clinical_reason_text"],
            "document_ids": draft["document_ids"],
            "synthetic": True,
        }

    return {
        "draft": draft,
        "missing_fields": missing,
        "contradictions": contradictions,
        # The referral text is the caller's own input, not a retrieval: it has no
        # evidence id, and inventing one would make the citation meaningless. The
        # envelope's own `evidence_refs` cover it.
        "evidence_ids": [],
        "completed_intake": completed,
    }


async def resolve_location(call: ToolCall, *, postal_code: str) -> dict[str, Any]:
    """Resolve a postal code to coordinates, without the address entering context.

    §6 keeps identity out of a specialist's context; a search origin is not
    identity. The tool returns the location *and* the coordinates, because the
    caller that needs a `GeoPoint` should not have to hold an address to get one.
    """
    body = await call.api.get("/locations", budget=call.budget, lease=call.lease)
    rows = body.get("locations")
    if not isinstance(rows, list):
        raise UnknownDataError(
            "GET /locations answered without a `locations` list; the response shape is not the "
            "one this tool can read, so no origin can be established."
        )
    wanted = postal_code.strip()
    matches = [row for row in rows if str(row.get("postal_code", "")).strip() == wanted]
    if not matches:
        raise UnknownDataError(
            f"no location is registered for postal code {wanted!r}. The referral cannot be "
            "searched from an origin the directory does not know."
        )
    location = matches[0]
    return {
        "location": {
            "location_id": location.get("location_id"),
            "city": location.get("city"),
            "state": location.get("state"),
            "postal_code": location.get("postal_code"),
            "geo": location.get("geo"),
            "timezone": location.get("timezone"),
        },
        "origin": location.get("geo"),
        # `Location` carries no `evidence_id` in the frozen contract (see
        # `packages/contracts/schemas/directory.json`). The origin is a query input
        # rather than a claim about the patient, so it is cited by the location id
        # and not presented as retrieved evidence. Recorded as a gap.
        "evidence_ids": [],
    }


#: Fields whose source is the system, not the patient (§2.1): they are mandatory
#: and cannot be relaxed by anyone, so a caller cannot mark them soft.
_SYSTEM_MANDATORY: Final = frozenset(
    {ConstraintField.specialty, ConstraintField.visit_type, ConstraintField.insurance_plan}
)

#: Keys the caller may use for a constraint field, mapped to the contract's own enum.
_PREFERENCE_KEYS: Final[Mapping[str, ConstraintField]] = {
    "specialty": ConstraintField.specialty,
    "provider_language": ConstraintField.provider_language,
    "language": ConstraintField.provider_language,
    "preferred_language": ConstraintField.provider_language,
    "preferred_day": ConstraintField.preferred_day,
    "day": ConstraintField.preferred_day,
    "preferred_time_of_day": ConstraintField.preferred_time_of_day,
    "time_of_day": ConstraintField.preferred_time_of_day,
    "max_distance_miles": ConstraintField.max_distance_miles,
    "distance": ConstraintField.max_distance_miles,
    "accepts_new_patients": ConstraintField.accepts_new_patients,
    "accessibility": ConstraintField.accessibility,
    "insurance_plan": ConstraintField.insurance_plan,
    "plan_id": ConstraintField.insurance_plan,
    "visit_type": ConstraintField.visit_type,
}


async def patient_preferences(
    call: ToolCall,
    *,
    stated: Mapping[str, Any],
    search_origin: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify stated preferences into hard constraints and soft preferences.

    The classification rule is deliberately the caller's, not this function's
    guess: a preference may be stated as a bare value (soft, unless its field is
    system-mandatory) or as ``{"value": ..., "hard": true}``. §2.1's mandatory
    fields are hard whatever the caller says — the field list decides, so a caller
    cannot downgrade "specialty" to a soft preference and let a later step relax it.

    `relaxable` follows §2.1's own sentence: `False` for mandatory fields "regardless
    of kind". A patient-stated hard constraint is still `relaxable: True` in the
    narrow sense that the patient may change it — through a `ConstraintChange` with
    an explicit decision, never by this system on their behalf.

    **Whose preferences these are is not a tool argument.** The workflow id comes
    from the task envelope and the patient id from this role's own validated input
    (`require_workflow_id`, `require_patient_id`), because the constraint ids are
    minted from the pair: a model that could name them could mint a constraint
    attributed to another workflow, and a constraint id is what a later decision
    cites when it relaxes one.
    """
    workflow_id = require_workflow_id(call)
    patient_id = require_patient_id(call)
    constraints: list[dict[str, Any]] = []
    unknown_keys: list[str] = []
    for key, raw in sorted(stated.items()):
        field = _PREFERENCE_KEYS.get(str(key).strip().lower())
        if field is None:
            unknown_keys.append(str(key))
            continue
        hard_flag = False
        value: Any = raw
        if isinstance(raw, Mapping):
            value = raw.get("value")
            hard_flag = bool(raw.get("hard", False))
        if value is None:
            continue
        mandatory = field in _SYSTEM_MANDATORY
        kind = ConstraintKind.hard if (mandatory or hard_flag) else ConstraintKind.soft
        source = ConstraintSource.system_mandatory if mandatory else ConstraintSource.patient_stated
        constraint = Constraint(
            constraint_id=deterministic_uuid("constraint", workflow_id, field.value, value),
            field=field,
            kind=kind,
            value=value,
            source=source,
            relaxable=not mandatory,
            recorded_at=datetime.now(UTC).isoformat(),
        )
        constraints.append(constraint.model_dump(mode="json"))

    if unknown_keys:
        raise UnknownDataError(
            f"preference keys {sorted(unknown_keys)} do not name a constraint field; the fields are "
            f"{sorted(field.value for field in ConstraintField)}. An unrecognised preference is not "
            "recorded as a soft one — it would silently never be applied."
        )

    preferences = PatientPreferences(
        workflow_id=workflow_id,
        patient_id=patient_id,
        constraints=[Constraint.model_validate(item) for item in constraints],
        search_origin=search_origin,
        version=0,
    )
    return {
        "preferences": preferences.model_dump(mode="json"),
        "hard_constraint_ids": [
            item["constraint_id"]
            for item in constraints
            if item["kind"] == ConstraintKind.hard.value
        ],
        # Stated by the patient and normalised here: nothing was retrieved, so
        # there is no retrieval to cite. The preferences record is the source.
        "evidence_ids": [],
    }
