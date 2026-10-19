"""Approval binding and the structural guard against double booking.

The claims under test:

* an approval is valid only for one (patient, workflow, state_version, payload
  hash, expiry) tuple, and any change to the data moves the tuple;
* a consumed approval cannot authorise a second write;
* an operator action cannot manufacture consent.
"""

from __future__ import annotations

import time

import pytest
from migration_fixtures import (
    SHA_A,
    SHA_B,
    advance,
    as_actor,
    assert_rejects,
    claim,
    grant_approval,
    make_workflow,
    record_evidence,
    set_payload_hash,
    walk,
)

pytestmark = pytest.mark.integration


def _awaiting_approval(db, *, payload_hash: str = SHA_A):
    """A workflow parked in awaiting_approval, with its booking payload hash set."""
    workflow_id = make_workflow(db)
    row = walk(
        db,
        workflow_id,
        [
            "PROVIDER_MATCHING",
            "AWAITING_HOSPITAL_CHOICE",
            "AWAITING_DOCTOR_CHOICE",
            "READINESS_CHECK",
            "AWAITING_SLOT_CHOICE",
            "AWAITING_FINAL_APPROVAL",
        ],
    )
    set_payload_hash(db, workflow_id, payload_hash)
    return workflow_id, row


# ---------------------------------------------------------------------------
# The happy path, so the refusals mean something
# ---------------------------------------------------------------------------


def test_a_live_bound_approval_allows_the_booking_flow(db):
    workflow_id, row = _awaiting_approval(db)
    evidence_id = record_evidence(db)
    approval_id = grant_approval(
        db,
        workflow_id,
        state_version=row["state_version"],
        evidence_id=evidence_id,
    )

    advanced = advance(
        db,
        workflow_id,
        "BOOKING_IN_FLIGHT",
        worker="w-1",
        expected_generation=row["generation"],
        expected_state_version=row["state_version"],
    )
    assert advanced["state"] == "BOOKING_IN_FLIGHT"

    booking = _create_booking(db, approval_id, workflow_id)
    assert booking["payload_hash"] == SHA_A
    assert booking["state"] == "pending"


def _create_booking(db, approval_id: str, workflow_id: str, *, external_ref: str | None = None):
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM booking_request_create(%s, %s, %s)",
            (approval_id, "booking-agent", external_ref),
        )
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Binding: patient + workflow + state_version + payload hash + expiry
# ---------------------------------------------------------------------------


def test_approval_is_bound_to_the_current_epoch(db):
    """An approval naming a state_version the workflow has moved past is
    refused at insert, not filtered out later."""
    workflow_id, row = _awaiting_approval(db)
    evidence_id = record_evidence(db)

    assert_rejects(
        db,
        "INSERT INTO approval (workflow_id, patient_ref, evidence_id, state_version,"
        " booking_payload_hash, channel, approved_by, expires_at)"
        " VALUES (%s, 'P-77', %s, %s, %s, 'patient_self_service', 'patient', now() + interval '1 hour')",
        (workflow_id, evidence_id, row["state_version"] - 1, SHA_A),
        sqlstate="RE004",
        match="state_version",
    )


def test_approval_is_refused_when_the_workflow_is_not_awaiting_approval(db):
    workflow_id = make_workflow(db)
    row = claim(db, workflow_id, "w-1")
    evidence_id = record_evidence(db)

    assert_rejects(
        db,
        "INSERT INTO approval (workflow_id, patient_ref, evidence_id, state_version,"
        " booking_payload_hash, channel, approved_by, expires_at)"
        " VALUES (%s, 'P-77', %s, %s, %s, 'patient_self_service', 'patient', now() + interval '1 hour')",
        (workflow_id, evidence_id, row["state_version"], SHA_A),
        sqlstate="RE004",
        match="not AWAITING_FINAL_APPROVAL",
    )


def test_approval_is_refused_for_another_patient(db):
    workflow_id, row = _awaiting_approval(db)
    evidence_id = record_evidence(db)

    assert_rejects(
        db,
        "INSERT INTO approval (workflow_id, patient_ref, evidence_id, state_version,"
        " booking_payload_hash, channel, approved_by, expires_at)"
        " VALUES (%s, 'P-OTHER', %s, %s, %s, 'patient_self_service', 'patient', now() + interval '1 hour')",
        (workflow_id, evidence_id, row["state_version"], SHA_A),
        sqlstate="RE004",
        match="does not match workflow patient",
    )


