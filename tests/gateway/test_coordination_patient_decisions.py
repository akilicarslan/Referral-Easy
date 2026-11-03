"""The coordinator's patient-decision helpers behind scenarios 2, 3 and 4.

Pure functions over a workflow row, so these run without agents or a hospital API. The journeys
themselves are `tests/e2e/test_scenario_journeys.py`.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from referraleasy_gateway.control import WorkflowRow
from referraleasy_gateway.coordination import CoordinationService, WorkflowInputs
from referraleasy_gateway.referral_sources import demo_sources

PATIENT = "b0000000-0000-4000-8000-000000000002"
PRIOR_REPORT = "a0000000-0000-4000-8000-000000000005"


def _row(
    payload: dict[str, Any], *, patient: str = PATIENT, state: str = "READINESS_CHECK"
) -> WorkflowRow:
    return WorkflowRow(
        workflow_id=uuid4(),
        ticket_ref="T",
        patient_ref=patient,
        state=state,
        state_version=5,
        generation=1,
        payload=payload,
    )


def test_the_patients_own_documents_are_offered_only_for_the_missing_kinds() -> None:
    source = WorkflowInputs(_row({"source_data": demo_sources(PATIENT)}))
    assert [d["document_id"] for d in source.attachable(["prior_report"])] == [PRIOR_REPORT]
    assert source.attachable(["authorization_reference"]) == []
    # Already on the referral: never offered again.
    assert source.attachable(["referral_letter"]) == []


def test_an_attachment_is_read_only_from_an_answer_to_the_card_it_was_offered_on() -> None:
    answers = [
        {"state": "READINESS_CHECK", "state_version": 4, "value": {"document_ids": ["old"]}},
        {"state": "READINESS_CHECK", "state_version": 5, "value": {"document_ids": [PRIOR_REPORT]}},
    ]
    source = WorkflowInputs(_row({"source_data": demo_sources(PATIENT), "answers": answers}))
    assert source.document_answer(5) == [PRIOR_REPORT]
    assert source.document_answer(6) is None


def test_attached_documents_travel_with_the_referrals_own() -> None:
    sources = demo_sources(PATIENT)
    source = WorkflowInputs(_row({"source_data": sources, "attached_document_ids": [PRIOR_REPORT]}))
    assert source.document_ids() == [*sources["referral"]["document_ids"], PRIOR_REPORT]


def test_a_passed_check_keeps_its_first_result_and_only_failed_ones_are_replaced() -> None:
    first = [
        {"kind": "referral_letter", "status": "satisfied", "run_id": "r1"},
        {"kind": "prior_report", "status": "missing", "run_id": "r1"},
    ]
    second = [
        {"kind": "referral_letter", "status": "satisfied", "run_id": "r2"},
        {"kind": "prior_report", "status": "satisfied", "run_id": "r2"},
    ]
    kept = CoordinationService._kept_checks(first, second)
    assert [(c["kind"], c["run_id"]) for c in kept] == [
        ("referral_letter", "r1"),
        ("prior_report", "r2"),
    ]
    assert CoordinationService._kept_checks(None, second) == second


def test_a_recorded_constraint_change_applies_on_top_of_the_untouched_preferences() -> None:
    patient = "b0000000-0000-4000-8000-000000000003"
    sources = demo_sources(patient)
    day = next(
        c
        for c in sources["preferences"]["constraints"]
        if c["field"] == "preferred_day" and not c["superseded_by"]
    )
    change = {"constraint_id": day["constraint_id"], "to_value": "tuesday"}
    source = WorkflowInputs(
        _row({"source_data": sources, "constraint_changes": [change]}, patient=patient)
    )
    assert (
        next(c for c in source.constraints if c["constraint_id"] == day["constraint_id"])["value"]
        == "tuesday"
    )
    assert day["value"] != "tuesday", "the recorded preference itself is never edited"


def test_a_coverage_finding_is_acknowledged_only_by_choosing_that_doctor_again_after_seeing_it() -> (
    None
):
    before = [{"state": "AWAITING_DOCTOR_CHOICE", "value": {"provider_id": "p1"}}]
    review = {"provider_id": "p1", "answers_before": 1}
    assert not CoordinationService._acknowledged(
        _row({"answers": before, "coverage_review": review}), "p1"
    )
    after = [*before, {"state": "AWAITING_DOCTOR_CHOICE", "value": {"provider_id": "p1"}}]
    assert CoordinationService._acknowledged(
        _row({"answers": after, "coverage_review": review}), "p1"
    )
    assert not CoordinationService._acknowledged(
        _row({"answers": after, "coverage_review": review}), "p2"
    )
