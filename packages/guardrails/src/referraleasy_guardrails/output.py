# ruff: noqa: RUF001, RUF002  # Turkish patterns and the §9 sentence are quoted verbatim
"""The output guardrail: what must be true before a card reaches a patient (§9, §2.5).

§9 states it as a list — *"Output guardrail: schema, mevcut entity ID, doğrulanmış saat, kaynaklı
telefon/adres ve gerçek booking state"* — with two prohibitions attached: no coverage or payment
guarantee, and no clinical language. Each item is a check in this module, and each check is written so
that a test can delete it and watch the suite fail.

Why an output guardrail is not optional in a system like this one: the input layers judge a *message*,
and everything after them is generation. A card is what a patient reads and acts on, so the claims it
makes have to be tied to records — an entity id that exists, a time that matches the slot record, a
phone number that appears in a retrieved source, a booking state that is the booking's own. The
failure this prevents is not "the model was rude", it is "the patient was told an appointment was
confirmed when the scheduler had only accepted a request".

Two of the checks carry an invariant the repository states elsewhere, and it is worth naming:
``REQUESTED`` and ``PENDING_REVIEW`` are never presented as confirmed, and HTTP 200 never implies
``CONFIRMED`` (§2.5, and the README's list of release blockers). The check reads that from the
booking's own record rather than from the card's wording, because wording can be reworded.

**Where this runs.** Not here. This module is a pure function over a component and the records it
claims to be about; :class:`SourceOracle` is the port the caller implements against whatever it can
actually query. Wiring it in front of the SSE surface belongs to the gateway, which is where the
components are built.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from pydantic import ValidationError

from referraleasy_contracts.generated.common import BookingState
from referraleasy_contracts.generated.sse import ComponentInstance

__all__ = [
    "CLINICAL_PATTERNS",
    "ENTITY_ID_FIELDS",
    "GUARANTEE_PATTERNS",
    "TIME_FIELDS",
    "OutputCheckResult",
    "OutputGuardrail",
    "OutputRejected",
    "OutputViolation",
    "SourceOracle",
]

#: Prop fields whose value is an entity id that must exist before the card is shown.
ENTITY_ID_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "provider_id",
        "hospital_id",
        "slot_id",
        "booking_id",
        "plan_id",
        "document_id",
        "provider_ids",
        "hospital_ids",
    }
)

#: Prop fields whose value is a time that must be verifiable against the record behind it.
TIME_FIELDS: Final[frozenset[str]] = frozenset(
    {"slot_time", "start_at", "starts_at", "appointment_time", "appointment_start"}
)

#: Which prop carries the booking's own state, if any.
BOOKING_STATE_FIELDS: Final[tuple[str, ...]] = ("booking_status", "booking_state", "status")

#: Phrases that promise an outcome the project cannot promise (§10, §9): the demo has no payment
#: integration and no payer contract, and a coverage result is always "as of the last check".
GUARANTEE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"\b(guarantee\w*|guaranteed|will be covered|fully covered|100% covered|covered in full|"
        r"no cost to you|free of charge|we will pay)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(garanti\w*|tamamen karşılan\w*|ücret alınmaz|masraf çıkmaz|kesinlikle öden\w*)\b",
        re.IGNORECASE,
    ),
)

#: Clinical phrasing. The demo gives no medical advice and no diagnosis (§1: it makes no clinical
#: decision). The patterns are deliberately blunt: refusing to reword a card costs a rewrite, and
#: letting a dosage through costs the project's only real claim — that its claims are true.
CLINICAL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"\b(diagnos\w*|prescrib\w*|prescription|dosage|milligram\w*|mg\b|take \d+ ?(?:mg|tablet)|"
        r"stop taking|you should take|increase your dose|side effects?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(teşhis\w*|reçete\w*|doz\w*|ilaç kullan\w*|günde \d+ kez)\b",
        re.IGNORECASE,
    ),
)

#: Props that only exist if someone is trying to put advice in the payload under a helpful name.
CLINICAL_FIELDS: Final[frozenset[str]] = frozenset(
    {"medical_advice", "clinical_note", "diagnosis", "treatment", "dosage"}
)

_PHONE: Final = re.compile(r"(?<!\d)(?:\+?\d[\d \-().]{7,}\d)(?!\d)")
_ADDRESS: Final = re.compile(
    r"\b\d{1,5}\s+[A-ZÇĞİÖŞÜ][\wçğıöşü.']*(?:\s+[A-ZÇĞİÖŞÜ]?[\wçğıöşü.']+){0,4}\s+"
    r"(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Sokak|Sok|Cadde|Cad|"
    r"Caddesi|Mahalle|Mah)\.?\b",
    re.IGNORECASE,
)

#: A date or an instant. ``2026-11-02`` is a valid *match* for the phone pattern — four digits, a
#: hyphen, two digits, a hyphen, two digits — so a card stating an appointment date was refused as
#: carrying an unsourced phone number that was not a phone number. Found by a test rather than by
#: reading; the timestamp case is covered by :func:`_parse_instant` and this covers the bare date.
_DATE_LIKE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Coverage claims that must be evidenced before they are shown (§10: structured records win over
#: retrieved text, and a plan status is never asserted without the record that says so).
_COVERAGE_FIELDS: Final[frozenset[str]] = frozenset(
    {"in_network_for_plan", "accepts_insurance", "coverage_status", "coverage"}
)


class SourceOracle(Protocol):
    """The records a card's claims are checked against. Implemented by the caller, not here.

    Deliberately three questions. An oracle that could answer anything would let this module grow a
    query for every field, which is the coupling the port exists to avoid — and in a test, three
    methods is a stub you can write in six lines.
    """

    def entity_exists(self, entity_id: str) -> bool: ...

    def slot_time(self, slot_id: str) -> str | None:
        """The slot's own start time as the scheduler recorded it, or None if unknown."""

    def booking_state(self, booking_id: str) -> str | None:
        """The booking's state, or None if the booking cannot be read."""


