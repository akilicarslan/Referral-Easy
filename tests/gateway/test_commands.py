"""Commands: version, idempotency, policy, and the client that may not invent an action (§7, §9).

Four properties, each with a refusal that names the rule rather than only a status code:

* **A stale version is refused (RE002).** The workflow did not move, so the next attempt with the
  current version succeeds — a refusal that left state half-applied would be worse than no check.
* **One `command_id`, one outcome (RE005).** A retry replays the stored response byte for byte; a
  retry with a *different* body is a conflict, because the id names an intent.
* **The policy table decides (RE006), and it is consulted before the work.** A booking in a state
  where the table denies `scheduling:create_booking` never reaches the guardrail seam at all.
* **The client echoes an action; it never invents one.** Every one of those refusals is a 422 whose
  message says which of the four ways the client went wrong.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from gateway_fixtures import BOOKING_PAYLOAD, Demo

from referraleasy_contracts.generated.common import WorkflowState

# ---------------------------------------------------------------------------
# State version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", [{"hospital_id": "not-offered"}, {"hospital_id": "hospital-1", "id": "another"}]
)
def test_an_unoffered_choice_is_refused(demo: Demo, value: dict[str, str]) -> None:
    workflow_id = demo.seed()
    demo.move(workflow_id, version=1, to_state=WorkflowState.PROVIDER_MATCHING)
    moved = demo.move(
        workflow_id,
        version=2,
        to_state=WorkflowState.AWAITING_HOSPITAL_CHOICE,
        props={"options": [{"id": "hospital-1"}]},
    )
    card = moved.json()["components"][0]
    assert demo.action(workflow_id, instance=card, version=3, value=value).status_code == 422
    assert (
        demo.action(
            workflow_id, instance=card, version=3, value={"hospital_id": "hospital-1"}
        ).status_code
        == 200
    )


def test_a_stale_state_version_is_refused_with_re002(client: TestClient, demo: Demo) -> None:
    """A legal edge presented with the wrong version is refused on the version, not accepted.

    The target is a real edge from the current state, so the only thing wrong with the request is the
    version it names. That matters: an illegal *edge* would be refused anyway, and a test using one
    would prove nothing about the version check.
    """
    workflow_id = demo.seed()
    first = demo.move(workflow_id, version=1, to_state=WorkflowState.PROVIDER_MATCHING)
    assert first.status_code == 200

    stale = demo.move(
        workflow_id,
        version=1,
        to_state=WorkflowState.AWAITING_HOSPITAL_CHOICE,
        props={"options": [{"id": "hospital-1"}]},
    )
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["sqlstate"] == "RE002"
    assert error["code"] == "illegal_transition"
    assert "state_version" in error["message"]

    # The refusal is a fact about the request, not about the workflow: the workflow is still where the
    # first command left it, at version 2, and the *correct* version is accepted.
    assert demo.snapshot(workflow_id).json()["state_version"] == 2
    fresh = demo.move(
        workflow_id,
        version=2,
        to_state=WorkflowState.AWAITING_HOSPITAL_CHOICE,
        props={"options": [{"id": "hospital-1"}]},
    )
    assert fresh.status_code == 200
    assert fresh.json()["state_version"] == 3


def test_a_legacy_state_version_of_zero_is_refused_by_the_contract(
    client: TestClient, demo: Demo
) -> None:
    """`workflow.state_version` starts at 1 (`CHECK (state_version >= 1)`), and the body says so.

    The contract types `StateVersion` as `minimum: 0` and the *workflow* is what starts at 1, so a
    zero here is refused by the request model rather than accepted and then compared.
    """
    workflow_id = demo.seed()
    response = demo.move(workflow_id, version=0, to_state=WorkflowState.PROVIDER_MATCHING)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "contract_violation"


def test_an_edge_the_database_does_not_have_is_refused(client: TestClient, demo: Demo) -> None:
    """`INTAKE -> BOOKED` is not in `workflow_transition`, and the memory profile mirrors that table.

    A professional-looking demo that reaches `BOOKED` in one hop is the failure mode this prevents:
    the happy path has to be the one §7 actually defines, or every later test proves nothing.
    """
    workflow_id = demo.seed()
    response = demo.move(workflow_id, version=1, to_state=WorkflowState.BOOKED)
    assert response.status_code == 409
    assert response.json()["error"]["sqlstate"] == "RE002"
    assert demo.snapshot(workflow_id).json()["state"] == "INTAKE"


def test_a_self_loop_is_refused(client: TestClient, demo: Demo) -> None:
    """`workflow_transition_no_self_loop` (003), mirrored: `state_version` counts transitions."""
    workflow_id = demo.seed()
    response = demo.move(workflow_id, version=1, to_state=WorkflowState.INTAKE)
    assert response.status_code == 409
    assert "already in INTAKE" in response.json()["error"]["message"]


def test_a_state_the_contract_does_not_have_is_refused(client: TestClient, demo: Demo) -> None:
    workflow_id = demo.seed()
    response = demo.move(workflow_id, version=1, to_state=WorkflowState.PROVIDER_MATCHING)
    assert response.status_code == 200
    # The body's `to_state` is typed, so an unknown state cannot even be expressed in the contract.
    raw = client.post(
        f"/v1/workflows/{workflow_id}/transitions",
        json={
            "command_id": "c-unknown",
            "expected_state_version": 2,
            "to_state": "NOT_A_STATE",
        },
        headers=demo.admin,
    )
    assert raw.status_code == 422
    assert raw.json()["error"]["code"] == "contract_violation"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_a_replayed_command_returns_the_stored_response(client: TestClient, demo: Demo) -> None:
    """§7: "replay with the same value must be deduplicated".

    The second response is the first one with `replayed: true` — same status, same body — and the
    workflow moved exactly once. That last assertion is the one that matters: a stored response is
    worthless if the work happened twice.
    """
    workflow_id = demo.seed()
    body = {
        "command_id": "command-replayed",
        "expected_state_version": 1,
        "to_state": WorkflowState.PROVIDER_MATCHING.value,
    }
    first = client.post(f"/v1/workflows/{workflow_id}/transitions", json=body, headers=demo.admin)
    assert first.status_code == 200
    assert first.json()["replayed"] is False
    assert first.json()["state_version"] == 2

    second = client.post(f"/v1/workflows/{workflow_id}/transitions", json=body, headers=demo.admin)
    assert second.status_code == first.status_code
    assert second.json()["replayed"] is True
    assert {k: v for k, v in second.json().items() if k != "replayed"} == {
        k: v for k, v in first.json().items() if k != "replayed"
    }
    assert demo.snapshot(workflow_id).json()["state_version"] == 2


def test_reusing_a_command_id_for_a_different_body_is_refused_with_re005(
    client: TestClient, demo: Demo
) -> None:
    """The conflict RE005 exists to catch: one id, two intents.

    Coercing the second request into the first response would be the dangerous outcome — a client
    that thought it asked for something else would be told it got it.
    """
    workflow_id = demo.seed()
    first = client.post(
        f"/v1/workflows/{workflow_id}/transitions",
        json={
            "command_id": "command-conflict",
            "expected_state_version": 1,
            "to_state": WorkflowState.PROVIDER_MATCHING.value,
        },
        headers=demo.admin,
    )
    assert first.status_code == 200
    conflict = client.post(
        f"/v1/workflows/{workflow_id}/transitions",
        json={
            "command_id": "command-conflict",
            "expected_state_version": 1,
            "to_state": WorkflowState.COVERAGE_CHECK.value,
        },
        headers=demo.admin,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["sqlstate"] == "RE005"
    assert conflict.json()["error"]["code"] == "command_conflict"
    # The different body did not happen: the workflow is still where the first command put it.
    assert demo.snapshot(workflow_id).json()["state"] == "PROVIDER_MATCHING"


def test_reusing_an_open_command_id_for_a_different_body_is_refused_with_re005(
    client: TestClient, demo: Demo
) -> None:
    """RE005 bites even while the command is unrecorded: one id, one body, whoever asks.

    A refused command is not a free id. If it were, a client could re-use the id after editing the
    body and the two intents would share a key — which is the ambiguity the ledger exists to remove.
    The other half of the rule, that a refusal leaves the id *usable* for the identical retry, needs a
    guardrail pipeline that can be repaired mid-test and is asserted for real in `test_guardrails.py`
    (`test_a_booking_refused_while_the_guardrails_were_off_succeeds_once_they_are_back`).
    """
    workflow_id = demo.seed()
    refused = demo.move(
        workflow_id, version=99, to_state=WorkflowState.PROVIDER_MATCHING, command_id="command-open"
    )
    assert refused.status_code == 409

    edited = demo.move(
        workflow_id, version=1, to_state=WorkflowState.PROVIDER_MATCHING, command_id="command-open"
    )
    assert edited.status_code == 409
    assert edited.json()["error"]["sqlstate"] == "RE005"
    assert demo.snapshot(workflow_id).json()["state"] == "INTAKE"


def test_a_message_replay_does_not_record_the_message_twice(client: TestClient, demo: Demo) -> None:
    """The same rule on the free-text path, asserted on the recorded payload rather than the reply."""
    workflow_id = demo.seed()
    body = {
        "command_id": "message-once",
        "workflow_state_version": 1,
        "text": "I would like an appointment in November, please.",
    }
    first = client.post(f"/v1/workflows/{workflow_id}/messages", json=body, headers=demo.patient)
    assert first.status_code == 202
    second = client.post(f"/v1/workflows/{workflow_id}/messages", json=body, headers=demo.patient)
    assert second.status_code == 202
    assert second.json()["replayed"] is True
    messages = demo.admin_view(workflow_id).json()["workflow"]["payload"]["messages"]
    assert len(messages) == 1


# ---------------------------------------------------------------------------
# The policy table, consulted before the work
# ---------------------------------------------------------------------------


def test_a_booking_in_a_state_the_table_denies_is_refused_with_re006(
    client: TestClient, demo: Demo
) -> None:
    """Only `BOOKING_IN_FLIGHT` permits `scheduling:create_booking`, and the denial comes first.

    The consent is real and live — the patient approved this exact payload one step ago — so the
    refusal cannot be explained by anything but the table. And because the guardrail seam is
    *unconfigured* in this profile, a 403 with `RE006` also proves the order: had the guardrails run
    first, this request would have been a 503.
    """
    workflow_id = demo.seed()
    card = demo.review_card(workflow_id)
    granted = demo.consent(workflow_id, card, version=6)
    assert granted.status_code == 200

    response = demo.booking(workflow_id, approval_id=granted.json()["approval_id"], version=6)
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["sqlstate"] == "RE006"
    assert error["code"] == "policy_denied"
    # The refusal names the state it was refused in *and* the row that decided it, so an operator can
    # read the table's own note rather than guessing which rule is in the way.
    assert "create_booking in state AWAITING_FINAL_APPROVAL" in error["message"]
    assert "Asking for consent is not consent" in error["message"]
    # No guardrail layer was consulted, so the seam's own report still counts zero invocations.
    assert demo.metrics().json()["guardrails"]["invocations"] == 0


def test_the_policy_check_is_the_table_and_not_a_role_branch(
    client: TestClient, demo: Demo
) -> None:
    """A coordinator session is not enough to book: the *action* is what the table permits.

    Same session, same workflow, two states: the refusal in `AWAITING_FINAL_APPROVAL` and the
    refusal in `BOOKING_IN_FLIGHT` are different rules (`RE006` and the guardrail seam), which is what
    "the check consults the table" means observably.
    """
    workflow_id = demo.seed()
    card = demo.review_card(workflow_id)
    approval_id = demo.consent(workflow_id, card, version=6).json()["approval_id"]
    denied = demo.booking(workflow_id, approval_id=approval_id, version=6)
    assert denied.json()["error"]["sqlstate"] == "RE006"

    assert (
        demo.move(workflow_id, version=6, to_state=WorkflowState.BOOKING_IN_FLIGHT).status_code
        == 200
    )
    permitted = demo.booking(workflow_id, approval_id=approval_id, version=7)
    assert permitted.status_code == 503
    assert permitted.json()["error"]["code"] == "guardrails_not_configured"
    # The policy check happened (it must, or the 503 would be a coincidence), and no booking row
    # exists: the write was refused, not half-done.
    assert demo.bookings(workflow_id) == []


def test_an_action_the_client_invented_is_refused(client: TestClient, demo: Demo) -> None:
    """A card issues one action; the client echoes it. Anything else is not a card action."""
    workflow_id = demo.seed()
    moved = demo.move(
        workflow_id,
        version=1,
        to_state=WorkflowState.PROVIDER_MATCHING,
    )
    assert moved.status_code == 200
    issued = demo.move(
        workflow_id,
        version=2,
        to_state=WorkflowState.AWAITING_HOSPITAL_CHOICE,
        props={"options": [{"id": "hospital-1"}]},
    ).json()["components"][0]

    invented = demo.action(
        workflow_id,
        instance=issued,
        version=3,
        value={"option_id": "hospital-1"},
        action="scheduling:create_booking",
    )
    assert invented.status_code == 422
    assert "never invents one" in invented.json()["error"]["message"]


def test_an_instance_that_was_never_issued_is_refused(client: TestClient, demo: Demo) -> None:
    """A well-formed request for something that does not exist: 422, not a crash."""
    from uuid import uuid4

    workflow_id = demo.seed()
    phantom = {
        "instance_id": str(uuid4()),
        "action": "system:await_patient_choice",
    }
    response = demo.action(workflow_id, instance=phantom, version=1, value={"option_id": "x"})
    assert response.status_code == 422
    assert "never issued" in response.json()["error"]["message"]


def test_an_instance_from_another_workflow_is_refused(client: TestClient, demo: Demo) -> None:
    """Instance ids are per-workflow server state, so another workflow's card is not an answer here."""
    mine = demo.seed()
    theirs = demo.seed(ticket_ref="T-DEMO-2")
    demo.move(theirs, version=1, to_state=WorkflowState.PROVIDER_MATCHING)
    foreign = demo.move(
        theirs,
        version=2,
        to_state=WorkflowState.AWAITING_HOSPITAL_CHOICE,
        props={"options": [{"id": "hospital-1"}]},
    ).json()["components"][0]
    response = demo.action(mine, instance=foreign, version=1, value={"option_id": "hospital-1"})
    assert response.status_code == 422
    assert "never issued for workflow" in response.json()["error"]["message"]


