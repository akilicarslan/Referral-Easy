# ruff: noqa: RUF001, RUF002  # the Turkish claims and §9's sentence are quoted verbatim
"""§9's output guardrail: a card is shown only if every claim on it is tied to a record (§9, §2.5).

§9 lists it as six things — *"schema, mevcut entity ID, doğrulanmış saat, kaynaklı telefon/adres ve
gerçek booking state"* — with two prohibitions attached: no coverage or payment guarantee, and no
clinical language. This file gives each one a failing example of its own, because a check whose
failure mode is never constructed is a check nobody has seen work. The passing card is here too, and
so is the number of checks that ran: an ``ok`` from a guardrail that looked at nothing is the
vacuously-green result this whole file exists to prevent.

Two rules in this module are the ones that matter most, and both are tested as *behaviour* rather than
as wording:

* **the sources are per call.** A phone number that appeared in yesterday's retrieval is not sourced
  today, so the same card is checked twice with different sources and has to pass once and fail once.
* **a booking state is the record's, not the card's.** The failing examples include a card that says
  ``CONFIRMED`` while the booking says ``REQUESTED``, and a card that merely *narrates* confirmation
  while its state field is honest — §2.5's release blocker, which wording alone would not catch.

The definitions are data (``ENTITY_ID_FIELDS``, ``TIME_FIELDS``, the patterns), so the tests drive the
public ``check`` rather than a private helper: what has to hold is that these specific cards are
refused, whatever the table looks like inside.
"""

from __future__ import annotations

import pytest
from guardrail_fixtures import INSTANCE_ID, StubOracle, card, validated_card

from referraleasy_guardrails.output import (
    BOOKING_STATE_FIELDS,
    OutputGuardrail,
    OutputRejected,
)

SLOT_TIME = "2026-11-02T09:30:00+03:00"
#: The number, the address and the time as a retrieval would return them for this turn. A one-element
#: tuple, and written as one: a bare string here is a set of characters and ``"\\n".join`` would
#: shred the haystack — which it did, which is how this comment got written.
SOURCES = ("Dr. Ayla Kaya — 12 Oak Street, Istanbul. Front desk: +90 555 000 1234.",)

#: Every *claim* a card makes about a record: the slot, the provider, the booking and its state.
CLAIMS_PROPS = {
    "slot_id": "slot-1",
    "provider_id": "prov-1",
    "booking_id": "book-1",
    "booking_status": "REQUESTED",
    "slot_time": SLOT_TIME,
}

#: The claims plus the two contact details, both of which appear in ``SOURCES``.
CONTACT_PROPS = {
    **CLAIMS_PROPS,
    "clinic_phone": "+90 555 000 1234",
    "clinic_address": "12 Oak Street",
}

#: Seven checks, and the names are asserted rather than counted: "the guardrail passed the card" means
#: something only if this list is the whole list.
ALL_CHECKS = (
    "schema",
    "entity_ids",
    "times",
    "sourced_contacts",
    "booking_state",
    "coverage",
    "clinical_language",
)


def live_oracle(**overrides: object) -> StubOracle:
    """An oracle that can read every record ``CLAIMS_PROPS`` claims."""
    values: dict[str, object] = {
        "entities": {"slot-1", "prov-1", "book-1"},
        "slots": {"slot-1": SLOT_TIME},
        "bookings": {"book-1": "REQUESTED"},
    }
    values.update(overrides)
    return StubOracle(**values)  # type: ignore[arg-type]


def run(props: object, *, sources: tuple[str, ...] = SOURCES, oracle: StubOracle | None = None):
    """One check, over a card built from ``props``. Returns the result, never raises."""
    guardrail = OutputGuardrail(oracle or live_oracle())
    return guardrail.check(card(props), sources=sources)  # type: ignore[arg-type]


def codes(result: object) -> list[str]:
    return [violation.code for violation in result.violations]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The card that passes, and what "passing" is evidence of
# ---------------------------------------------------------------------------


