"""Consent, and the five things it is tied to (§9).

§9's sentence is that a booking requires "a live approval bound to the payload". Read strictly it is
five requirements, and each one is a way a booking can be attempted with something that is not
consent:

1. **patient** — the approval is the patient's, not the coordinator's and not another patient's;
2. **workflow** — consent for one referral never authorises another;
3. **state_version** — the version the patient answered *at*, so a workflow that moved on before the
   write cannot spend a stale answer;
4. **payload hash** — the hash of what the patient was shown, by the domain's one canonical encoder;
5. **expiry** — consent is a moment, not a permanent permission.

The version clause is the one that needs saying out loud, because the naive reading is wrong: consent
is captured in `AWAITING_FINAL_APPROVAL`, and the workflow has to *move* to `BOOKING_IN_FLIGHT` before
the policy table permits a booking at all. So the version a live approval names is always one behind
the workflow's current one. Migration 014's `workflow_booking_consent_guard` is the authority — it
compares against `OLD.state_version` as the workflow crosses the edge — and the gateway's booking gate
mirrors it. Comparing against the current version instead refuses every booking, which is what this
module's first test pins by naming both answers.
"""

from __future__ import annotations

import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from gateway_fixtures import BOOKING_PAYLOAD, Demo

from referraleasy_contracts.generated.common import WorkflowState
from referraleasy_domain.approval import (
    PAYLOAD_HASH_KEY,
    PAYLOAD_HASH_PATTERN,
    canonical_booking_payload_hash,
)
from referraleasy_domain.errors import ApprovalRefusedError
from referraleasy_gateway.approval import (
    DEFAULT_APPROVAL_TTL_SECONDS,
    ApprovalBinding,
    ApprovalService,
    InMemoryApprovalLedger,
    payload_hash_of,
)
from referraleasy_gateway.commands import BOOKING_PAYLOAD_KEY
from referraleasy_gateway.timing import ManualClock


@pytest.mark.parametrize(
    "value",
    [
        {"approved": False},
        {"consented": False},
        {"other": True},
        {"approved": "true"},
        {"consented": False, "approved": True},
    ],
)
def test_consent_requires_an_explicit_true_answer(demo: Demo, value: dict[str, object]) -> None:
    workflow_id = demo.seed()
    card = demo.review_card(workflow_id)
    response = demo.action(workflow_id, instance=card, version=6, value=value)
    assert response.status_code == 422, response.text
    assert "explicit" in response.json()["error"]["message"]
    # A refusal must leave the question answerable.
    assert demo.consent(workflow_id, card, version=6).status_code == 200


def _service(
    clock: ManualClock, *, ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS
) -> ApprovalService:
    return ApprovalService(InMemoryApprovalLedger(), clock=clock, ttl_seconds=ttl_seconds)


# ---------------------------------------------------------------------------
# The hash
# ---------------------------------------------------------------------------


def test_the_hash_the_gateway_binds_is_the_domain_s_canonical_one() -> None:
    """There is exactly one encoder for the payload hash, and it is `packages/domain`'s.

    A second encoder is how "the patient approved something else" happens: two functions that agree on
    a flat dictionary and disagree on key order or on a nested value would produce two hashes for one
    payload, and the booking gate would refuse consent the patient really gave — or, worse, accept
    consent for a payload they were not shown.
    """
    for payload in (
        BOOKING_PAYLOAD,
        dict(reversed(list(BOOKING_PAYLOAD.items()))),  # same mapping, different key order
        {"outer": {"inner": [1, 2, {"deep": True}]}, "slot_id": "slot-1"},
        {},
    ):
        assert payload_hash_of(payload) == canonical_booking_payload_hash(payload)
        assert re.fullmatch(PAYLOAD_HASH_PATTERN, payload_hash_of(payload))

    # And it is sensitive to the value, which is the property the whole gate rests on.
    assert payload_hash_of(BOOKING_PAYLOAD) != payload_hash_of(
        {**BOOKING_PAYLOAD, "slot_id": "slot-2"}
    )