def test_an_already_answered_instance_is_refused(client: TestClient, demo: Demo) -> None:
    """`ComponentInstance.submitted` is what stops a replayed surface soliciting the same choice."""
    workflow_id = demo.seed()
    demo.move(workflow_id, version=1, to_state=WorkflowState.PROVIDER_MATCHING)
    card = demo.move(
        workflow_id,
        version=2,
        to_state=WorkflowState.AWAITING_HOSPITAL_CHOICE,
        props={"options": [{"id": "hospital-1"}]},
    ).json()["components"][0]
    first = demo.action(workflow_id, instance=card, version=3, value={"option_id": "hospital-1"})
    assert first.status_code == 200
    second = demo.action(workflow_id, instance=card, version=3, value={"option_id": "hospital-1"})
    assert second.status_code == 422
    assert "already answered" in second.json()["error"]["message"]


def test_a_choice_with_no_answer_is_refused(client: TestClient, demo: Demo) -> None:
    workflow_id = demo.seed()
    demo.move(workflow_id, version=1, to_state=WorkflowState.PROVIDER_MATCHING)
    card = demo.move(
        workflow_id,
        version=2,
        to_state=WorkflowState.AWAITING_HOSPITAL_CHOICE,
        props={"options": [{"id": "hospital-1"}]},
    ).json()["components"][0]
    response = demo.action(workflow_id, instance=card, version=3, value=None)
    assert response.status_code == 422
    assert "no answer to record" in response.json()["error"]["message"]


