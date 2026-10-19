"""Current cards survive rebuilding the gateway over the same control store."""

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from gateway_fixtures import Demo

from referraleasy_contracts.generated.common import WorkflowState
from referraleasy_gateway.app import create_app
from referraleasy_gateway.config import GatewaySettings
from referraleasy_gateway.control import SeedableControlState
from referraleasy_gateway.projection import WorkflowSurfaceProjection
from referraleasy_gateway.timing import ManualClock


def test_a_restart_restores_the_current_question_and_accepts_its_answer(
    settings: GatewaySettings, clock: ManualClock, control: SeedableControlState
) -> None:
    with TestClient(create_app(settings=settings, clock=clock, control=control)) as first:
        demo = Demo(first)
        workflow_id = demo.seed()
        demo.move(workflow_id, version=1, to_state=WorkflowState.PROVIDER_MATCHING)
        response = demo.move(
            workflow_id,
            version=2,
            to_state=WorkflowState.AWAITING_HOSPITAL_CHOICE,
            props={"options": [{"id": "hospital-1"}]},
        )
        original = response.json()["components"][0]
    with TestClient(create_app(settings=settings, clock=clock, control=control)) as second:
        demo = Demo(second)
        restored = demo.snapshot(workflow_id).json()["components"]
        assert restored == [original]
        answered = demo.action(
            workflow_id, instance=original, version=3, value={"selected_id": "hospital-1"}
        )
        assert answered.status_code == 200, answered.text
    with TestClient(create_app(settings=settings, clock=clock, control=control)) as third:
        demo = Demo(third)
        assert demo.snapshot(workflow_id).json()["components"] == []
        refused = demo.action(
            workflow_id, instance=original, version=3, value={"selected_id": "hospital-1"}
        )
        assert refused.status_code == 422


def stop_projection(*args, **kwargs):
    raise RuntimeError("The process stopped before card storage.")


def test_a_snapshot_repairs_a_transition_interrupted_before_card_storage(
    settings, clock, control, monkeypatch
):
    with TestClient(create_app(settings=settings, clock=clock, control=control)) as first:
        demo = Demo(first)
        workflow_id = demo.seed()
        demo.move(workflow_id, version=1, to_state=WorkflowState.PROVIDER_MATCHING)
        with monkeypatch.context() as patch:
            patch.setattr(WorkflowSurfaceProjection, "show", stop_projection)
            with pytest.raises(RuntimeError, match="process stopped"):
                demo.move(
                    workflow_id,
                    version=2,
                    to_state=WorkflowState.AWAITING_HOSPITAL_CHOICE,
                    props={"options": [{"id": "hospital-1"}]},
                )
    with TestClient(create_app(settings=settings, clock=clock, control=control)) as second:
        demo = Demo(second)
        snapshot = demo.snapshot(workflow_id).json()
        card = snapshot["components"][0]
        assert (snapshot["state"], snapshot["state_version"]) == ("AWAITING_HOSPITAL_CHOICE", 3)
        assert card["props"] == {"options": [{"id": "hospital-1"}]}
        assert demo.snapshot(workflow_id).json() == snapshot
        accepted = demo.action(workflow_id, instance=card, version=3, value={"id": "hospital-1"})
        assert accepted.status_code == 200, accepted.text


def test_two_gateways_read_the_same_answered_card(settings, clock, control):
    with (
        TestClient(create_app(settings=settings, clock=clock, control=control)) as first,
        TestClient(create_app(settings=settings, clock=clock, control=control)) as second,
    ):
        writer, reader = Demo(first), Demo(second)
        workflow_id = writer.seed()
        writer.review_card(workflow_id)
        card = reader.snapshot(workflow_id).json()["components"][0]
        accepted = writer.consent(workflow_id, card, version=6)
        assert accepted.status_code == 200, accepted.text
        assert reader.snapshot(workflow_id).json()["components"] == []
        refused = reader.consent(workflow_id, card, version=6)
        assert refused.status_code == 422, refused.text


@pytest.mark.parametrize("outcome", ["confirmed", "unverified"])
def test_booking_repair_uses_the_recorded_outcome_without_another_booking(
    configured_demo, settings, clock, control, monkeypatch, outcome
):
    workflow_id = configured_demo.seed()
    approval_id = configured_demo.in_booking_flight(workflow_id)
    with monkeypatch.context() as patch:
        patch.setattr(WorkflowSurfaceProjection, "show", stop_projection)
        with pytest.raises(RuntimeError, match="process stopped"):
            configured_demo.booking(
                workflow_id, approval_id=approval_id, version=7, outcome=outcome
            )
    before = control.bookings_for(UUID(workflow_id))
    assert len(before) == 1
    with TestClient(create_app(settings=settings, clock=clock, control=control)) as restarted:
        demo = Demo(restarted)
        snapshot = demo.snapshot(workflow_id).json()
        card = snapshot["components"][0]
        assert snapshot["state"] == ("BOOKED" if outcome == "confirmed" else "BOOKING_UNVERIFIED")
        if outcome == "confirmed":
            assert card["component"] == "AppointmentReceipt"
            assert card["action"] is None
            assert card["props"]["appointment"]["external_ref"] == before[0].external_ref
        else:
            assert card["action"] == "scheduling:lookup_request"
        assert control.bookings_for(UUID(workflow_id)) == before
        assert demo.snapshot(workflow_id).json() == snapshot


def test_handoff_repair_restores_the_receipt_after_queueing(settings, clock, control, monkeypatch):
    with TestClient(create_app(settings=settings, clock=clock, control=control)) as first:
        demo = Demo(first)
        workflow_id = demo.seed()
        row = control.load(UUID(workflow_id))
        draft = {
            "reason_class": "technical_outage",
            "patient_visible_summary": "Review the referral.",
            "reason_detail": "The directory did not answer.",
            "sources": [],
        }
        control.update_payload(
            row.workflow_id,
            expected_state_version=row.state_version,
            patch={"handoff_draft": draft},
        )
        moved = demo.move(
            workflow_id,
            version=1,
            to_state=WorkflowState.HUMAN_HANDOFF,
            props={"handoff": draft, "consent_required": True},
        )
        card = moved.json()["components"][0]
        with monkeypatch.context() as patch:
            patch.setattr(WorkflowSurfaceProjection, "answer", stop_projection)
            with pytest.raises(RuntimeError, match="process stopped"):
                demo.action(workflow_id, instance=card, version=2, value={"consented": True})
        queued = control.load(row.workflow_id).payload["handoff"]
    with TestClient(create_app(settings=settings, clock=clock, control=control)) as restarted:
        demo = Demo(restarted)
        receipt = demo.snapshot(workflow_id).json()["components"][0]
        assert receipt["action"] is None
        assert receipt["props"]["consent_required"] is False
        assert receipt["props"]["reference"] == queued["ticket_ref"]
        assert receipt["instance_id"] != card["instance_id"]
        refused = demo.action(workflow_id, instance=card, version=2, value={"consented": True})
        assert refused.status_code == 422, refused.text
        assert control.load(row.workflow_id).payload["handoff"] == queued
        assert control.bookings_for(row.workflow_id) == ()
