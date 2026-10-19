"""Audit stays in PostgreSQL.

The tests here are about two things: that the log cannot be rewritten, and that
what it records is derived from the database's own view of events rather than
from whatever the caller remembered to log.
"""

from __future__ import annotations

import pytest
from migration_fixtures import (
    SHA_A,
    as_actor,
    assert_rejects,
    grant_approval,
    make_workflow,
    record_evidence,
    set_payload_hash,
    walk,
)

pytestmark = pytest.mark.integration


def entries(db, **filters) -> list[dict]:
    where = " AND ".join(f"{key} = %s" for key in filters) or "true"
    with db.cursor() as cur:
        cur.execute(f"SELECT * FROM audit WHERE {where} ORDER BY seq", tuple(filters.values()))
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------


def test_an_audit_entry_cannot_be_rewritten(db):
    as_actor(db, "automation", "worker-1")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM audit_record('test.action', 'thing', 'x-1')")
        entry_id = cur.fetchone()["seq"]

    assert_rejects(
        db,
        "UPDATE audit SET action = 'something.else' WHERE seq = %s",
        (entry_id,),
        sqlstate="RE008",
        match="append-only",
    )


def test_an_audit_entry_cannot_be_deleted(db):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM audit_record('test.action', 'thing', 'x-1')")
        entry_id = cur.fetchone()["seq"]

    assert_rejects(
        db,
        "DELETE FROM audit WHERE seq = %s",
        (entry_id,),
        sqlstate="RE008",
    )


def test_the_audit_log_cannot_be_truncated(db):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM audit_record('test.action', 'thing', 'x-1')")

    assert_rejects(db, "TRUNCATE audit CASCADE", (), sqlstate="RE008")


def test_the_actor_is_taken_from_the_transaction_not_from_the_caller(db):
    """audit_record has no actor argument on purpose: a caller cannot attribute
    its own act to somebody else."""
    as_actor(db, "human", "op-7")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM audit_record('test.action', 'thing', 'x-1')")
        row = cur.fetchone()

    assert row["actor_kind"] == "human"
    assert row["actor_id"] == "op-7"


# ---------------------------------------------------------------------------
# The entry hash chain
# ---------------------------------------------------------------------------


def test_the_chain_is_intact_after_ordinary_use(db):
    as_actor(db, "automation", "worker-1")
    for index in range(5):
        with db.cursor() as cur:
            cur.execute("SELECT * FROM audit_record('test.action', 'thing', %s)", (f"x-{index}",))

    with db.cursor() as cur:
        cur.execute("SELECT * FROM audit_verify_chain()")
        assert cur.fetchall() == []


def test_entries_are_linked_to_their_predecessor(db):
    as_actor(db, "automation", "worker-1")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM audit_record('first', 'thing', 'x-1')")
        first = cur.fetchone()
        cur.execute("SELECT * FROM audit_record('second', 'thing', 'x-2')")
        second = cur.fetchone()

    assert first["prev_hash"] == "0" * 64, "the first entry anchors the chain"
    assert second["prev_hash"] == first["entry_hash"]
    assert second["entry_hash"] != first["entry_hash"]


def test_tampering_with_an_entry_is_detectable(db):
    """The chain is tamper-EVIDENT. Rewriting a row requires disabling the
    append-only trigger first, which is exactly the privileged act the chain
    exists to make visible afterwards."""
    as_actor(db, "automation", "worker-1")
    for index in range(3):
        with db.cursor() as cur:
            cur.execute("SELECT * FROM audit_record('test.action', 'thing', %s)", (f"x-{index}",))

    # A privileged tamper: step around the guard, edit the middle entry.
    db.cursor().execute("ALTER TABLE audit DISABLE TRIGGER audit_append_only_trigger")
    db.cursor().execute(
        "UPDATE audit SET action = 'something.else', entry_hash = repeat('0', 64) WHERE seq = 2"
    )

    with db.cursor() as cur:
        cur.execute("SELECT * FROM audit_verify_chain()")
        problems = cur.fetchall()
    db.cursor().execute("ALTER TABLE audit ENABLE TRIGGER audit_append_only_trigger")

    assert problems, "the chain did not notice an edited entry"
    assert problems[0]["problem"] == "entry_hash does not match the row content"


# ---------------------------------------------------------------------------
# The audit records the events themselves, not the caller's memory of them
# ---------------------------------------------------------------------------


def test_a_workflow_transition_is_recorded_automatically(db):
    as_actor(db, "automation", "worker-1")
    workflow_id = make_workflow(db)
    walk(db, workflow_id, ["PROVIDER_MATCHING"])

    recorded = entries(db, action="workflow.transition")

    assert len(recorded) == 1
    assert recorded[0]["detail"]["from_state"] == "INTAKE"
    assert recorded[0]["detail"]["to_state"] == "PROVIDER_MATCHING"
    assert str(recorded[0]["workflow_id"]) == workflow_id


def test_an_approval_being_consumed_is_recorded(db):
    as_actor(db, "automation", "worker-1")
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
    set_payload_hash(db, workflow_id, SHA_A)
    evidence_id = record_evidence(db)
    approval_id = grant_approval(
        db,
        workflow_id,
        state_version=row["state_version"],
        evidence_id=evidence_id,
    )

    as_actor(db, "automation", "booking-agent")
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM booking_request_create(%s, 'booking-agent', NULL)", (approval_id,)
        )

    recorded = entries(db, action="approval.consumed")
    assert len(recorded) == 1
    assert recorded[0]["actor_id"] == "booking-agent"
    assert recorded[0]["detail"]["booking_payload_hash"] == SHA_A


def test_a_booking_request_is_recorded(db):
    as_actor(db, "automation", "worker-1")
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
    set_payload_hash(db, workflow_id, SHA_A)
    evidence_id = record_evidence(db)
    approval_id = grant_approval(
        db,
        workflow_id,
        state_version=row["state_version"],
        evidence_id=evidence_id,
    )

    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM booking_request_create(%s, 'booking-agent', 'EXT-1')", (approval_id,)
        )

    recorded = entries(db, action="booking.requested")
    assert len(recorded) == 1
    assert recorded[0]["detail"]["payload_hash"] == SHA_A


def test_a_denial_does_not_leave_the_audit_log_truncated(db):
    """A refused write aborts its transaction, so the audited events of that
    transaction are rolled back with it. The log is never left half-written --
    which is the property worth asserting, rather than pretending denials are
    logged automatically."""
    as_actor(db, "automation", "worker-1")
    workflow_id = make_workflow(db)
    row = walk(db, workflow_id, ["PROVIDER_MATCHING"])

    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM audit")
        before = cur.fetchone()["n"]

    # An illegal transition, in its own transaction, by the worker that
    # legitimately holds the lease: the refusal is about the edge, not about
    # ownership.
    db.commit()
    assert_rejects(
        db,
        "SELECT * FROM workflow_advance(%s, %s, %s, %s, %s, %s)",
        (workflow_id, "w-1", row["generation"], row["state_version"], "resolved", 60),
        sqlstate="RE002",
    )

    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM audit")
        after = cur.fetchone()["n"]

    assert after == before, "an aborted transaction left audit rows behind"