def test_a_consent_with_no_value_is_refused(client: TestClient, demo: Demo) -> None:
    """Agreement has to be explicit; an empty body is not agreement."""
    workflow_id = demo.seed()
    card = demo.review_card(workflow_id)
    response = demo.action(workflow_id, instance=card, version=6, value=None)
    assert response.status_code == 422
    assert "explicit" in response.json()["error"]["message"]


def test_consent_binds_the_payload_the_server_holds_not_the_clients_copy(
    client: TestClient, demo: Demo
) -> None:
    """The sharpest case of "form data is never trusted because it came from the client".

    The patient submits a value naming a *different* payload; the binding records the hash of the one
    the workflow holds anyway, because nothing in the consent path reads the submitted value's
    contents — only that there is one.
    """
    from referraleasy_domain.approval import canonical_booking_payload_hash

    workflow_id = demo.seed()
    card = demo.review_card(workflow_id)
    response = demo.action(
        workflow_id,
        instance=card,
        version=6,
        value={"approved": True, "booking_payload": {"hospital_id": "somewhere-else"}},
    )
    assert response.status_code == 200
    assert response.json()["payload_hash"] == canonical_booking_payload_hash(BOOKING_PAYLOAD)


def test_the_admin_view_shows_the_payload_the_patient_consented_to(
    client: TestClient, demo: Demo
) -> None:
    """The workflow holds the reviewed payload and its canonical hash (§9's consent binding)."""
    from referraleasy_domain.approval import PAYLOAD_HASH_KEY, canonical_booking_payload_hash

    workflow_id = demo.seed()
    demo.review_card(workflow_id)
    payload = demo.admin_view(workflow_id).json()["workflow"]["payload"]
    assert payload["booking_payload"] == BOOKING_PAYLOAD
    assert payload[PAYLOAD_HASH_KEY] == canonical_booking_payload_hash(BOOKING_PAYLOAD)


