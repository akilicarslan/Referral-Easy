"""Verify the projection lifecycle through its public interface and both adapters."""

from uuid import uuid4

import pytest

from referraleasy_contracts.generated.common import WorkflowState
from referraleasy_domain import IllegalTransitionError
from referraleasy_gateway.control import ControlState, WorkflowRow
from referraleasy_gateway.errors import ContractViolationError
from referraleasy_gateway.projection import WorkflowSurfaceProjection
from referraleasy_gateway.surfaces import instance_id_of

HOSPITAL_PROPS = {"options": [{"id": "hospital-1"}]}
DOCTOR_PROPS = {"options": [{"id": "doctor-1"}]}
CHOICE_ACTION = "system:await_patient_choice"


def move(
    control: ControlState,
    projection: WorkflowSurfaceProjection,
    row: WorkflowRow,
    state: WorkflowState,
    props: dict,
) -> WorkflowRow:
    control.assert_can_advance(
        row.workflow_id, expected_state_version=row.state_version, to_state=state.value
    )
    projection.prepare(row, state, props)
    moved = control.advance(
        row.workflow_id, expected_state_version=row.state_version, to_state=state.value
    )
    projection.show(moved, props)
    current = control.load(row.workflow_id)
    assert current is not None
    return current


@pytest.fixture
def hospital(control):
    projection = WorkflowSurfaceProjection(control)
    row = control.create("projection-patient")
    row = move(control, projection, row, WorkflowState.PROVIDER_MATCHING, {})
    row = move(control, projection, row, WorkflowState.AWAITING_HOSPITAL_CHOICE, HOSPITAL_PROPS)
    return projection, row


def test_a_second_projection_sees_the_answer_and_does_not_open_it_again(control, hospital):
    projection, row = hospital
    original = projection.visible(row)[0]
    other = WorkflowSurfaceProjection(control)
    assert other.visible(row) == (original,)
    assert other.require_action(row, instance_id_of(original), CHOICE_ACTION) == original

    change = other.answer(row, instance_id_of(original))
    assert change.components[0].submitted is True
    assert projection.visible(row) == ()
    assert WorkflowSurfaceProjection(control).repair(row) is None
    with pytest.raises(ContractViolationError, match="was already answered"):
        projection.require_action(row, instance_id_of(original), CHOICE_ACTION)


def test_retired_cards_stay_refused_after_replacement(control, hospital):
    projection, row = hospital
    hospital_id = instance_id_of(projection.visible(row)[0])
    projection.prepare(row, WorkflowState.AWAITING_DOCTOR_CHOICE, DOCTOR_PROPS)
    moved = control.advance(
        row.workflow_id,
        expected_state_version=row.state_version,
        to_state=WorkflowState.AWAITING_DOCTOR_CHOICE.value,
    )
    change = projection.show(moved, DOCTOR_PROPS)
    assert change.remove_instance_ids == (hospital_id,)
    assert change.components[0].props == DOCTOR_PROPS

    restarted = WorkflowSurfaceProjection(control)
    assert restarted.visible(moved) == change.components
    with pytest.raises(ContractViolationError, match="was already answered"):
        restarted.require_action(moved, hospital_id, CHOICE_ACTION)
    with pytest.raises(ContractViolationError, match="was never issued"):
        restarted.require_action(moved, uuid4(), CHOICE_ACTION)


def test_repair_uses_the_same_intent_and_retires_the_previous_card(control, hospital):
    projection, row = hospital
    previous = instance_id_of(projection.visible(row)[0])
    projection.prepare(row, WorkflowState.AWAITING_DOCTOR_CHOICE, DOCTOR_PROPS)
    moved = control.advance(
        row.workflow_id,
        expected_state_version=row.state_version,
        to_state=WorkflowState.AWAITING_DOCTOR_CHOICE.value,
    )
    # The process stops after the transition and before card storage.
    restarted = WorkflowSurfaceProjection(control)
    assert restarted.visible(moved) == ()
    repaired = restarted.repair(moved)
    assert repaired is not None
    assert repaired.remove_instance_ids == (previous,)
    assert repaired.components[0].props == DOCTOR_PROPS
    assert restarted.visible(moved) == repaired.components
    assert WorkflowSurfaceProjection(control).repair(moved) is None
    assert (
        restarted.require_action(moved, instance_id_of(repaired.components[0]), CHOICE_ACTION)
        == repaired.components[0]
    )


