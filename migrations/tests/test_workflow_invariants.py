"""Owner/lease + generation, and the optimistic check on every transition.

These are the two release-blocking invariants of the whole schema: a stale
worker must be unable to write, and two attempts at the same transition must not
both succeed.
"""

from __future__ import annotations

import pytest
from migration_fixtures import (
    advance,
    assert_rejects,
    claim,
    make_workflow,
    run_concurrently,
    walk,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Stale generation
# ---------------------------------------------------------------------------


def test_stale_generation_write_is_rejected(db):
    """A worker holding an older generation cannot advance the workflow."""
    workflow_id = make_workflow(db)
    row = claim(db, workflow_id, "worker-old")

    # The old worker stalls, its lease lapses, and another worker picks the
    # work up. The first worker is now provably stale -- but it does not know
    # that yet, which is exactly the situation this constraint exists for.
    db.cursor().execute(
        "UPDATE workflow SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
        (workflow_id,),
    )
    claim(db, workflow_id, "worker-new", expected_generation=row["generation"])
    db.commit()

    assert_rejects(
        db,
        "SELECT * FROM workflow_advance(%s, %s, %s, %s, %s, %s)",
        (
            workflow_id,
            "worker-old",
            row["generation"],
            row["state_version"],
            "PROVIDER_MATCHING",
            60,
        ),
        sqlstate="RE001",
        match="stale worker cannot write",
    )


def test_generation_cannot_be_decremented_directly(db):
    """Even a direct UPDATE cannot roll the fencing token backwards."""
    workflow_id = make_workflow(db)
    claim(db, workflow_id, "worker-1")

    assert_rejects(
        db,
        "UPDATE workflow SET generation = 0 WHERE id = %s",
        (workflow_id,),
        sqlstate="RE001",
        match="stale generation",
    )


def test_ownership_change_requires_a_generation_bump(db):
    """Swapping the owner without a new epoch would let two workers believe
    they hold the same generation."""
    workflow_id = make_workflow(db)
    claim(db, workflow_id, "worker-1")
    db.commit()

    assert_rejects(
        db,
        "UPDATE workflow SET owner_worker_id = 'worker-2' WHERE id = %s",
        (workflow_id,),
        sqlstate="RE001",
        match="without advancing generation",
    )


# ---------------------------------------------------------------------------
# Stale lease
# ---------------------------------------------------------------------------


def test_stale_lease_write_is_rejected(db):
    """An expired lease makes the owner stale even though it still calls itself
    the owner: the row is untouched, only the deadline has passed."""
    workflow_id = make_workflow(db)
    row = claim(db, workflow_id, "worker-1", lease_seconds=60)

    db.cursor().execute(
        "UPDATE workflow SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
        (workflow_id,),
    )

    assert_rejects(
        db,
        "SELECT * FROM workflow_advance(%s, %s, %s, %s, %s, %s)",
        (workflow_id, "worker-1", row["generation"], row["state_version"], "PROVIDER_MATCHING", 60),
        sqlstate="RE001",
        match="lease expired",
    )


def test_an_expired_lease_is_reclaimable(db):
    """The lease protects against a dead worker, it does not wedge the row."""
    workflow_id = make_workflow(db)
    first = claim(db, workflow_id, "worker-1", lease_seconds=60)
    db.cursor().execute(
        "UPDATE workflow SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
        (workflow_id,),
    )

    second = claim(db, workflow_id, "worker-2", expected_generation=first["generation"])

    assert second["owner_worker_id"] == "worker-2"
    assert second["generation"] == first["generation"] + 1


def test_a_live_lease_cannot_be_stolen(db):
    """...and while the lease is live, another worker cannot take the row."""
    workflow_id = make_workflow(db)
    claim(db, workflow_id, "worker-1", lease_seconds=600)

    assert_rejects(
        db,
        "SELECT * FROM workflow_claim(%s, %s, %s, %s)",
        (workflow_id, "worker-2", 0, 60),
        sqlstate="RE001",
        match="owned/leased elsewhere",
    )


# ---------------------------------------------------------------------------
# Optimistic concurrency on transitions
# ---------------------------------------------------------------------------


def test_illegal_transition_is_rejected_not_coerced(db):
    """intake -> resolved is not an edge of the state machine.

    Nothing about the row is invalid except the edge, so the refusal is the
    transition guard's own (RE002) and not some downstream complaint.
    """
    workflow_id = make_workflow(db)
    row = claim(db, workflow_id, "worker-1")

    assert_rejects(
        db,
        "SELECT * FROM workflow_advance(%s, %s, %s, %s, %s, %s)",
        (workflow_id, "worker-1", row["generation"], row["state_version"], "resolved", 60),
        sqlstate="RE002",
        match="illegal transition",
    )


def test_a_transition_into_the_booking_flow_without_consent_is_refused(db):
    """intake -> booked is refused twice over: it is not an edge, and it would
    need consent. The consent guard fires first, and says so."""
    workflow_id = make_workflow(db)
    row = claim(db, workflow_id, "worker-1")

    assert_rejects(
        db,
        "SELECT * FROM workflow_advance(%s, %s, %s, %s, %s, %s)",
        (workflow_id, "worker-1", row["generation"], row["state_version"], "BOOKED", 60),
        sqlstate="RE004",
        match="no confirmed booking_request",
    )


def test_state_version_must_advance_by_exactly_one(db):
    """A transition that skips a version is refused: the version IS the
    optimistic token every other writer compares against."""
    workflow_id = make_workflow(db)
    claim(db, workflow_id, "worker-1")

    assert_rejects(
        db,
        "UPDATE workflow SET state = 'PROVIDER_MATCHING', state_version = state_version + 2 WHERE id = %s",
        (workflow_id,),
        sqlstate="RE002",
        match="exactly one",
    )


def test_state_version_cannot_move_without_a_state_change(db):
    workflow_id = make_workflow(db)
    claim(db, workflow_id, "worker-1")

    assert_rejects(
        db,
        "UPDATE workflow SET state_version = state_version + 1 WHERE id = %s",
        (workflow_id,),
        sqlstate="RE002",
        match="without changing state",
    )


def test_concurrent_state_transition_exactly_one_wins(db, connect_factory):
    """Two workers issue the same transition from the same observed version.

    Exactly one commits; the other is refused because the winner's commit moved
    both the generation and the state_version out from under it.
    """
    workflow_id = make_workflow(db)
    row = claim(db, workflow_id, "worker-1")
    db.commit()

    generation = row["generation"]
    version = row["state_version"]

    def transition(conn):
        def run():
            result = advance(
                conn,
                workflow_id,
                "PROVIDER_MATCHING",
                worker="worker-1",
                expected_generation=generation,
                expected_state_version=version,
            )
            conn.commit()
            return result["state_version"]

        return run

    conn_a, conn_b = connect_factory(), connect_factory()
    results, errors = run_concurrently(transition(conn_a), transition(conn_b))

    winners = [r for r in results if r is not None]
    losers = [e for e in errors if e is not None]

    assert len(winners) == 1, f"expected exactly one winner, got {winners}"
    assert len(losers) == 1, f"expected exactly one loser, got {losers}"
    assert getattr(losers[0], "sqlstate", None) == "RE001"

    with db.cursor() as cur:
        cur.execute(
            "SELECT state, state_version, generation FROM workflow WHERE id = %s", (workflow_id,)
        )
        final = cur.fetchone()
    assert final["state"] == "PROVIDER_MATCHING"
    assert final["state_version"] == version + 1
    assert final["generation"] == generation + 1


def test_concurrent_claim_exactly_one_wins(db, connect_factory):
    """The claim itself is a compare-and-set, so a race resolves to one owner."""
    workflow_id = make_workflow(db)
    db.commit()

    def claimer(conn, worker):
        def run():
            result = claim(conn, workflow_id, worker, expected_generation=0)
            conn.commit()
            return result["owner_worker_id"]

        return run

    conn_a, conn_b = connect_factory(), connect_factory()
    results, errors = run_concurrently(claimer(conn_a, "worker-a"), claimer(conn_b, "worker-b"))

    assert len([r for r in results if r is not None]) == 1
    assert len([e for e in errors if e is not None]) == 1

    with db.cursor() as cur:
        cur.execute(
            "SELECT owner_worker_id, generation FROM workflow WHERE id = %s", (workflow_id,)
        )
        final = cur.fetchone()
    assert final["owner_worker_id"] in {"worker-a", "worker-b"}
    assert final["generation"] == 1


def test_a_walked_workflow_reaches_the_expected_state(db):
    """Sanity check on the legal path, so the rejections above mean something."""
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

    assert row["state"] == "AWAITING_FINAL_APPROVAL"
    # One transition per state on the route, counted as a delta from the initial version so
    # that lengthening the route cannot silently invalidate the assertion.
    assert row["state_version"] == 1 + 6