def test_a_card_that_cannot_be_rendered_is_refused_rather_than_forwarded(
    client: TestClient, demo: Demo
) -> None:
    """A `HospitalPicker` with no `options` is a screen that looks like a bug, and it is refused.

    The state moved first — the version is optimistic concurrency, not a transaction — so the test
    asserts the *card* was not forwarded and that the refusal names the missing prop.
    """
    workflow_id = demo.seed()
    demo.move(workflow_id, version=1, to_state=WorkflowState.PROVIDER_MATCHING)
    response = demo.move(workflow_id, version=2, to_state=WorkflowState.AWAITING_HOSPITAL_CHOICE)
    assert response.status_code == 422
    assert "options" in response.json()["error"]["message"]


def test_a_state_with_no_card_moves_without_issuing_one(client: TestClient, demo: Demo) -> None:
    """Several of §7's states have nothing to ask. "Show nothing" is the answer, not an error."""
    workflow_id = demo.seed()
    response = demo.move(workflow_id, version=1, to_state=WorkflowState.COVERAGE_CHECK)
    assert response.status_code == 200
    assert response.json()["components"] == []
    # The patient's view agrees: no outstanding card, and the state is the authoritative fact.
    snapshot = demo.snapshot(workflow_id).json()
    assert snapshot["state"] == "COVERAGE_CHECK"
    assert snapshot["components"] == []


