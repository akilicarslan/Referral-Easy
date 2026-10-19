"""Integration tests for the booking gate (§9).

The invariant: **no booking without a live approval bound to patient + workflow + state_version +
payload hash + expiry.** It is enforced at three points, and every one of them is exercised below,
because a gate is only as strong as the weakest of them:

1. entering `BOOKING_IN_FLIGHT` requires `approval_is_live(...)` at the epoch and payload hash the
   consent was given for (`workflow_booking_consent_guard`, RE004). Asking for consent is not
   consent, so entering the *booking* flow is what needs it -- not the state where consent is
   collected.
2. `booking_request` carries the binding as a composite foreign key to `approval`, so no row can
   exist that disagrees with its approval about workflow, patient or payload hash. The function
   copies those columns from the approval, which is why `record_booking` has no hash argument to get
   wrong.
3. the insert spends the approval, and `UNIQUE(approval_id)` means one approval can never authorise
   two bookings -- not even after the first is cancelled.

`BOOKED` is then unreachable without a *confirmed* booking request, which keeps "we think we booked
it" and "we booked it" distinct.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from referraleasy_domain import ApprovalRefusedError, DomainError, canonical_booking_payload_hash

pytestmark = pytest.mark.integration

# What the schema refuses when consent itself is written down wrongly -- a mismatched patient, a
# stale epoch, a workflow that is not waiting for one -- is asserted in
# `migrations/tests/test_approval_booking.py`, against the direct INSERT that a patient-side consent
# service would make. Those refusals are real, but they are not this client's surface: the domain
# package deliberately offers no way to record an approval, because consent comes from the patient
# rather than from any worker, and a store method that wrote one would be a way to forge it. What is
# tested here is the client's own path: entering the booking flow, and recording a booking that the
# approval authorises.


def _enter_booking_flow(store, workflow, *, worker: str = "w-1"):
    return store.advance(
        workflow.id,
        worker,
        expected_generation=workflow.generation,
        expected_state_version=workflow.state_version,
        to_state="BOOKING_IN_FLIGHT",
    )


# ---------------------------------------------------------------------------
# The binding, as a question
# ---------------------------------------------------------------------------


def test_a_live_approval_matches_the_full_binding_tuple(seed, store) -> None:
    workflow, _approval_id, payload_hash = seed.consent_ready()

    assert store.has_live_approval(workflow.id, "P-77", workflow.state_version, payload_hash)

    # Every part of the tuple is load-bearing: change any one and the consent no longer applies.
    assert not store.has_live_approval(workflow.id, "P-99", workflow.state_version, payload_hash)
    assert not store.has_live_approval(
        workflow.id, "P-77", workflow.state_version + 1, payload_hash
    )
    assert not store.has_live_approval(workflow.id, "P-77", workflow.state_version, "b" * 64)
    assert not store.has_live_approval(uuid4(), "P-77", workflow.state_version, payload_hash)


def test_a_lapsed_approval_is_not_live(seed, store) -> None:
    """Expiry is tested through the predicate's own clock.

    A lapsed approval cannot be staged: `approval_expiry_after_approval` refuses `expires_at <=
    approved_at` on insert, and `approval_no_rebinding` makes `expires_at` immutable afterwards. That
    is the point -- consent cannot be back-dated into still being valid -- but it also means the only
    controllable clock is the `at` argument, which is exactly why `approval_is_live` takes one.
    """
    workflow, _approval_id, payload_hash = seed.consent_ready()
    bound_at = workflow.state_version

    assert store.has_live_approval(workflow.id, "P-77", bound_at, payload_hash)
    assert not store.has_live_approval(
        workflow.id, "P-77", bound_at, payload_hash, at=datetime.now(UTC) + timedelta(hours=2)
    )


# ---------------------------------------------------------------------------
# Entering the booking flow
# ---------------------------------------------------------------------------


def test_entering_the_booking_flow_requires_live_consent(seed, store, refusals) -> None:
    workflow = seed.to_final_approval()
    # A payload hash but no approval: the refusal must be about consent, not about a missing hash.
    seed.set_booking_payload_hash(workflow.id, canonical_booking_payload_hash({"slot_id": "S-1"}))

    with refusals(ApprovalRefusedError):
        _enter_booking_flow(store, workflow)


def test_entering_the_booking_flow_without_a_payload_hash_is_refused(seed, store, refusals) -> None:
    """No hash means nothing was ever consented to, so there is nothing to compare against."""
    workflow = seed.to_final_approval()

    with refusals(ApprovalRefusedError):
        _enter_booking_flow(store, workflow)


def test_a_lapsed_approval_does_not_admit_the_workflow(seed, store, db, refusals) -> None:
    """The expiry half of the binding, at the gate that actually matters: the client's own call.

    Real time has to pass. `approval_is_live` compares against `now()`, which is the transaction
    timestamp -- deliberately, so a decision is judged against one consistent clock -- so a lapse
    cannot be simulated inside a transaction. The consent is granted with a fraction of a second to
    live, and the test crosses a real transaction boundary to outlive it.
    """
    workflow, _approval_id, _payload_hash = seed.consent_ready(expires_in="0.1 seconds")
    db.commit()
    time.sleep(0.35)

    with refusals(ApprovalRefusedError):
        _enter_booking_flow(store, workflow)


def test_live_consent_admits_the_workflow_to_the_booking_flow(seed, store) -> None:
    workflow, _approval_id, _payload_hash = seed.consent_ready()

    entered = _enter_booking_flow(store, workflow)

    assert entered.state == "BOOKING_IN_FLIGHT"
    assert entered.state_version == workflow.state_version + 1


def test_the_approval_is_bound_to_the_epoch_it_was_granted_in(seed, store) -> None:
    """Entering the booking flow moves the workflow on, and the consent does not move with it.

    Consent authorises one transition out of the epoch it was given in. Reading the approval against
    the *new* state_version finds nothing -- which is why a later attempt needs the approval to have
    been consumed into a booking rather than re-read.
    """
    workflow, _approval_id, payload_hash = seed.consent_ready()
    entered = _enter_booking_flow(store, workflow)

    assert store.has_live_approval(entered.id, "P-77", workflow.state_version, payload_hash)
    assert not store.has_live_approval(entered.id, "P-77", entered.state_version, payload_hash)


# ---------------------------------------------------------------------------
# Recording the booking
# ---------------------------------------------------------------------------


def test_recording_a_booking_copies_the_binding_from_the_approval(seed, store) -> None:
    workflow, approval_id, payload_hash = seed.consent_ready()
    entered = _enter_booking_flow(store, workflow)

    booking = store.record_booking(approval_id, "scheduling")

    assert booking.workflow_id == entered.id
    assert booking.patient_ref == "P-77"
    assert booking.payload_hash == payload_hash
    assert booking.state == "pending"
    assert booking.external_ref is None


def test_recording_a_booking_spends_the_approval(seed, store) -> None:
    workflow, approval_id, payload_hash = seed.consent_ready()
    entered = _enter_booking_flow(store, workflow)
    store.record_booking(approval_id, "scheduling")

    # Single-use: the insert is what consumes it, so a second attempt has nothing to spend.
    assert not store.has_live_approval(entered.id, "P-77", workflow.state_version, payload_hash)


def test_a_second_booking_on_one_approval_is_impossible(seed, store, db) -> None:
    """`UNIQUE(approval_id)`, checked while the row is inserted.

    The failure surfaces with a raw integrity code rather than an RE00x: it is a constraint doing its
    job, and the store reports it faithfully as a `DomainError` carrying that code instead of
    inventing a category for it.
    """
    workflow, approval_id, _payload_hash = seed.consent_ready()
    _enter_booking_flow(store, workflow)
    store.record_booking(approval_id, "scheduling")

    with pytest.raises(DomainError) as excinfo, db.transaction():
        store.record_booking(approval_id, "scheduling")

    assert excinfo.value.sqlstate == "23505"
    assert "booking_request" in str(excinfo.value)


def test_consent_that_lapses_before_the_booking_is_written_cannot_authorise_it(
    seed, store, db, refusals
) -> None:
    """Expiry is re-checked where the approval is spent, not trusted from an earlier check.

    The workflow was admitted to the booking flow while the consent was live, and the consent lapsed
    in between -- the gap between deciding to book and writing the booking down is exactly where a
    stale decision would otherwise slip through. The insert looks again and refuses.
    """
    workflow, approval_id, _payload_hash = seed.consent_ready(expires_in="0.1 seconds")
    _enter_booking_flow(store, workflow)

    db.commit()
    time.sleep(0.35)

    with refusals(ApprovalRefusedError):
        store.record_booking(approval_id, "scheduling")


def test_a_booking_against_an_unknown_approval_is_refused(seed, store, refusals) -> None:
    with refusals(ApprovalRefusedError):
        store.record_booking(uuid4(), "scheduling")


# ---------------------------------------------------------------------------
# Reaching BOOKED
# ---------------------------------------------------------------------------


def test_booked_is_unreachable_while_the_booking_request_is_only_pending(
    seed, store, refusals
) -> None:
    """A booking in flight is not a booking. Until a request is confirmed, BOOKED is refused."""
    workflow, approval_id, _payload_hash = seed.consent_ready()
    entered = _enter_booking_flow(store, workflow)
    store.record_booking(approval_id, "scheduling")

    with refusals(ApprovalRefusedError):
        store.advance(
            entered.id,
            "w-1",
            expected_generation=entered.generation,
            expected_state_version=entered.state_version,
            to_state="BOOKED",
        )


def test_booked_is_reachable_once_the_booking_is_confirmed(seed, store, db) -> None:
    workflow, approval_id, _payload_hash = seed.consent_ready()
    entered = _enter_booking_flow(store, workflow)
    booking = store.record_booking(approval_id, "scheduling")

    # Confirming is the scheduling worker's own act on the provider's answer; there is no function
    # for it, and `booking_request_guard` is what constrains the UPDATE (pending -> confirmed, with
    # an external reference).
    with db.cursor() as cur:
        cur.execute(
            "UPDATE booking_request SET state = 'confirmed', external_ref = %s WHERE id = %s",
            ("PROVIDER-REF-1", booking.id),
        )

    booked = store.advance(
        entered.id,
        "w-1",
        expected_generation=entered.generation,
        expected_state_version=entered.state_version,
        to_state="BOOKED",
    )

    assert booked.state == "BOOKED"


def test_the_booking_request_carries_the_approval_it_is_bound_to(seed, store) -> None:
    workflow, approval_id, _payload_hash = seed.consent_ready()
    _enter_booking_flow(store, workflow)

    booking = store.record_booking(approval_id, "scheduling")

    assert booking.approval_id == UUID(approval_id)