def test_a_card_whose_claims_are_all_records_passes_every_check() -> None:
    """The passing case, asserted together with the list of checks that ran.

    ``ok`` alone would be satisfied by a guardrail that ran nothing, so the seven names are pinned in
    their order. The card carries a phone number and an address, which proves the sourced-contact check
    is a *sourcing* rule rather than a ban on contact details.
    """
    result = run(CONTACT_PROPS)
    assert result.ok is True
    assert result.violations == ()
    assert result.checked == ALL_CHECKS
    assert result.as_dict() == {"ok": True, "checked": list(ALL_CHECKS), "violations": []}


def test_the_same_card_is_also_a_component_instance_and_not_only_a_mapping() -> None:
    """The contract's own model is accepted, because that is what the SSE surface hands over."""
    guardrail = OutputGuardrail(live_oracle())
    result = guardrail.check(
        validated_card(props=CONTACT_PROPS, instance_id=INSTANCE_ID), sources=SOURCES
    )
    assert result.ok is True
    assert result.checked == ALL_CHECKS


def test_check_or_raise_returns_what_check_returned_when_the_card_is_clean() -> None:
    """The raising form is the one the surface calls, so its success path is asserted too."""
    guardrail = OutputGuardrail(live_oracle())
    result = guardrail.check_or_raise(card(CONTACT_PROPS), sources=SOURCES)
    assert result.ok is True


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instance_id", "not-a-uuid"),
        ("component", "NotAComponent"),
        ("workflow_state_version", "one"),
        ("props", "not-an-object"),
        ("stray_key", "a renderer would not know what to do with this"),
    ],
)
def test_a_card_that_is_not_the_contract_s_envelope_is_refused_before_anything_else_is_read(
    field: str, value: object
) -> None:
    """The envelope is validated by the contract's model, and a failure there stops the walk.

    The payload is edited field by field rather than rebuilt, so the *invalid* values reach the
    guardrail: a helper that validated them first would be testing the helper.

    Stopping is deliberate: without a parsed envelope there are no props to check, and findings about
    props would be findings about a card that does not exist. ``checked`` says exactly that — one check
    ran and it failed.
    """
    payload = dict(card(CONTACT_PROPS))
    payload[field] = value
    result = OutputGuardrail(live_oracle()).check(payload, sources=SOURCES)
    assert result.ok is False
    assert codes(result) == ["schema_invalid"]
    assert result.checked == ("schema",)
    assert result.violations[0].detail


# ---------------------------------------------------------------------------
# Entity ids
# ---------------------------------------------------------------------------


def test_an_entity_id_that_exists_nowhere_is_refused_and_named() -> None:
    """A card about a provider who is not in the directory is a card about nothing."""
    result = run({**CLAIMS_PROPS, "provider_id": "prov-404"})
    assert result.ok is False
    assert codes(result) == ["unknown_entity"]
    assert "prov-404" in result.violations[0].detail
    assert "provider_id" in result.violations[0].detail


def test_an_unknown_entity_nested_inside_a_list_is_still_found() -> None:
    """Depth is the point of the walk: candidate lists carry ids one level down."""
    result = run(
        {
            "slot_id": "slot-1",
            "slot_time": SLOT_TIME,
            "candidates": [{"slot_id": "slot-404", "provider_id": "prov-1"}],
        }
    )
    assert codes(result) == ["unknown_entity"]
    assert "slot-404" in result.violations[0].detail


# ---------------------------------------------------------------------------
# Times
# ---------------------------------------------------------------------------


def test_a_time_without_an_offset_has_not_been_verified() -> None:
    """A naive timestamp means different things in different places, so it is not a stated instant."""
    result = run({**CLAIMS_PROPS, "slot_time": "2026-11-02T09:30:00"})
    assert result.ok is False
    assert codes(result) == ["unverified_time"]
    assert "UTC offset" in result.violations[0].detail