def test_changing_the_booking_payload_invalidates_the_approval(db):
    """THE binding rule, stated as a test: the patient approved a particular
    payload. Change the payload and the approval stops matching -- there is no
    "refresh the approval" step, because the old one no longer applies to
    anything."""
    workflow_id, row = _awaiting_approval(db, payload_hash=SHA_A)
    evidence_id = record_evidence(db)
    grant_approval(db, workflow_id, state_version=row["state_version"], evidence_id=evidence_id)

    # Something in the booking changes after consent was given.
    set_payload_hash(db, workflow_id, SHA_B)

    assert_rejects(
        db,
        "SELECT * FROM workflow_advance(%s, %s, %s, %s, %s, %s)",
        (workflow_id, "w-1", row["generation"], row["state_version"], "BOOKING_IN_FLIGHT", 60),
        sqlstate="RE004",
        match="no live approval",
    )


def test_moving_the_workflow_past_the_approved_epoch_invalidates_the_approval(db):
    """Same rule for the other binding column: the epoch. The approval was
    given for version N; the workflow is no longer at N."""
    workflow_id, row = _awaiting_approval(db, payload_hash=SHA_A)
    evidence_id = record_evidence(db)
    grant_approval(db, workflow_id, state_version=row["state_version"], evidence_id=evidence_id)

    # Out to the slot state and back. The workflow returns to the SAME state at a
    # new state_version, which is a new epoch — so this is a stricter case than a
    # round trip through a different state: nothing about the workflow's
    # appearance has changed, and the old consent still must not apply to it.
    back = advance(
        db,
        workflow_id,
        "AWAITING_SLOT_CHOICE",
        worker="w-1",
        expected_generation=row["generation"],
        expected_state_version=row["state_version"],
    )
    forward = advance(
        db,
        workflow_id,
        "AWAITING_FINAL_APPROVAL",
        worker="w-1",
        expected_generation=back["generation"],
        expected_state_version=back["state_version"],
    )

    assert_rejects(
        db,
        "SELECT * FROM workflow_advance(%s, %s, %s, %s, %s, %s)",
        (
            workflow_id,
            "w-1",
            forward["generation"],
            forward["state_version"],
            "BOOKING_IN_FLIGHT",
            60,
        ),
        sqlstate="RE004",
        match="no live approval",
    )


def test_expiry_cannot_be_pushed_back(db):
    """Why the expiry tests below have to wait for real time to pass: expiry is
    part of the immutable binding. An "expired" approval cannot be manufactured
    by editing one, which is itself a guarantee worth asserting."""
    workflow_id, row = _awaiting_approval(db)
    evidence_id = record_evidence(db)
    approval_id = grant_approval(
        db,
        workflow_id,
        state_version=row["state_version"],
        evidence_id=evidence_id,
    )

    assert_rejects(
        db,
        "UPDATE approval SET expires_at = now() + interval '1 day' WHERE id = %s",
        (approval_id,),
        sqlstate="RE004",
        match="cannot be rebound",
    )


def test_an_expired_approval_cannot_authorise_a_transition(db):
    workflow_id, row = _awaiting_approval(db)
    evidence_id = record_evidence(db)
    grant_approval(
        db,
        workflow_id,
        state_version=row["state_version"],
        evidence_id=evidence_id,
        expires_in="0.1 seconds",
    )

    # `now()` is the transaction timestamp, deliberately: a decision is judged
    # against one consistent clock. So an expiry cannot be simulated inside a
    # transaction -- the test has to genuinely cross a transaction boundary.
    db.commit()
    time.sleep(0.35)

    assert_rejects(
        db,
        "SELECT * FROM workflow_advance(%s, %s, %s, %s, %s, %s)",
        (workflow_id, "w-1", row["generation"], row["state_version"], "BOOKING_IN_FLIGHT", 60),
        sqlstate="RE004",
        match="no live approval",
    )


def test_the_binding_cannot_be_rewritten(db):
    """Rebinding is how a stale consent silently becomes a valid one."""
    workflow_id, row = _awaiting_approval(db)
    evidence_id = record_evidence(db)
    approval_id = grant_approval(
        db,
        workflow_id,
        state_version=row["state_version"],
        evidence_id=evidence_id,
    )

    assert_rejects(
        db,
        "UPDATE approval SET booking_payload_hash = %s WHERE id = %s",
        (SHA_B, approval_id),
        sqlstate="RE004",
        match="cannot be rebound",
    )