def test_empty_projection_retires_the_card_and_is_complete_after_restart(control, hospital):
    projection, row = hospital
    previous = instance_id_of(projection.visible(row)[0])
    projection.prepare(row, WorkflowState.CANCELLED, {})
    moved = control.advance(
        row.workflow_id,
        expected_state_version=row.state_version,
        to_state=WorkflowState.CANCELLED.value,
    )
    restarted = WorkflowSurfaceProjection(control)
    change = restarted.repair(moved)
    assert change is not None
    assert change.components == ()
    assert change.remove_instance_ids == (previous,)
    assert restarted.visible(moved) == ()
    assert WorkflowSurfaceProjection(control).repair(moved) is None
    with pytest.raises(ContractViolationError, match="was already answered"):
        restarted.require_action(moved, previous, CHOICE_ACTION)


def test_invalid_props_do_not_change_the_intent_or_current_card(control, hospital):
    projection, row = hospital
    before = control.load(row.workflow_id)
    with pytest.raises(ContractViolationError, match="DoctorPicker needs"):
        projection.prepare(row, WorkflowState.AWAITING_DOCTOR_CHOICE, {})
    with pytest.raises(ContractViolationError, match="HospitalPicker needs"):
        projection.show(row, {})
    assert control.load(row.workflow_id) == before


def test_repair_does_not_apply_an_intent_before_its_transition(control, hospital):
    projection, row = hospital
    original = projection.visible(row)
    projection.prepare(row, WorkflowState.AWAITING_DOCTOR_CHOICE, DOCTOR_PROPS)
    assert WorkflowSurfaceProjection(control).repair(row) is None
    assert projection.visible(row) == original


def test_repair_refuses_an_intent_for_an_earlier_visit_to_the_same_state(control, hospital):
    projection, row = hospital
    projection.prepare(row, WorkflowState.AWAITING_DOCTOR_CHOICE, DOCTOR_PROPS)
    for target in (
        WorkflowState.AWAITING_DOCTOR_CHOICE,
        WorkflowState.AWAITING_HOSPITAL_CHOICE,
        WorkflowState.AWAITING_DOCTOR_CHOICE,
    ):
        row = control.advance(
            row.workflow_id, expected_state_version=row.state_version, to_state=target.value
        )
    assert WorkflowSurfaceProjection(control).repair(row) is None
    assert projection.visible(row) == ()


def test_repair_accepts_intent_records_from_before_the_version_field(control, hospital):
    projection, row = hospital
    # Gateways before this change stored the target state without a target version.
    control.update_payload(
        row.workflow_id,
        expected_state_version=row.state_version,
        patch={
            "coordination_projection": {"state": "AWAITING_DOCTOR_CHOICE", "props": DOCTOR_PROPS}
        },
    )
    moved = control.advance(
        row.workflow_id,
        expected_state_version=row.state_version,
        to_state=WorkflowState.AWAITING_DOCTOR_CHOICE.value,
    )
    repaired = WorkflowSurfaceProjection(control).repair(moved)
    assert repaired is not None
    assert repaired.components[0].props == DOCTOR_PROPS
    assert projection.visible(moved) == repaired.components