def test_a_time_that_disagrees_with_the_slot_record_is_refused_with_both_values() -> None:
    """The comparison is against the scheduler's record, not against the card's own wording.

    The oracle here says 10:30 and the card says 09:30: an appointment at the wrong hour is exactly
    what a patient would act on, and both values appear in the detail so the mismatch can be judged.
    """
    oracle = live_oracle(slots={"slot-1": "2026-11-02T10:30:00+03:00"})
    result = run(CLAIMS_PROPS, oracle=oracle)
    assert codes(result) == ["unverified_time"]
    assert "09:30" in result.violations[0].detail
    assert "10:30" in result.violations[0].detail


def test_a_slot_with_no_readable_time_cannot_verify_anything() -> None:
    """An unreadable slot is not a verified slot: the check refuses rather than shrugging."""
    result = run(CLAIMS_PROPS, oracle=live_oracle(slots={}))
    assert codes(result) == ["unverified_time"]
    assert "no readable time" in result.violations[0].detail


def test_the_same_instant_written_as_z_or_as_an_offset_agrees() -> None:
    """Two spellings of one instant are one instant — the check compares times, not strings."""
    oracle = live_oracle(slots={"slot-1": "2026-11-02T06:30:00+00:00"})
    result = run({**CLAIMS_PROPS, "slot_time": "2026-11-02T06:30:00Z"}, oracle=oracle)
    assert result.ok is True


# ---------------------------------------------------------------------------
# Sourced contacts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "sources", "ok"),
    [
        ("+90 555 000 1234", SOURCES, True),
        ("+90 555 000 9999", SOURCES, False),
        ("+90 555 000 9999", (), False),
        ("+90 555 000 9999", ("reach the clinic on 90 555 000 9999",), True),
    ],
)
def test_a_phone_number_is_shown_only_when_the_turn_retrieved_it(
    value: str, sources: tuple[str, ...], ok: bool
) -> None:
    """Written with and without the country code, because a number is a number whichever way it is spelled."""
    result = run({**CLAIMS_PROPS, "clinic_phone": value}, sources=sources)
    assert result.ok is ok
    if not ok:
        assert codes(result) == ["unsourced_contact"]
        assert value in result.violations[0].detail


@pytest.mark.parametrize(
    ("card_number", "source_text", "ok"),
    [
        ("+90 555 000 4321", "the desk can be reached on 555 000 4321", True),
        ("555 000 4321", "the desk can be reached on +90 555 000 4321", True),
        ("0555 000 4321", "the desk can be reached on +90 555 000 4321", True),
        ("+90 555 000 9999", "the desk can be reached on +90 555 000 1234", False),
        ("555 000 9999", "the desk can be reached on +90 555 000 1234", False),
    ],
)
def test_one_number_written_with_a_prefix_is_one_sourced_number(
    card_number: str, source_text: str, ok: bool
) -> None:
    """A country code or a trunk zero is a prefix, not part of the number, so it may be on either side.

    The negative cases are the point: two *different* numbers of the same length must not match, which
    is why the comparison keeps the whole number for anything shorter than nine digits and only
    tolerates a prefix beyond that.
    """
    result = run({**CLAIMS_PROPS, "clinic_phone": card_number}, sources=(source_text,))
    assert result.ok is ok
    if not ok:
        assert codes(result) == ["unsourced_contact"]


def test_an_address_that_appears_in_no_source_is_refused() -> None:
    """The address case: a patient who drives to an invented address is the cost of missing this."""
    result = run({**CLAIMS_PROPS, "clinic_address": "44 Elm Avenue"})
    assert codes(result) == ["unsourced_contact"]
    assert "44 Elm Avenue" in result.violations[0].detail