# ---------------------------------------------------------------------------
# Consumption: one approval, one booking, forever
# ---------------------------------------------------------------------------


def test_creating_a_booking_consumes_the_approval(db):
    workflow_id, row = _awaiting_approval(db)
    evidence_id = record_evidence(db)
    approval_id = grant_approval(
        db,
        workflow_id,
        state_version=row["state_version"],
        evidence_id=evidence_id,
    )
    _create_booking(db, approval_id, workflow_id)

    with db.cursor() as cur:
        cur.execute("SELECT consumed_at, consumed_by FROM approval WHERE id = %s", (approval_id,))
        consumed = cur.fetchone()

    assert consumed["consumed_at"] is not None
    assert consumed["consumed_by"] == "booking-agent"


def test_a_consumed_approval_cannot_authorise_a_second_booking(db):
    """THE double-booking test.

    The second attempt is refused by the unique constraint on approval_id (and
    would also be refused by the consumption trigger). Two independent guards,
    which is the point: neither a retry nor a race gets through.
    """
    workflow_id, row = _awaiting_approval(db)
    evidence_id = record_evidence(db)
    approval_id = grant_approval(
        db,
        workflow_id,
        state_version=row["state_version"],
        evidence_id=evidence_id,
    )
    first = _create_booking(db, approval_id, workflow_id)
    assert first["id"]

    assert_rejects(
        db,
        "SELECT * FROM booking_request_create(%s, %s, %s)",
        (approval_id, "booking-agent", None),
        sqlstate="23505",  # unique_violation
        match="booking_request_one_per_approval",
    )


def test_a_consumed_approval_cannot_authorise_a_new_booking_request_directly(db):
    """The same guarantee when the caller bypasses booking_request_create and
    writes the row itself."""
    workflow_id, row = _awaiting_approval(db)
    evidence_id = record_evidence(db)
    approval_id = grant_approval(
        db,
        workflow_id,
        state_version=row["state_version"],
        evidence_id=evidence_id,
    )
    _create_booking(db, approval_id, workflow_id)

    assert_rejects(
        db,
        "INSERT INTO booking_request (approval_id, workflow_id, patient_ref, payload_hash,"
        " requested_by) VALUES (%s, %s, 'P-77', %s, 'sneaky')",
        (approval_id, workflow_id, SHA_A),
        sqlstate="23505",
    )


def test_consumption_cannot_be_undone(db):
    workflow_id, row = _awaiting_approval(db)
    evidence_id = record_evidence(db)
    approval_id = grant_approval(
        db,
        workflow_id,
        state_version=row["state_version"],
        evidence_id=evidence_id,
    )
    _create_booking(db, approval_id, workflow_id)

    assert_rejects(
        db,
        "UPDATE approval SET consumed_at = NULL, consumed_by = NULL WHERE id = %s",
        (approval_id,),
        sqlstate="RE004",
        match="already consumed",
    )


def test_an_expired_approval_cannot_be_consumed(db):
    """The booking insert itself is what spends the approval, so an approval
    that expired between the decision and the write is caught here."""
    workflow_id, row = _awaiting_approval(db)
    evidence_id = record_evidence(db)
    approval_id = grant_approval(
        db,
        workflow_id,
        state_version=row["state_version"],
        evidence_id=evidence_id,
        expires_in="0.1 seconds",
    )
    # The approval was genuinely valid when it was recorded; it simply lapses
    # before the booking is written. Crossing the transaction boundary is what
    # lets the clock move (see the note in the previous test).
    db.commit()
    time.sleep(0.35)

    assert_rejects(
        db,
        "SELECT * FROM booking_request_create(%s, %s, %s)",
        (approval_id, "booking-agent", None),
        sqlstate="RE004",
        match="consumed or has expired",
    )


def test_a_booking_cannot_carry_a_payload_the_patient_did_not_approve(db):
    """The composite foreign key does the comparing: a row that disagrees with
    its approval on workflow, patient or payload hash has no parent to point at."""
    workflow_id, row = _awaiting_approval(db)
    evidence_id = record_evidence(db)
    approval_id = grant_approval(
        db,
        workflow_id,
        state_version=row["state_version"],
        evidence_id=evidence_id,
    )

    assert_rejects(
        db,
        "INSERT INTO booking_request (approval_id, workflow_id, patient_ref, payload_hash,"
        " requested_by) VALUES (%s, %s, 'P-77', %s, 'booking-agent')",
        (approval_id, workflow_id, SHA_B),
        sqlstate="23503",  # foreign_key_violation
        match="booking_request_approval_binding_fkey",
    )