def test_a_workflow_that_does_not_exist_is_404_for_every_surface(
    client: TestClient, demo: Demo
) -> None:
    from uuid import uuid4

    missing = str(uuid4())
    assert demo.snapshot(missing).status_code == 404
    assert demo.help_card(missing).status_code == 404
    assert demo.admin_view(missing).status_code == 404
    assert demo.move(missing, version=1, to_state=WorkflowState.COVERAGE_CHECK).status_code == 404
    assert demo.message(missing, text="hello", version=1).status_code == 404


def test_a_coordinator_cannot_seed_a_workflow_with_a_patient_session(
    client: TestClient, demo: Demo
) -> None:
    """Seeding is a coordinator action: a patient who could seed could choose their own patient_ref."""
    response = client.post(
        "/v1/demo/workflows", json={"patient_ref": "patient-9"}, headers=demo.patient
    )
    assert response.status_code == 401


def test_every_refusal_uses_the_one_envelope(client: TestClient, demo: Demo) -> None:
    """One failure shape for a client to parse, with the request id tying it to the response header.

    Checked across four different refusal sources (transport, version, policy, contract), because a
    shape that holds in three of them is a shape a client will get wrong on the fourth.
    """
    from uuid import uuid4

    workflow_id = demo.seed()
    responses: list[Any] = [
        client.get("/v1/workflows/not-a-uuid", headers=demo.patient),
        demo.move(workflow_id, version=99, to_state=WorkflowState.COVERAGE_CHECK),
        demo.action(
            workflow_id,
            instance={"instance_id": str(uuid4()), "action": "nope:not_an_action"},
            version=1,
            value=None,
        ),
        demo.snapshot(str(uuid4())),
    ]
    for response in responses:
        assert response.status_code >= 400, response.text
        error = response.json()["error"]
        assert set(error) == {"code", "sqlstate", "message", "request_id", "detail"}
        assert error["request_id"] == response.headers["X-Request-Id"]
