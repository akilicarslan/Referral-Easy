"""The catalog, the instances, and the two rules that make a card answerable (§12, §9).

§12 fixes the surface as eight components and one rule with teeth behind them: *"tekrar render tekrar
booking yaratmaz"* — re-rendering does not create a second booking. That rule is only true if an
instance id is a function of the slot rather than of the emission, so the ids here are computed and
compared rather than assumed, over the wire and inside the registry.

The second rule is §9's, and it is the reason this module exists at all: **the client echoes an action
the server issued; it never invents one.** So every planned action is checked against the policy table
(they are two tables of the same fact and must not drift), the snapshot only ever offers cards that
carry an action, and the action a client submits has to be the one on the instance it names.

One ordering is pinned here too: a card that cannot be rendered refuses the whole transition, and the
workflow does not move. The refusal and the move are two steps, and a refusal that has already
advanced the state leaves the patient in a state that shows nothing with no way to answer it.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from gateway_fixtures import Demo

from referraleasy_contracts.generated.common import WorkflowState
from referraleasy_contracts.generated.sse import RenderableComponent
from referraleasy_gateway.events import AUTHORITATIVE_EVENT_TYPES
from referraleasy_gateway.policy import TablePolicy
from referraleasy_gateway.surfaces import (
    CATALOG,
    HELP_CARD_COMPONENT,
    AgentDecision,
    PartialOutputError,
    instance_id_for,
    workflow_state_of,
)

#: The §7 states that are work happening with nothing to ask. Written out rather than derived, so a
#: change to the catalog has to argue with this list rather than silently join it.
CARDLESS_STATES = (
    WorkflowState.INTAKE,
    WorkflowState.COVERAGE_CHECK,
    WorkflowState.PROVIDER_MATCHING,
    WorkflowState.READINESS_CHECK,
    WorkflowState.BOOKING_IN_FLIGHT,
    WorkflowState.ABANDONED,
    WorkflowState.CANCELLED,
    WorkflowState.FAILED,
)

HOSPITAL_OPTIONS = {"options": [{"id": "hospital-1"}, {"id": "hospital-2"}]}
DOCTOR_OPTIONS = {"options": [{"id": "doctor-1"}]}
SLOT_OPTIONS = {"options": [{"id": "slot-1"}]}


def _frames(response: Any) -> list[dict[str, Any]]:
    """The `data:` documents of a stream response, parsed. The `id:` line is `test_stream.py`'s."""
    return [
        json.loads(line[len("data: ") :])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def _advance(
    demo: Demo, workflow_id: str, state: WorkflowState, props: dict[str, Any] | None = None
):
    """One coordinator move at the version the workflow is actually at, asserted accepted.

    The version is read from the server rather than counted in the test: the state machine's edges are
    not this module's subject, and a test that has to be right about how many moves preceded it is a
    test that fails when a path changes for unrelated reasons.
    """
    version = demo.snapshot(workflow_id).json()["state_version"]
    response = demo.move(workflow_id, version=version, to_state=state, props=props)
    assert response.status_code == 200, response.text
    return response


def _to_picker(demo: Demo, workflow_id: str, target: WorkflowState) -> dict[str, Any]:
    """Walk a seeded workflow to `target` and return the card the snapshot shows there.

    `INTAKE → AWAITING_HOSPITAL_CHOICE` is not an edge — verification comes first (§7) — so every path
    to a picker goes through `PROVIDER_MATCHING`, which is also where a real coordinator would be.
    """
    _advance(demo, workflow_id, WorkflowState.PROVIDER_MATCHING)
    _advance(demo, workflow_id, WorkflowState.AWAITING_HOSPITAL_CHOICE, HOSPITAL_OPTIONS)
    if target is not WorkflowState.AWAITING_HOSPITAL_CHOICE:
        _advance(demo, workflow_id, WorkflowState.AWAITING_DOCTOR_CHOICE, DOCTOR_OPTIONS)
    if target is WorkflowState.AWAITING_SLOT_CHOICE:
        _advance(demo, workflow_id, WorkflowState.AWAITING_SLOT_CHOICE, SLOT_OPTIONS)
    components = demo.snapshot(workflow_id).json()["components"]
    assert len(components) == 1, components
    assert components[0]["component"] == CATALOG[target].component.value
    return dict(components[0])


# ---------------------------------------------------------------------------
# The catalog is the policy table's other half
# ---------------------------------------------------------------------------


def test_every_planned_action_is_permitted_where_it_is_issued() -> None:
    """The card's action and the policy row are the same fact, so they are compared.

    A catalog that offered `system:capture_consent` in a state whose row denies it would render a
    button the server refuses — a card the patient cannot answer, which is the thing this phase treats
    as a bug rather than as a validation error. Both directions matter: the action has to be permitted
    where the card is issued, and it must not be permitted everywhere (a permission that is always
    true is not a policy).
    """
    table = TablePolicy.from_seed()
    checked = 0
    for state, plan in CATALOG.items():
        if plan.action is None:
            continue
        assert table.is_allowed(plan.action, state.value) is True, (
            f"{state.value}'s card offers {plan.action}, which the policy table denies there"
        )
        permitted = {s for s in WorkflowState if table.is_allowed(plan.action, s.value)}
        assert state in permitted and len(permitted) < len(WorkflowState), (
            f"{plan.action} is permitted in every state, so `is_allowed` is not answering the "
            "question this test asks"
        )
        checked += 1
    assert checked == len([plan for plan in CATALOG.values() if plan.action is not None]) == 5


def test_a_card_that_asks_nothing_carries_no_action() -> None:
    """Three cards are issued *to* the patient and ask them nothing. An action on one would be a
    button with no question behind it, and — through `outstanding` — would make a receipt an
    unanswered question that a snapshot keeps offering and a client keeps trying to answer."""
    actionless = {state: plan for state, plan in CATALOG.items() if plan.action is None}
    assert set(actionless) == {
        WorkflowState.BOOKED,
        WorkflowState.RECOVERY,
        WorkflowState.HUMAN_HANDOFF,
    }
    for state, plan in actionless.items():
        assert plan.component in {
            RenderableComponent.AppointmentReceipt,
            RenderableComponent.DirectoryFallbackCard,
            RenderableComponent.HumanHandoffCard,
        }, f"{state.value} shows {plan.component.value} with no action"


def test_the_catalog_uses_the_contract_s_eight_components_and_names_them_back() -> None:
    """§12's list is closed at eight, and every card in it reads back to the state that shows it.

    The count is asserted rather than assumed: a ninth component is a new UI system for a feature,
    which §12 forbids — so the change should fail here and force the docstring in `surfaces.py` to be
    re-read, not slip through as an enum member nobody compares against a migration.
    """
    assert len(RenderableComponent) == 8, "§12 fixes the catalog at eight components"

    used = {plan.component for plan in CATALOG.values()}
    assert used <= set(RenderableComponent)
    assert RenderableComponent.PatientDetailsForm not in used, (
        "PatientDetailsForm is one of the eight and no state in this phase's state machine shows it; "
        "if that changed, this test is where it is noticed"
    )
    for component in used:
        state = workflow_state_of(component)
        assert state is not None and CATALOG[state].component is component
    # The static help card is the handoff card shown outside the catalog: it belongs to a state, and
    # it is served from `help_card` rather than issued by the registry (§9).
    assert HELP_CARD_COMPONENT is RenderableComponent.HumanHandoffCard
    assert workflow_state_of(HELP_CARD_COMPONENT) is WorkflowState.HUMAN_HANDOFF


def test_the_cardless_states_are_exactly_the_ones_the_catalog_omits() -> None:
    """Every state has a card or is named here. A state that quietly fell out of the catalog would
    look like "nothing to ask" and be indistinguishable from a card nobody can render."""
    assert set(CATALOG) | set(CARDLESS_STATES) == set(WorkflowState)
    assert not set(CATALOG) & set(CARDLESS_STATES)


# ---------------------------------------------------------------------------
# Over the wire
# ---------------------------------------------------------------------------


def test_a_card_that_cannot_be_rendered_is_refused_before_the_workflow_moves(
    client: TestClient, demo: Demo
) -> None:
    """The refusal is total. This is the ordering, not just the 422.

    `HospitalPicker` needs `options`; a coordinator that omits them gets a refusal. What the state is
    after that refusal is the substance of the test: a workflow that had already advanced would be
    sitting in a state whose card cannot be built, showing the patient nothing and offering no way
    forward, with the transition already burned.

    The stream is asserted on as well, and only for the events that carry state: the refused turn
    still reports that it ran (`agent_progress`, §12's "show system status"), and it must not have
    moved anything. The two are different facts and only the second one is a bug.
    """
    workflow_id = demo.seed()
    _advance(demo, workflow_id, WorkflowState.PROVIDER_MATCHING)
    before = demo.snapshot(workflow_id).json()["last_event_id"]

    response = demo.move(workflow_id, version=2, to_state=WorkflowState.AWAITING_HOSPITAL_CHOICE)
    assert response.status_code == 422, response.text
    assert "HospitalPicker needs ['options']" in response.json()["error"]["message"]

    snapshot = demo.snapshot(workflow_id).json()
    assert (snapshot["state"], snapshot["state_version"]) == ("PROVIDER_MATCHING", 2)
    assert snapshot["components"] == []

    after = _frames(demo.events(workflow_id, last_event_id=before))
    assert [frame["type"] for frame in after] == ["agent_progress"], after
    assert not [frame for frame in after if frame["type"] in AUTHORITATIVE_EVENT_TYPES]


def test_a_refused_card_leaves_a_retry_able_to_succeed(client: TestClient, demo: Demo) -> None:
    """The consequence of that ordering: the same move with the props supplied works afterwards.

    A refusal that had moved the workflow would make this a `409` about a stale version — the client's
    request was fine and the state under it had changed, which is the failure mode the ordering exists
    to prevent.
    """
    workflow_id = demo.seed()
    _advance(demo, workflow_id, WorkflowState.PROVIDER_MATCHING)
    assert (
        demo.move(
            workflow_id, version=2, to_state=WorkflowState.AWAITING_HOSPITAL_CHOICE
        ).status_code
        == 422
    )

    accepted = demo.move(
        workflow_id,
        version=2,
        to_state=WorkflowState.AWAITING_HOSPITAL_CHOICE,
        props=HOSPITAL_OPTIONS,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["state_version"] == 3


def test_an_impossible_target_is_not_reported_as_a_props_problem(
    client: TestClient, demo: Demo
) -> None:
    """The order of the two pre-flight refusals, asked as a client would experience it.

    `INTAKE → BOOKED` is not an edge, and `BOOKED`'s receipt needs an `appointment` prop that this
    request does not carry. Both are true, so the order decides what the client is told — and only one
    order is useful: "the props are wrong" invites a retry that cannot succeed, while "there is no such
    edge" is the whole answer. The state machine's refusals come first, and the props question is not
    even reached.
    """
    workflow_id = demo.seed()
    response = demo.move(workflow_id, version=1, to_state=WorkflowState.BOOKED)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["sqlstate"] == "RE002"
    assert "is not an edge in workflow_transition" in response.json()["error"]["message"]
    assert "AppointmentReceipt" not in response.text

    # A stale version likewise, on an edge that *is* legal: the card's missing props must not mask the
    # fact that the caller's view of the workflow is behind (§7), which is the refusal to act on first.
    _advance(demo, workflow_id, WorkflowState.PROVIDER_MATCHING)
    stale = demo.move(workflow_id, version=99, to_state=WorkflowState.AWAITING_HOSPITAL_CHOICE)
    assert stale.status_code == 409, stale.text
    assert "state_version" in stale.json()["error"]["message"]
    assert "HospitalPicker" not in stale.text
    assert demo.snapshot(workflow_id).json()["state_version"] == 2


def test_a_cardless_state_shows_nothing_and_offers_no_action(
    client: TestClient, demo: Demo
) -> None:
    """`PROVIDER_MATCHING` is work happening. The honest render is an empty surface, not a card."""
    workflow_id = demo.seed()
    moved = demo.move(workflow_id, version=1, to_state=WorkflowState.PROVIDER_MATCHING)
    assert moved.status_code == 200, moved.text
    assert moved.json()["components"] == []

    snapshot = demo.snapshot(workflow_id).json()
    assert snapshot["state"] == "PROVIDER_MATCHING"
    assert snapshot["components"] == []
    assert demo.admin_view(workflow_id).json()["outstanding_components"] == []

    # The empty `surface_update` is the answer, not noise: it is how the client is told the surface is
    # now empty, and it is the only frame that says so for a state that never had a card. There is no
    # `terminal` here either — a transition is not the end of the workflow, and a client that read one
    # as an ending would close the session in the middle of the flow (§12).
    frames = _frames(demo.events(workflow_id))
    assert [frame["type"] for frame in frames] == [
        "agent_progress",
        "workflow_state",
        "surface_update",
    ]
    surface = next(frame for frame in frames if frame["type"] == "surface_update")["payload"]
    assert surface["components"] == []
    assert "remove_instance_ids" not in surface, (
        "nothing was superseded on the way into a cardless state"
    )


def test_the_snapshot_offers_the_card_the_state_shows_and_the_server_issued_it(
    client: TestClient, demo: Demo
) -> None:
    """What a snapshot promises: the outstanding card, its action, and a deterministic id.

    The id is recomputed here from `(workflow, component, slot)`, which is §12's "re-rendering does not
    create a second booking" in its checkable form: a client that reloads gets the same instance, so
    the answer it sends is about this question and not about a new one.
    """
    workflow_id = demo.seed()
    card = _to_picker(demo, workflow_id, WorkflowState.AWAITING_HOSPITAL_CHOICE)

    assert card["component"] == "HospitalPicker"
    assert card["action"] == "system:await_patient_choice"
    assert card["submitted"] is False
    assert card["workflow_state_version"] == 3
    assert card["props"] == HOSPITAL_OPTIONS
    assert UUID(card["instance_id"]) == instance_id_for(
        UUID(workflow_id), RenderableComponent.HospitalPicker, "primary"
    )
    # A second read is the same read: no new instance, no second question.
    assert demo.snapshot(workflow_id).json()["components"] == [card]


def test_a_superseded_card_is_removed_and_cannot_be_answered(
    client: TestClient, demo: Demo
) -> None:
    """The move that answers a question closes it — on the wire and in the registry.

    Two separate facts, because they fail separately: the `surface_update` carries the dead instance id
    so the client drops it, and the instance is marked submitted so a client that kept it cannot
    submit yesterday's answer against today's state. The second is the one that matters, since only
    one of the two sides is under our control.
    """
    workflow_id = demo.seed()
    stale = _to_picker(demo, workflow_id, WorkflowState.AWAITING_HOSPITAL_CHOICE)

    _advance(demo, workflow_id, WorkflowState.AWAITING_DOCTOR_CHOICE, DOCTOR_OPTIONS)
    after = demo.snapshot(workflow_id).json()
    doctor = after["components"][0]
    assert doctor["component"] == "DoctorPicker"

    removals = [
        frame["payload"]
        for frame in _frames(demo.events(workflow_id))
        if "remove_instance_ids" in frame["payload"]
    ]
    assert len(removals) == 1, removals
    assert removals[0]["remove_instance_ids"] == [stale["instance_id"]]

    # A removal frame cannot also carry the removed cards: A2UI applies additions after removals.
    carried = {component["instance_id"]: component for component in removals[0]["components"]}
    assert carried == {}, "a removal frame must not re-add the same instances"
    assert doctor["instance_id"] not in removals[0]["remove_instance_ids"]

    replay = demo.action(
        workflow_id, instance=stale, version=after["state_version"], value={"id": "hospital-1"}
    )
    assert replay.status_code == 422, replay.text
    assert "was already answered" in replay.json()["error"]["message"]
    assert "must not solicit the same choice twice" in replay.json()["error"]["message"]


def test_the_client_must_echo_the_action_the_server_issued(client: TestClient, demo: Demo) -> None:
    """§9's "the client echoes an action, it never invents one", as a refusal.

    The instance is real and its version is current, so the only thing wrong is the action — and that
    is enough. Without this check a `SlotPicker` could be answered with `system:capture_consent`, which
    would turn a picker into a consent record.
    """
    workflow_id = demo.seed()
    card = _to_picker(demo, workflow_id, WorkflowState.AWAITING_SLOT_CHOICE)
    assert card["action"] == "system:await_patient_choice"

    invented = demo.action(
        workflow_id,
        instance=card,
        version=card["workflow_state_version"],
        value={"id": "slot-1"},
        action="system:capture_consent",
    )
    assert invented.status_code == 422, invented.text
    assert "it never invents one" in invented.json()["error"]["message"]
    # The action is refused *before* the work, so nothing was recorded.
    assert "answer" not in demo.admin_view(workflow_id).json()["workflow"]["payload"]
    assert demo.snapshot(workflow_id).json()["components"][0]["submitted"] is False


def test_an_instance_the_server_never_issued_is_refused(client: TestClient, demo: Demo) -> None:
    """The other half: an id that is not in the surface came from somewhere else.

    Well-formed and nonexistent, so it is a contract violation (422) rather than a crash or a silent
    no-op — the surface is server-side state and a client cannot add to it by asking.
    """
    workflow_id = demo.seed()
    card = _to_picker(demo, workflow_id, WorkflowState.AWAITING_SLOT_CHOICE)
    elsewhere = instance_id_for(UUID(workflow_id), RenderableComponent.SlotPicker, "another-slot")

    response = demo.client.post(
        f"/v1/workflows/{workflow_id}/actions",
        json={
            "command_id": str(uuid4()),
            "instance_id": str(elsewhere),
            "action": "system:await_patient_choice",
            "workflow_state_version": card["workflow_state_version"],
            "value": {"id": "slot-1"},
        },
        headers=demo.patient,
    )
    assert response.status_code == 422, response.text
    assert "was never issued for workflow" in response.json()["error"]["message"]


def test_an_instance_id_is_a_function_of_the_slot_and_not_of_the_emission() -> None:
    """§12's "re-rendering does not create a second booking", at the level where it is decided.

    Same workflow, same card, same slot: one id, so a re-issue replaces. Different workflow, different
    component, or a different slot: three different ids, so two questions never collide.
    """
    workflow_id = UUID(int=5)
    component = RenderableComponent.HospitalPicker
    assert instance_id_for(workflow_id, component, "primary") == instance_id_for(
        workflow_id, component, "primary"
    )
    assert instance_id_for(workflow_id, component, "primary") != instance_id_for(
        UUID(int=6), component, "primary"
    )
    assert instance_id_for(workflow_id, component, "primary") != instance_id_for(
        workflow_id, RenderableComponent.DoctorPicker, "primary"
    )
    assert instance_id_for(workflow_id, component, "primary") != instance_id_for(
        workflow_id, component, "recovery"
    )


def test_an_unfinished_decision_cannot_supply_card_props() -> None:
    with pytest.raises(PartialOutputError):
        AgentDecision(
            complete=False, to_state=WorkflowState.AWAITING_HOSPITAL_CHOICE.value
        ).commit()