def test_a_booking_cannot_be_rebound_to_another_approval(db):
    workflow_id, row = _awaiting_approval(db)
    evidence_id = record_evidence(db)
    approval_id = grant_approval(
        db,
        workflow_id,
        state_version=row["state_version"],
        evidence_id=evidence_id,
    )
    booking = _create_booking(db, approval_id, workflow_id)

    assert_rejects(
        db,
        "UPDATE booking_request SET approval_id = gen_random_uuid() WHERE id = %s",
        (booking["id"],),
        sqlstate="RE004",
        match="cannot be rebound",
    )


# ---------------------------------------------------------------------------
# Consent cannot come from an operator, or from stale evidence
# ---------------------------------------------------------------------------


def test_an_operator_cannot_grant_consent(db):
    """The human actor context is set only by the handoff functions, so a
    handler cannot accidentally (or deliberately) mint an approval."""
    workflow_id, row = _awaiting_approval(db)
    evidence_id = record_evidence(db)
    as_actor(db, "human", "operator-7")

    assert_rejects(
        db,
        "INSERT INTO approval (workflow_id, patient_ref, evidence_id, state_version,"
        " booking_payload_hash, channel, approved_by, expires_at)"
        " VALUES (%s, 'P-77', %s, %s, %s, 'patient_self_service', 'operator-7', now() + interval '1 hour')",
        (workflow_id, evidence_id, row["state_version"], SHA_A),
        sqlstate="RE004",
        match="human actor cannot grant booking consent",
    )


def test_stale_evidence_cannot_support_an_approval(db):
    """A stale record is distinguishable from a fresh one -- and the difference
    is load-bearing: only fresh evidence gets to sit behind an approval."""
    workflow_id, row = _awaiting_approval(db)
    stale_evidence = record_evidence(
        db,
        evidence_id="EV-STALE",
        observed_at="now() - interval '2 days'",
        fresh_for="1 hour",
    )

    with db.cursor() as cur:
        cur.execute("SELECT is_fresh FROM evidence_freshness WHERE id = %s", (stale_evidence,))
        assert cur.fetchone()["is_fresh"] is False

    assert_rejects(
        db,
        "INSERT INTO approval (workflow_id, patient_ref, evidence_id, state_version,"
        " booking_payload_hash, channel, approved_by, expires_at)"
        " VALUES (%s, 'P-77', %s, %s, %s, 'patient_self_service', 'patient', now() + interval '1 hour')",
        (workflow_id, stale_evidence, row["state_version"], SHA_A),
        sqlstate="RE004",
        match="went stale",
    )


def test_non_authoritative_evidence_cannot_support_an_approval(db):
    workflow_id, row = _awaiting_approval(db)
    weak_evidence = record_evidence(db, evidence_id="EV-WEAK", authoritative=False)

    assert_rejects(
        db,
        "INSERT INTO approval (workflow_id, patient_ref, evidence_id, state_version,"
        " booking_payload_hash, channel, approved_by, expires_at)"
        " VALUES (%s, 'P-77', %s, %s, %s, 'patient_self_service', 'patient', now() + interval '1 hour')",
        (workflow_id, weak_evidence, row["state_version"], SHA_A),
        sqlstate="RE004",
        match="not authoritative",
    )


# ---------------------------------------------------------------------------
# Evidence store
# ---------------------------------------------------------------------------


def test_evidence_is_append_only(db):
    evidence_id = record_evidence(db)

    assert_rejects(
        db,
        "UPDATE evidence SET authoritative = true WHERE id = %s",
        (evidence_id,),
        sqlstate="RE008",
        match="append-only",
    )


def test_evidence_freshness_is_computed_by_the_database(db):
    """A writer cannot assert its own freshness."""
    fresh_id = record_evidence(db, evidence_id="EV-FRESH", observed_at="now()", fresh_for="1 hour")

    with db.cursor() as cur:
        cur.execute(
            "SELECT observed_at, stale_after, is_fresh FROM evidence_freshness WHERE id = %s",
            (fresh_id,),
        )
        row = cur.fetchone()

    assert row["is_fresh"] is True
    assert row["stale_after"] > row["observed_at"]