@dataclass(frozen=True, slots=True)
class OutputViolation:
    """One reason a card must not be shown. There is no advisory severity: a violation is a refusal."""

    code: str
    detail: str

    def as_dict(self) -> Mapping[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class OutputCheckResult:
    """What the guardrail looked at, and what it found. ``checked`` is the anti-vacuity field."""

    ok: bool
    violations: tuple[OutputViolation, ...]
    checked: tuple[str, ...]

    def as_dict(self) -> Mapping[str, object]:
        return {
            "ok": self.ok,
            "checked": list(self.checked),
            "violations": [violation.as_dict() for violation in self.violations],
        }


class OutputRejected(RuntimeError):
    """A card that failed the output guardrail. The message lists every violation, not the first."""

    def __init__(self, result: OutputCheckResult) -> None:
        super().__init__(
            "the output guardrail refused a card:\n  - "
            + "\n  - ".join(f"{item.code}: {item.detail}" for item in result.violations)
        )
        self.result = result


class OutputGuardrail:
    """§9's output guardrail, as one pure check over a card and the records behind it.

    ``sources`` is the retrieved material for the turn — the snippets and tool results the card is
    allowed to have taken a phone number or an address from. It is passed per call rather than held,
    because the sources differ per turn and a guardrail holding yesterday's would pass a number it
    never saw.
    """

    def __init__(self, oracle: SourceOracle) -> None:
        self._oracle = oracle

    def check(
        self,
        component: Mapping[str, Any] | ComponentInstance,
        *,
        sources: Sequence[str] = (),
    ) -> OutputCheckResult:
        """Run every check and return all the findings. Never stops at the first one.

        Reporting only the first violation would make the second one's existence unknown, and a card
        that failed for three reasons has three edits to make.
        """
        checked: list[str] = []
        violations: list[OutputViolation] = []

        raw: Mapping[str, Any] = (
            component.model_dump(mode="json")
            if isinstance(component, ComponentInstance)
            else dict(component)
        )

        checked.append("schema")
        violations.extend(self._schema(raw))
        if violations and violations[0].code == "schema_invalid":
            # Without a parsed envelope there are no props to check and guessing at them would
            # produce findings about a card that does not exist.
            return OutputCheckResult(ok=False, violations=tuple(violations), checked=tuple(checked))

        props = raw.get("props") or {}
        assert isinstance(props, Mapping)  # narrowed by the schema check above

        checked.append("entity_ids")
        violations.extend(self._entities(props))
        checked.append("times")
        violations.extend(self._times(props))
        checked.append("sourced_contacts")
        violations.extend(self._contacts(props, sources))
        checked.append("booking_state")
        violations.extend(self._booking_state(props))
        checked.append("coverage")
        violations.extend(self._coverage(props))
        checked.append("clinical_language")
        violations.extend(self._clinical(props))

        return OutputCheckResult(
            ok=not violations, violations=tuple(violations), checked=tuple(checked)
        )

    def check_or_raise(
        self,
        component: Mapping[str, Any] | ComponentInstance,
        *,
        sources: Sequence[str] = (),
    ) -> OutputCheckResult:
        result = self.check(component, sources=sources)
        if not result.ok:
            raise OutputRejected(result)
        return result

    # -- the checks ---------------------------------------------------------

    def _schema(self, raw: Mapping[str, Any]) -> list[OutputViolation]:
        """The envelope, validated against the contract rather than against a look-alike.

        The *props* are component-specific and are validated by the card catalog that builds them
        (required keys per state); what this check adds is that the envelope the client receives is
        the contract's shape — a known component, a uuid instance, an integer state version and no
        stray keys — so a component that would break the renderer never leaves the server.
        """
        try:
            ComponentInstance.model_validate(raw)
        except ValidationError as error:
            first = error.errors()[0]
            location = ".".join(str(part) for part in first.get("loc", ()))
            return [
                OutputViolation(
                    code="schema_invalid",
                    detail=f"{location or '<root>'}: {first.get('msg', 'invalid')}",
                )
            ]
        return []

    def _entities(self, props: Mapping[str, Any]) -> list[OutputViolation]:
        """Every referenced entity id must exist. An id that does not is a card about nothing."""
        violations: list[OutputViolation] = []
        for field_name, value in _walk(props):
            if field_name not in ENTITY_ID_FIELDS:
                continue
            for entity_id in _as_ids(value):
                if not self._oracle.entity_exists(entity_id):
                    violations.append(
                        OutputViolation(
                            code="unknown_entity",
                            detail=f"{field_name}={entity_id!r} is not a record the caller can read",
                        )
                    )
        return violations

    def _times(self, props: Mapping[str, Any]) -> list[OutputViolation]:
        """A shown time must be a stated instant and must agree with the slot it claims to be.

        Two different failures, and both matter: a naive timestamp is a time that means different
        things in different places, and a time that disagrees with the slot record is a wrong
        appointment. The second is checked against the *record*, not against another card.
        """
        violations: list[OutputViolation] = []
        slot_ids = _as_ids(props.get("slot_id"))
        for field_name, value in _walk(props):
            if field_name not in TIME_FIELDS or not isinstance(value, str):
                continue
            parsed = _parse_instant(value)
            if parsed is None:
                violations.append(
                    OutputViolation(
                        code="unverified_time",
                        detail=(
                            f"{field_name}={value!r} is not an ISO 8601 timestamp with a UTC offset; "
                            "a time that does not say where it is has not been verified"
                        ),
                    )
                )
                continue
            for slot_id in slot_ids:
                recorded = self._oracle.slot_time(slot_id)
                recorded_instant = _parse_instant(recorded) if recorded else None
                if recorded_instant is None:
                    violations.append(
                        OutputViolation(
                            code="unverified_time",
                            detail=f"slot {slot_id!r} has no readable time to verify {field_name} against",
                        )
                    )
                elif recorded_instant != parsed:
                    violations.append(
                        OutputViolation(
                            code="unverified_time",
                            detail=(
                                f"{field_name}={value!r} does not match the slot record for "
                                f"{slot_id!r} ({recorded!r})"
                            ),
                        )
                    )
        return violations

    def _contacts(self, props: Mapping[str, Any], sources: Sequence[str]) -> list[OutputViolation]:
        """Phone numbers and street addresses must come from the material the turn retrieved (§10).

        A contact detail that appears nowhere in the sources was invented by the step that wrote the
        card. That is the single most damaging fabrication this system can produce, because a patient
        will call it.
        """
        haystack = "\n".join(sources)
        haystack_digits = re.sub(r"\D", "", haystack)
        haystack_folded = haystack.casefold()
        violations: list[OutputViolation] = []
        seen: set[str] = set()
        for field_name, value in _walk(props):
            if not isinstance(value, str) or value in seen:
                continue
            seen.add(value)
            if (
                field_name in TIME_FIELDS
                or _parse_instant(value)
                or _DATE_LIKE.match(value.strip())
            ):
                # A time is not a contact detail, and it is the *other* check's business. ISO instants
                # and dates have the shape of a hyphenated number, so without this a card that stated
                # a time was refused for an unsourced phone number it never carried.
                continue
            for phone in _PHONE.findall(value):
                digits = re.sub(r"\D", "", phone)
                if len(digits) < 7:
                    continue
                if not _sourced_number(digits, haystack_digits):
                    violations.append(
                        OutputViolation(
                            code="unsourced_contact",
                            detail=f"the phone number {phone!r} appears in no source for this turn",
                        )
                    )
            for address in _ADDRESS.findall(value):
                if str(address).casefold() not in haystack_folded:
                    violations.append(
                        OutputViolation(
                            code="unsourced_contact",
                            detail=f"the address {address!r} appears in no source for this turn",
                        )
                    )
        return violations

    def _booking_state(self, props: Mapping[str, Any]) -> list[OutputViolation]:
        """The booking state must be the booking's own, and it must not be narrated as more than it is.

        §2.5 and the README's release blockers: ``REQUESTED`` and ``PENDING_REVIEW`` are never shown
        as confirmed. Checked against the booking record, because a card that says "confirmed" is
        exactly the failure the record can settle.
        """
        violations: list[OutputViolation] = []
        booking_ids = _as_ids(props.get("booking_id"))
        recorded_state: str | None = None
        if booking_ids:
            recorded_state = self._oracle.booking_state(booking_ids[0])
        for field_name in BOOKING_STATE_FIELDS:
            claimed = props.get(field_name)
            if not isinstance(claimed, str):
                continue
            try:
                state = BookingState(claimed)
            except ValueError:
                violations.append(
                    OutputViolation(
                        code="unconfirmed_booking_state",
                        detail=f"{field_name}={claimed!r} is not a booking state the contract defines",
                    )
                )
                continue
            if recorded_state is None:
                violations.append(
                    OutputViolation(
                        code="unconfirmed_booking_state",
                        detail=(
                            f"the card claims {field_name}={claimed!r} but no booking record could "
                            "be read for it; an unreadable booking is not a confirmed one"
                        ),
                    )
                )
                continue
            if state is not BookingState(recorded_state):
                violations.append(
                    OutputViolation(
                        code="unconfirmed_booking_state",
                        detail=(
                            f"the card says {field_name}={claimed!r} and the booking record says "
                            f"{recorded_state!r}"
                        ),
                    )
                )
            if state in {BookingState.REQUESTED, BookingState.PENDING_REVIEW}:
                for _, value in _walk(props):
                    if not isinstance(value, str):
                        continue
                    match = re.search(r"\bconfirm(ed|ation)\b", value, re.IGNORECASE)
                    if match is not None:
                        violations.append(
                            OutputViolation(
                                code="unconfirmed_booking_state",
                                detail=(
                                    f"the card narrates {match.group(0)!r} while the booking is "
                                    f"{state.value}: REQUESTED and PENDING_REVIEW are never shown as "
                                    "confirmed (§2.5)"
                                ),
                            )
                        )
        return violations

    def _coverage(self, props: Mapping[str, Any]) -> list[OutputViolation]:
        """No guarantee of coverage or payment, and no coverage claim without its evidence."""
        violations: list[OutputViolation] = []
        for _, value in _walk(props):
            if isinstance(value, str):
                for pattern in GUARANTEE_PATTERNS:
                    match = pattern.search(value)
                    if match is not None:
                        violations.append(
                            OutputViolation(
                                code="coverage_guarantee",
                                detail=(
                                    f"the card promises {match.group(0)!r}; a coverage result is a "
                                    "check as of a date, never a guarantee (§10)"
                                ),
                            )
                        )
        claims = sorted(
            field
            for field, value in _walk(props)
            if field in _COVERAGE_FIELDS and value is not None
        )
        if claims:
            evidence = [item for item in _as_ids(props.get("evidence_ids")) if item]
            if not evidence:
                violations.append(
                    OutputViolation(
                        code="unsourced_coverage",
                        detail=(
                            f"the card reports {', '.join(claims)} with no evidence id; structured "
                            "coverage is shown with the record that produced it (§10)"
                        ),
                    )
                )
        return violations

    def _clinical(self, props: Mapping[str, Any]) -> list[OutputViolation]:
        """No clinical language, and no field to put advice in (§1: the demo makes no clinical call)."""
        violations: list[OutputViolation] = []
        for field_name, value in _walk(props):
            if field_name in CLINICAL_FIELDS:
                violations.append(
                    OutputViolation(
                        code="clinical_language",
                        detail=f"the card carries a {field_name!r} field, which has no place in it",
                    )
                )
            if not isinstance(value, str):
                continue
            for pattern in CLINICAL_PATTERNS:
                match = pattern.search(value)
                if match is not None:
                    violations.append(
                        OutputViolation(
                            code="clinical_language",
                            detail=(
                                f"the card says {match.group(0)!r}; this demo gives no diagnosis, "
                                "dosage or treatment advice (§1)"
                            ),
                        )
                    )
        return violations


#: How many trailing digits make two spellings the same number. A country code or a trunk ``0`` is a
#: prefix, not part of the number, so a card that re-spells a sourced number with one still matches.
_NUMBER_TAIL: Final = 9


def _sourced_number(digits: str, haystack_digits: str) -> bool:
    """Whether a number the card shows is a number the sources carried, allowing a prefix on either side.

    ``555 000 4321``, ``0555 000 4321`` and ``+90 555 000 4321`` are one number written three ways, and
    a card that re-spells a number the retrieval provided has not fabricated anything. Containment
    covers the case where the *source* is the longer spelling; the tail comparison covers the case where
    the *card* is. A number shorter than nine digits must match exactly: at that length a tail
    comparison starts matching different numbers, and this check exists to catch invented ones.
    """
    if digits in haystack_digits:
        return True
    return len(digits) > _NUMBER_TAIL and digits[-_NUMBER_TAIL:] in haystack_digits


def _walk(props: Mapping[str, Any]) -> Iterable[tuple[str, Any]]:
    """Every (field, value) pair in a props tree, at any depth.

    Depth matters: a guardrail that only read the top level would pass a card whose candidate list
    carries an invented phone number one level down.
    """
    for key, value in props.items():
        yield key, value
        if isinstance(value, Mapping):
            yield from _walk(value)
        elif isinstance(value, Sequence) and not isinstance(value, str):
            for item in value:
                if isinstance(item, Mapping):
                    yield from _walk(item)


def _as_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Sequence):
        return [str(item) for item in value if isinstance(item, str) and item.strip()]
    return []


def _parse_instant(value: str | None) -> datetime | None:
    """Parse an ISO instant, requiring an offset. A naive time is not a verified time."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed
