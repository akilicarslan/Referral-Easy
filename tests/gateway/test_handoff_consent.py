"""A human ticket contains only the summary that the patient approved."""

from uuid import UUID, uuid4

from referraleasy_contracts.generated.common import WorkflowState
from referraleasy_gateway.projection import SURFACE_PAYLOAD_KEY


def test_handoff_needs_explicit_consent_and_replays_once(demo, control):
    workflow_id = UUID(demo.seed())
    row = control.load(workflow_id)
    draft = {
        "reason_class": "technical_outage",
        "patient_visible_summary": "Review the referral.",
        "reason_detail": "The directory did not answer.",
        "sources": [],
    }
    control.update_payload(
        workflow_id, expected_state_version=row.state_version, patch={"handoff_draft": draft}
    )
    moved = demo.move(
        workflow_id,
        version=1,
        to_state=WorkflowState.HUMAN_HANDOFF,
        props={"handoff": draft, "consent_required": True},
    )
    assert moved.status_code == 200, moved.text
    assert control.load(workflow_id).payload.get("handoff") is None
    card = demo.snapshot(workflow_id).json()["components"][0]
    # Use the patient route and the action issued by the server.
    body = {
        "command_id": str(uuid4()),
        "workflow_state_version": 2,
        "instance_id": card["instance_id"],
        "action": card["action"],
        "value": {"consented": False},
    }
    refused = demo.client.post(
        f"/v1/workflows/{workflow_id}/actions", headers=demo.patient, json=body
    )
    assert refused.status_code == 422
    assert control.load(workflow_id).payload.get("handoff") is None
    body.update(command_id=str(uuid4()), value={"consented": True})
    accepted = demo.client.post(
        f"/v1/workflows/{workflow_id}/actions", headers=demo.patient, json=body
    )
    assert accepted.status_code == 200, accepted.text
    receipt = demo.snapshot(workflow_id).json()["components"]
    assert len(receipt) == 1
    assert receipt[0]["props"]["consent_required"] is False
    assert receipt[0]["props"]["title"] == "Your referral is queued for a coordinator"
    assert receipt[0]["action"] is None
    assert receipt[0]["instance_id"] != card["instance_id"]
    assert control.load(workflow_id).payload[SURFACE_PAYLOAD_KEY] == receipt
    replay = demo.client.post(
        f"/v1/workflows/{workflow_id}/actions", headers=demo.patient, json=body
    )
    assert replay.json()["handoff"] == accepted.json()["handoff"]
    assert replay.json()["replayed"] is True
    assert (
        control.load(workflow_id).payload["handoff"]["patient_visible_summary"]
        == draft["patient_visible_summary"]
    )
    assert control.bookings_for(workflow_id) == ()