def test_the_binding_records_the_domain_s_hash_and_the_workflow_s_version(
    clock: ManualClock,
) -> None:
    """`bind` stores a hash, not a payload: the gateway compares hashes and never re-derives consent."""
    binding = _service(clock).bind(
        workflow_id=uuid4(),
        patient_ref="patient-1",
        state_version=6,
        payload=BOOKING_PAYLOAD,
    )
    assert binding.payload_hash == canonical_booking_payload_hash(BOOKING_PAYLOAD)
    assert binding.state_version == 6
    assert binding.consumed_at is None
    assert binding.is_live(clock.now())
    assert not binding.is_consumed


# ---------------------------------------------------------------------------
# The version clause
# ---------------------------------------------------------------------------


def test_consent_is_checked_against_the_version_it_was_given_at(clock: ManualClock) -> None:
    """A live consent authorises the booking one transition later, and only one.

    Both directions, because either alone would pass for the wrong rule: `state_version=6` must be
    accepted (the version the patient answered at) and `state_version=7` — the version the workflow is
    *at* when the write happens — must be refused by the same binding. A gate that passed the current
    version would refuse every real booking; one that ignored the version entirely would let a
    workflow that moved on spend an answer given before it moved.
    """
    workflow_id = uuid4()
    service = _service(clock)
    granted = service.bind(
        workflow_id=workflow_id,
        patient_ref="patient-1",
        state_version=6,
        payload=BOOKING_PAYLOAD,
    )

    consenting = service.authorise(
        workflow_id=workflow_id,
        patient_ref="patient-1",
        state_version=granted.state_version,
        payload=BOOKING_PAYLOAD,
    )
    assert consenting.approval_id == granted.approval_id

    with pytest.raises(ApprovalRefusedError) as stale:
        service.authorise(
            workflow_id=workflow_id,
            patient_ref="patient-1",
            state_version=7,
            payload=BOOKING_PAYLOAD,
        )
    message = str(stale.value)
    assert "was given at state_version 6" in message
    assert "the workflow moved on after the patient answered" in message


def test_the_booking_gate_uses_the_entry_version_on_the_wire(
    configured_client: TestClient, configured_demo: Demo
) -> None:
    """The same clause, one layer up: the route books at version 7 with consent given at 6.

    This is the end-to-end form of the test above, and it is the one that fails if someone "fixes" the
    gate by passing the workflow's current version: reaching `BOOKING_IN_FLIGHT` is what makes the
    booking permitted, so the two versions are never equal at the moment of the write.
    """
    workflow_id = configured_demo.seed()
    approval_id = configured_demo.in_booking_flight(workflow_id)
    snapshot = configured_demo.snapshot(workflow_id).json()
    assert (snapshot["state"], snapshot["state_version"]) == ("BOOKING_IN_FLIGHT", 7)

    response = configured_demo.booking(workflow_id, approval_id=approval_id, version=7)
    assert response.status_code == 200, response.text
    assert response.json()["approval_id"] == approval_id


# ---------------------------------------------------------------------------
# The other four clauses
# ---------------------------------------------------------------------------


def test_each_of_the_five_parts_is_necessary(clock: ManualClock) -> None:
    """The conjunction, one clause at a time, on the value that holds the rule.

    `ApprovalBinding.authorises` is the five-part check as a single expression, so it can be asked
    about exactly one clause at a time — which is the only way to show that each of the five is doing
    work rather than being carried along by the others.
    """
    workflow_id = uuid4()
    binding = _service(clock).bind(
        workflow_id=workflow_id,
        patient_ref="patient-1",
        state_version=6,
        payload=BOOKING_PAYLOAD,
    )
    now = clock.now()
    matches = {
        "workflow_id": workflow_id,
        "patient_ref": "patient-1",
        "state_version": 6,
        "payload": BOOKING_PAYLOAD,
        "now": now,
    }
    assert binding.authorises(**matches) is True

    for clause, wrong in (
        ("patient", {"patient_ref": "patient-2"}),
        ("workflow", {"workflow_id": uuid4()}),
        ("version", {"state_version": 5}),
        ("payload", {"payload": {**BOOKING_PAYLOAD, "doctor_id": "doctor-2"}}),
        ("expiry", {"now": binding.expires_at}),
    ):
        assert binding.authorises(**{**matches, **wrong}) is False, (
            f"the {clause} clause is not checked"
        )