def test_the_sources_are_the_turn_s_and_yesterday_s_are_not() -> None:
    """The same card, twice: sourced this turn and unsourced when the material is not there.

    Passing the sources per call is what makes the second check fail. A guardrail that held them would
    have passed a number it never saw on this turn, and it would do so silently.
    """
    guardrail = OutputGuardrail(live_oracle())
    props = {**CLAIMS_PROPS, "clinic_phone": "+90 555 000 4321"}
    assert (
        guardrail.check(card(props), sources=("the desk can be reached on 555 000 4321",)).ok
        is True
    )
    later = guardrail.check(card(props), sources=SOURCES)
    assert later.ok is False
    assert codes(later) == ["unsourced_contact"]


def test_one_unsourced_number_is_reported_once_however_many_fields_carry_it() -> None:
    """Two fields, one finding — a card that repeats a number has one thing to fix, not three."""
    result = run(
        {
            **CLAIMS_PROPS,
            "clinic_phone": "+90 555 000 7777",
            "alternate_phone": "+90 555 000 7777",
        }
    )
    assert codes(result) == ["unsourced_contact"]
    assert len(result.violations) == 1


@pytest.mark.parametrize("value", ["2026-11-02T09:30:00+03:00", "2026-11-02", "02/11/2026"])
def test_a_time_is_not_read_as_a_phone_number(value: str) -> None:
    """The regression this check needed: an ISO instant has the shape of a hyphenated number.

    ``2026-11-02`` matches the phone pattern (four digits, hyphen, two, hyphen, two), so a card that
    stated a time or a date was refused for carrying an unsourced phone number it never carried. The
    sources here contain neither the date nor the time, which is what makes the test meaningful — a
    card must not be refused for stating when the appointment is.
    """
    result = run(
        {"slot_id": "slot-1", "appointment_date": value, "label": "Your appointment"},
        sources=("Dr. Ayla Kaya, cardiology. Front desk: +90 555 000 1234.",),
    )
    assert codes(result) == []
    assert result.ok is True


# ---------------------------------------------------------------------------
# Booking state (§2.5's release blocker)
# ---------------------------------------------------------------------------


def test_a_booking_claim_is_checked_against_the_booking_record() -> None:
    """The card says CONFIRMED, the record says REQUESTED: both are printed and the card is refused."""
    result = run({**CLAIMS_PROPS, "booking_status": "CONFIRMED"})
    assert result.ok is False
    assert codes(result) == ["unconfirmed_booking_state"]
    assert "CONFIRMED" in result.violations[0].detail
    assert "REQUESTED" in result.violations[0].detail


def test_a_state_the_contract_does_not_define_is_not_a_state() -> None:
    """A free-text status is refused rather than read generously."""
    result = run({**CLAIMS_PROPS, "booking_status": "probably fine"})
    assert codes(result) == ["unconfirmed_booking_state"]
    assert "not a booking state the contract defines" in result.violations[0].detail


def test_an_unreadable_booking_is_not_a_confirmed_one() -> None:
    """No record means the claim cannot be checked, and an unchecked claim is not shown."""
    result = run(CLAIMS_PROPS, oracle=live_oracle(bookings={}))
    assert codes(result) == ["unconfirmed_booking_state"]
    assert "no booking record could be read" in result.violations[0].detail


def test_a_card_that_narrates_confirmation_while_the_booking_is_only_requested_is_refused() -> None:
    """§2.5's release blocker: the state field is honest and the *wording* is not.

    Every string in the tree is read for the word, because the failure this project is most exposed to
    is not a wrong enum value — it is a card whose heading says "Appointment confirmed" while the
    booking sits in the review queue.
    """
    result = run({**CLAIMS_PROPS, "title": "Appointment confirmed"})
    assert result.ok is False
    assert codes(result) == ["unconfirmed_booking_state"]
    assert "REQUESTED" in result.violations[0].detail
    assert "never shown as confirmed" in result.violations[0].detail


def test_a_confirmed_booking_may_say_so() -> None:
    """The same wording on a booking the record confirms is allowed — the rule is about the record."""
    oracle = live_oracle(bookings={"book-1": "CONFIRMED"})
    result = run(
        {**CLAIMS_PROPS, "booking_status": "CONFIRMED", "title": "Appointment confirmed"},
        oracle=oracle,
    )
    assert result.ok is True


