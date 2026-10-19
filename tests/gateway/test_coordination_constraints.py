"""Recorded constraints remain binding when the coordinator offers appointments."""

import copy
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from referraleasy_gateway.control import WorkflowRow
from referraleasy_gateway.coordination import CoordinationError, CoordinationService, WorkflowInputs
from referraleasy_gateway.referral_sources import demo_sources

ROOT = Path(__file__).resolve().parents[2]
PATIENT = "b0000000-0000-4000-8000-000000000001"


def source_with(*constraints):
    sources = demo_sources(PATIENT)
    sources["preferences"]["constraints"].extend(
        {
            "constraint_id": str(uuid4()),
            "field": field,
            "value": value,
            "kind": "hard",
            "source": "patient_stated",
            "recorded_at": "2026-10-18T12:00:00Z",
        }
        for field, value in constraints
    )
    return WorkflowInputs(
        WorkflowRow(
            workflow_id=uuid4(),
            ticket_ref="test",
            patient_ref=PATIENT,
            state="READINESS_CHECK",
            state_version=5,
            generation=1,
            payload={
                "source_data": sources,
                "answers": [
                    {
                        "state": "AWAITING_HOSPITAL_CHOICE",
                        "value": {"hospital_id": "20000000-0000-4000-8000-000000000001"},
                    },
                    {
                        "state": "AWAITING_DOCTOR_CHOICE",
                        "value": {"provider_id": "30000000-0000-4000-8000-000000000001"},
                    },
                ],
            },
        )
    )


def test_multiple_days_and_periods_offer_either_value_in_the_display_timezone():
    source = source_with(
        ("preferred_day", "Tuesday"),
        ("preferred_day", "Thursday"),
        ("preferred_time_of_day", "morning"),
        ("preferred_time_of_day", "afternoon"),
    )
    slots = json.loads((ROOT / "fixtures/directory/slots.json").read_text())
    wanted = [slots[0], slots[2]]
    wednesday = {**slots[0], "start_at": "2026-10-21T14:00:00Z"}
    assert CoordinationService._slots([*wanted, wednesday], source) == wanted


@pytest.mark.parametrize(
    "field,value",
    [
        ("accepts_new_patients", "false"),
        ("preferred_day", "Funday"),
        ("preferred_time_of_day", "night"),
        ("max_distance_miles", -1),
        ("accessibility", "unknown"),
        ("specialty", "neurology"),
        ("visit_type", "return_visit"),
    ],
)
def test_invalid_or_conflicting_constraints_need_review(field, value):
    with pytest.raises(CoordinationError, match="needs review"):
        source_with((field, value))


def test_a_slot_from_another_selection_is_refused():
    slots = json.loads((ROOT / "fixtures/directory/slots.json").read_text())
    wrong = copy.deepcopy(slots[0])
    wrong["provider_id"] = str(uuid4())
    with pytest.raises(CoordinationError, match="different selection"):
        CoordinationService._slots([wrong], source_with())


@pytest.mark.parametrize(
    "state,value,expected",
    [
        ("AWAITING_HOSPITAL_CHOICE", {"hospital_id": "hospital"}, "hospital"),
        ("AWAITING_DOCTOR_CHOICE", {"hospital_id": None, "provider_id": "doctor"}, "doctor"),
        ("AWAITING_DOCTOR_CHOICE", {"hospital_id": "hospital", "provider_id": "doctor"}, "doctor"),
        (
            "AWAITING_SLOT_CHOICE",
            {"hospital_id": "hospital", "provider_id": "doctor", "slot_id": "slot"},
            "slot",
        ),
        ("AWAITING_SLOT_CHOICE", {"slot_id": None, "selected_id": "slot"}, "slot"),
        ("AWAITING_DOCTOR_CHOICE", {"provider_id": None}, None),
        ("AWAITING_DOCTOR_CHOICE", {"selected_id": "doctor"}, "doctor"),
        ("AWAITING_DOCTOR_CHOICE", {"option_id": "doctor"}, "doctor"),
        ("AWAITING_DOCTOR_CHOICE", {"id": "doctor"}, "doctor"),
    ],
)
def test_patient_choice_uses_the_id_for_its_question(state, value, expected):
    row = source_with().row
    inputs = WorkflowInputs(
        replace(row, payload={**row.payload, "answers": [{"state": state, "value": value}]})
    )
    assert inputs.choice(state) == expected