def test_consent_for_one_workflow_cannot_book_another(clock: ManualClock) -> None:
    """Workflow scope is checked before the binding is even considered, and the refusal says so."""
    service = _service(clock)
    mine = service.bind(
        workflow_id=uuid4(), patient_ref="patient-1", state_version=6, payload=BOOKING_PAYLOAD
    )
    other = uuid4()

    with pytest.raises(ApprovalRefusedError) as refusal:
        service.authorise(
            workflow_id=other,
            patient_ref="patient-1",
            state_version=6,
            payload=BOOKING_PAYLOAD,
        )
    assert "no approval exists for workflow" in str(refusal.value)
    assert "asking for consent is not consent" in str(refusal.value)

    # The same id asked about the workflow it was granted for still works: the refusal was about
    # scope, not about the binding having gone anywhere.
    assert (
        service.authorise(
            workflow_id=mine.workflow_id,
            patient_ref="patient-1",
            state_version=6,
            payload=BOOKING_PAYLOAD,
        ).approval_id
        == mine.approval_id
    )


def test_a_consent_bound_to_another_patient_is_not_mine(clock: ManualClock) -> None:
    """The patient clause: a binding that names someone else is invisible, not merely refused."""
    service = _service(clock)
    workflow_id = uuid4()
    service.bind(
        workflow_id=workflow_id, patient_ref="patient-2", state_version=6, payload=BOOKING_PAYLOAD
    )

    with pytest.raises(ApprovalRefusedError) as refusal:
        service.authorise(
            workflow_id=workflow_id,
            patient_ref="patient-1",
            state_version=6,
            payload=BOOKING_PAYLOAD,
        )
    assert "no approval exists for workflow" in str(refusal.value)


def test_a_payload_that_hashes_differently_is_a_different_question(clock: ManualClock) -> None:
    """Changing the appointment after consent invalidates the consent, and the message says which."""
    workflow_id = uuid4()
    service = _service(clock)
    granted = service.bind(
        workflow_id=workflow_id,
        patient_ref="patient-1",
        state_version=6,
        payload=BOOKING_PAYLOAD,
    )

    with pytest.raises(ApprovalRefusedError) as refusal:
        service.authorise(
            workflow_id=workflow_id,
            patient_ref="patient-1",
            state_version=6,
            payload={**BOOKING_PAYLOAD, "starts_at": "2026-11-02T15:00:00+00:00"},
        )
    message = str(refusal.value)
    assert granted.payload_hash[:12] in message
    assert "changing the payload invalidates consent" in message


def test_an_expired_approval_is_refused_with_its_deadline(clock: ManualClock) -> None:
    """Expiry is the injected clock's, so this is a fact about the rule and not about a sleep."""
    workflow_id = uuid4()
    service = _service(clock, ttl_seconds=60)
    granted = service.bind(
        workflow_id=workflow_id,
        patient_ref="patient-1",
        state_version=6,
        payload=BOOKING_PAYLOAD,
    )

    clock.advance_ms(59_000)
    assert (
        service.authorise(
            workflow_id=workflow_id,
            patient_ref="patient-1",
            state_version=6,
            payload=BOOKING_PAYLOAD,
        ).approval_id
        == granted.approval_id
    ), "consent is live one second before it expires"

    clock.advance_ms(2_000)
    with pytest.raises(ApprovalRefusedError) as refusal:
        service.authorise(
            workflow_id=workflow_id,
            patient_ref="patient-1",
            state_version=6,
            payload=BOOKING_PAYLOAD,
        )
    assert "expired at" in str(refusal.value)
    assert granted.expires_at.isoformat() in str(refusal.value)