def test_the_booking_state_fields_are_what_the_check_reads() -> None:
    """The documented field names, asserted so a rename in the card catalog is a test failure."""
    assert set(BOOKING_STATE_FIELDS) == {"booking_status", "booking_state", "status"}


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "claim",
    [
        "Your visit is fully covered.",
        "The insurer will be covered for this. Guaranteed.",
        "There is no cost to you for this appointment.",
        "Bu işlem tamamen karşılanır.",
        "Ücret alınmaz.",
    ],
)
def test_a_promise_about_coverage_or_payment_is_refused_in_both_languages(claim: str) -> None:
    """§10: a coverage result is a check as of a date. The demo has no payer contract to promise with."""
    result = run({**CLAIMS_PROPS, "coverage_note": claim})
    assert result.ok is False
    assert codes(result) == ["coverage_guarantee"]


def test_a_coverage_claim_without_its_evidence_is_refused() -> None:
    """Structured coverage is shown with the record that produced it, or not shown."""
    result = run({**CLAIMS_PROPS, "coverage_status": "in_network"})
    assert codes(result) == ["unsourced_coverage"]
    assert "evidence id" in result.violations[0].detail


def test_a_coverage_claim_with_its_evidence_is_allowed() -> None:
    """The passing side of the same check: the claim is fine once it points at a record."""
    oracle = live_oracle(entities={"slot-1", "prov-1", "book-1", "plan-1"})
    result = run(
        {**CLAIMS_PROPS, "coverage_status": "in_network", "evidence_ids": ["plan-1"]},
        oracle=oracle,
    )
    assert result.ok is True


# ---------------------------------------------------------------------------
# Clinical language
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "note",
    [
        "Take 500 mg twice a day.",
        "The diagnosis is migraine.",
        "Stop taking your current prescription.",
        "Günde 2 kez ilaç kullanın.",
    ],
)
def test_clinical_language_is_refused(note: str) -> None:
    """§1: this demo makes no clinical decision, so no card may carry one."""
    result = run({**CLAIMS_PROPS, "clinic_note": note})
    assert result.ok is False
    assert codes(result) == ["clinical_language"]


def test_a_clinical_field_is_refused_even_when_its_text_looks_innocent() -> None:
    """The field *name* is the finding: advice arriving under a helpful key is still advice."""
    result = run({**CLAIMS_PROPS, "medical_advice": "see a doctor if it persists"})
    assert codes(result) == ["clinical_language"]
    assert "medical_advice" in result.violations[0].detail


# ---------------------------------------------------------------------------
# Reporting all of it
# ---------------------------------------------------------------------------


def test_every_violation_is_reported_and_not_only_the_first() -> None:
    """A card that failed for three reasons has three edits to make, and one refusal to read."""
    result = run(
        {
            **CLAIMS_PROPS,
            "provider_id": "prov-404",
            "clinic_phone": "+90 555 000 9999",
            "clinic_note": "Take 500 mg twice a day.",
        }
    )
    assert result.ok is False
    assert sorted(codes(result)) == ["clinical_language", "unknown_entity", "unsourced_contact"]
    for violation in result.violations:
        assert violation.detail
        assert set(violation.as_dict()) == {"code", "detail"}


def test_the_rejection_message_lists_every_violation_and_carries_the_result() -> None:
    """The message is what a log line and a developer see, so it names all three codes."""
    guardrail = OutputGuardrail(live_oracle())
    dirty = card(
        {**CLAIMS_PROPS, "provider_id": "prov-404", "clinic_note": "Take 500 mg twice a day."}
    )
    with pytest.raises(OutputRejected) as raised:
        guardrail.check_or_raise(dirty, sources=SOURCES)
    message = str(raised.value)
    assert "unknown_entity" in message and "clinical_language" in message
    assert raised.value.result.ok is False
    assert raised.value.result.checked == ALL_CHECKS