def test_a_stale_row_cannot_replace_a_newer_projection(control, hospital):
    projection, row = hospital
    moved = move(control, projection, row, WorkflowState.AWAITING_DOCTOR_CHOICE, DOCTOR_PROPS)
    with pytest.raises(IllegalTransitionError):
        projection.show(row, HOSPITAL_PROPS)
    with pytest.raises(IllegalTransitionError):
        projection.prepare(row, WorkflowState.AWAITING_DOCTOR_CHOICE, DOCTOR_PROPS)
    assert projection.visible(moved)[0].props == DOCTOR_PROPS


def test_reads_keep_the_observed_version_if_a_worker_advances(control, hospital):
    projection, row = hospital
    original = projection.visible(row)
    moved = move(control, projection, row, WorkflowState.AWAITING_DOCTOR_CHOICE, DOCTOR_PROPS)
    assert projection.repair(row) is None
    assert projection.visible(row) == original
    assert projection.visible(moved)[0].props == DOCTOR_PROPS


def test_repair_allows_a_worker_to_advance_before_card_storage(control, hospital, monkeypatch):
    projection, row = hospital
    projection.prepare(row, WorkflowState.AWAITING_DOCTOR_CHOICE, DOCTOR_PROPS)
    moved = control.advance(
        row.workflow_id,
        expected_state_version=row.state_version,
        to_state=WorkflowState.AWAITING_DOCTOR_CHOICE.value,
    )
    show = projection.show

    def advance_before_storage(row, props):
        control.advance(
            row.workflow_id,
            expected_state_version=row.state_version,
            to_state=WorkflowState.CANCELLED.value,
        )
        return show(row, props)

    monkeypatch.setattr(projection, "show", advance_before_storage)
    assert projection.repair(moved) is None
    assert control.load(row.workflow_id).state == "CANCELLED"


def test_repeated_projection_keeps_one_card_and_one_id(control, hospital):
    projection, row = hospital
    original = projection.visible(row)[0]
    change = projection.show(row, HOSPITAL_PROPS)
    assert change.components == (original,)
    assert change.remove_instance_ids == ()
    assert WorkflowSurfaceProjection(control).visible(row) == (original,)


def test_an_action_cannot_cross_workflows_or_change_the_issued_action(control, hospital):
    projection, row = hospital
    card_id = instance_id_of(projection.visible(row)[0])
    with pytest.raises(ContractViolationError, match="it never invents one"):
        projection.require_action(row, card_id, "system:capture_consent")
    elsewhere = control.create("another-patient", ticket_ref="T-PROJECTION-OTHER")
    with pytest.raises(ContractViolationError, match="was never issued"):
        projection.require_action(elsewhere, card_id, CHOICE_ACTION)


def test_an_earlier_handoff_receipt_cannot_replace_a_new_consent_question(control, hospital):
    projection, row = hospital
    draft = {
        "patient_visible_summary": "Review the first summary.",
        "reason_class": "technical_outage",
    }
    row = control.update_payload(
        row.workflow_id, expected_state_version=row.state_version, patch={"handoff_draft": draft}
    )
    row = move(
        control,
        projection,
        row,
        WorkflowState.HUMAN_HANDOFF,
        {"handoff": draft, "consent_required": True},
    )
    card = projection.visible(row)[0]
    queued = control.queue_handoff(
        row.workflow_id,
        expected_state_version=row.state_version,
        consent_at="2026-10-19T12:00:00+00:00",
    )
    projection.answer(row, instance_id_of(card), handoff=queued)
    row = move(control, projection, row, WorkflowState.PROVIDER_MATCHING, {})
    next_draft = {**draft, "patient_visible_summary": "Review the second summary."}
    row = move(
        control,
        projection,
        row,
        WorkflowState.HUMAN_HANDOFF,
        {"handoff": next_draft, "consent_required": True},
    )
    assert projection.repair(row) is None
    current = projection.visible(row)[0]
    assert current.props["handoff"] == next_draft
    assert current.props["consent_required"] is True
    assert (
        projection.require_action(row, instance_id_of(current), "system:capture_consent") == current
    )