@pytest.mark.memory_only  # the schema refuses this; see test_postgres_control_state.py
def test_an_expired_consent_is_refused_on_the_wire_before_the_row_is_written(
    configured_client: TestClient, configured_demo: Demo, clock: ManualClock
) -> None:
    """The same rule over HTTP, where the order of the gates is observable: nothing is written.

    The clock is the app's own (`conftest`'s `clock` fixture), so the window is moved rather than
    waited out — which is also the only way this test could be honest about a 15-minute rule.

    Memory profile only. §9's expiry is decided by the *database's* `now()`, and the gateway's
    test clock starts on 2026-01-01 — months behind the server — so advancing it here cannot
    expire a consent `PostgresApprovalLedger` has already had stamped as live. Moving the
    clock is the honest way to test a 15-minute window; it is also memory-only, and that is a
    finding rather than a convenience. See `test_postgres_control_state.py`.
    """
    workflow_id = configured_demo.seed()
    approval_id = configured_demo.in_booking_flight(workflow_id)

    clock.advance_ms(DEFAULT_APPROVAL_TTL_SECONDS * 1000 + 1_000)
    response = configured_demo.booking(workflow_id, approval_id=approval_id, version=7)
    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["sqlstate"] == "RE004"
    assert "expired" in error["message"]

    assert configured_demo.bookings(workflow_id) == []
    assert configured_demo.snapshot(workflow_id).json()["state"] == "BOOKING_IN_FLIGHT"


def test_a_spent_approval_names_which_way_it_is_gone(clock: ManualClock) -> None:
    """Consumed and expired are different situations for the patient, so they are different sentences.

    §9 makes them different rules; a single "this approval is not live" would leave a patient unable
    to tell "you already approved this" from "your approval timed out", and the second is the one that
    needs re-asking.
    """
    workflow_id = uuid4()
    service = _service(clock, ttl_seconds=60)
    granted = service.bind(
        workflow_id=workflow_id,
        patient_ref="patient-1",
        state_version=6,
        payload=BOOKING_PAYLOAD,
    )
    service.consume(granted.approval_id)

    with pytest.raises(ApprovalRefusedError) as consumed:
        service.authorise(
            workflow_id=workflow_id,
            patient_ref="patient-1",
            state_version=6,
            payload=BOOKING_PAYLOAD,
        )
    assert "already consumed" in str(consumed.value)
    assert "one approval authorises exactly one booking" in str(consumed.value)

    # A second consumption is refused too: the database's `UNIQUE(approval_id)` is the last gate, and
    # the ledger mirrors it rather than overwriting the first spend.
    with pytest.raises(ApprovalRefusedError) as again:
        service.consume(granted.approval_id)
    assert "already consumed at" in str(again.value)

    # The ledger still holds it: a refusal that could not distinguish the two ways consent is gone
    # would tell the patient nothing, which is why `for_workflow` does not filter.
    assert [binding.approval_id for binding in service._ledger.for_workflow(workflow_id)] == [
        granted.approval_id
    ]


def test_a_ledger_refuses_to_spend_an_unknown_approval(clock: ManualClock) -> None:
    """Spending something that was never granted is a refusal, not a no-op."""
    service = _service(clock)
    with pytest.raises(ApprovalRefusedError) as refusal:
        service.consume(uuid4())
    assert "no approval" in str(refusal.value)


def test_a_consent_whose_hash_is_not_the_workflow_s_cannot_book(
    configured_client: TestClient, configured_demo: Demo
) -> None:
    """The gate compares against the workflow's recorded hash, not against the presented payload.

    A payload that hashes differently from the one *the workflow holds* is refused before the approval
    is even consulted, and the message names the workflow's own `booking_payload_hash`. That ordering
    is the point: a client cannot consent to a payload by presenting it twice.
    """
    workflow_id = configured_demo.seed()
    approval_id = configured_demo.in_booking_flight(workflow_id)
    response = configured_demo.booking(
        workflow_id,
        approval_id=approval_id,
        version=7,
        payload={**BOOKING_PAYLOAD, "doctor_id": "doctor-9"},
    )
    assert response.status_code == 422
    assert "not the one this workflow holds" in response.json()["error"]["message"]
    assert configured_demo.bookings(workflow_id) == []


def test_the_workflow_records_the_payload_and_hash_its_review_card_showed(
    configured_client: TestClient, configured_demo: Demo
) -> None:
    """Consent is bound to the payload the *card* rendered, so both are written together.

    The review card's props are the source: what the patient is shown travels into the workflow with
    its canonical hash, which is what makes the booking gate's comparison meaningful. Asserted before
    the booking, so the two are not confused with what the write itself records.
    """
    workflow_id = configured_demo.seed()
    card = configured_demo.review_card(workflow_id)
    payload = configured_demo.admin_view(workflow_id).json()["workflow"]["payload"]
    assert payload[BOOKING_PAYLOAD_KEY] == card["props"][BOOKING_PAYLOAD_KEY]
    assert payload[PAYLOAD_HASH_KEY] == canonical_booking_payload_hash(BOOKING_PAYLOAD)
    assert payload[PAYLOAD_HASH_KEY] == canonical_booking_payload_hash(
        card["props"][BOOKING_PAYLOAD_KEY]
    )
    # No consent yet, so nothing can be booked from this state — the policy row says so (§9).
    assert (
        configured_demo.booking(
            workflow_id,
            approval_id=str(uuid4()),
            version=6,
        ).status_code
        == 403
    )


def test_a_binding_is_a_value_with_no_way_to_become_live_again(clock: ManualClock) -> None:
    """`ApprovalBinding` is frozen: consumption produces a *new* binding rather than mutating one.

    That is what makes "exactly one booking" checkable: the ledger's stored binding is the record of
    the spend, and a caller holding the old value cannot un-spend it.
    """
    service = _service(clock)
    granted = service.bind(
        workflow_id=uuid4(),
        patient_ref="patient-1",
        state_version=6,
        payload=BOOKING_PAYLOAD,
    )
    spent = service.consume(granted.approval_id)

    assert granted.is_consumed is False, "the value the caller already held is unchanged"
    assert spent.is_consumed is True
    assert spent.consumed_at == clock.now()
    with pytest.raises(TypeError):
        ApprovalBinding(  # type: ignore[call-arg]
            approval_id=granted.approval_id,
            workflow_id=granted.workflow_id,
            patient_ref=granted.patient_ref,
            state_version=granted.state_version,
            payload_hash=granted.payload_hash,
            approved_at=granted.approved_at,
            expires_at=granted.expires_at,
            # `revoked_at` is deliberately not a parameter: the shape is closed, so the schema is
            # what refuses this, not the caller's restraint. The ignore is narrowed to `call-arg`
            # so that if the refusal moves, this line starts failing instead of quietly passing.
            revoked_at=clock.now(),  # type: ignore[call-arg]
        )


def test_the_booking_state_is_reachable_only_from_the_state_consent_is_given_in(
    configured_demo: Demo,
) -> None:
    """Why the version clause is one behind: there is no other way into `BOOKING_IN_FLIGHT`.

    Read out of the state machine rather than asserted here by hand, because the consent guard on this
    single edge is the thing that makes a live approval meaningful at all (§9's note on the migration:
    booking straight out of an approval state is not an edge).
    """
    from referraleasy_gateway.transitions import LEGAL_EDGES

    entry = sorted(
        from_state for from_state, to_state in LEGAL_EDGES if to_state == "BOOKING_IN_FLIGHT"
    )
    assert entry == ["AWAITING_FINAL_APPROVAL", "BOOKING_UNVERIFIED"]
    # The second entry is the reconciliation path, and consent is already spent by then: the approval
    # that opened the first attempt was consumed by it, which is why `RE004` — not the version — is
    # what refuses a second booking there (see `test_booking_once.py`).

    assert WorkflowState.BOOKING_IN_FLIGHT.value in {to for _from, to in LEGAL_EDGES}, (
        "the state is reachable at all"
    )
