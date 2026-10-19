"""Human ownership: suspension of automation, exclusive claims, and the fact
that resolving a ticket is not booking consent.
"""

from __future__ import annotations

import pytest
from migration_fixtures import (
    as_actor,
    assert_rejects,
    claim,
    make_workflow,
    run_concurrently,
    walk,
)

pytestmark = pytest.mark.integration


def raise_ticket(
    db, workflow_id: str, *, ticket_ref: str = "T-1001", reason: str = "ambiguous referral"
) -> str:
    """Open a ticket for a workflow that has been escalated to a human."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO handoff (workflow_id, ticket_ref, reason) VALUES (%s, %s, %s)"
            " RETURNING id",
            (workflow_id, ticket_ref, reason),
        )
        return str(cur.fetchone()["id"])


def escalated_workflow(db) -> tuple[str, str]:
    """A workflow in human_required, with an open ticket. The realistic path:
    triage decided a person had to look at this."""
    workflow_id = make_workflow(db)
    walk(db, workflow_id, ["HUMAN_HANDOFF"])
    return workflow_id, raise_ticket(db, workflow_id)


def handoff_claim(
    conn, handoff_id: str, operator: str, expected_generation: int, lease_seconds: int = 900
) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM handoff_claim(%s, %s, %s, %s)",
            (handoff_id, operator, expected_generation, lease_seconds),
        )
        return cur.fetchone()


# ---------------------------------------------------------------------------
# One ticket, one operator
# ---------------------------------------------------------------------------


def test_a_claim_takes_human_ownership_of_the_workflow(db):
    workflow_id = make_workflow(db)
    handoff_id = raise_ticket(db, workflow_id)

    handoff_claim(db, handoff_id, "op-1", 0)

    with db.cursor() as cur:
        cur.execute(
            "SELECT human_owner_id, owner_worker_id, lease_expires_at FROM workflow WHERE id = %s",
            (workflow_id,),
        )
        row = cur.fetchone()

    assert row["human_owner_id"] == "op-1"
    assert row["owner_worker_id"] is None, "machine ownership ends when a human takes over"
    assert row["lease_expires_at"] is None


def test_a_stale_claim_generation_is_refused(db):
    """The compare-and-set: an operator acting on a ticket state they last read
    an hour ago finds no row to update."""
    _workflow_id, handoff_id = escalated_workflow(db)
    handoff_claim(db, handoff_id, "op-1", 0)

    assert_rejects(
        db,
        "SELECT * FROM handoff_claim(%s, %s, %s, %s)",
        (handoff_id, "op-2", 0, 900),
        sqlstate="RE001",
        match="not current",
    )


def test_conflicting_claim_exactly_one_wins(db, connect_factory):
    """Two operators grab the same ticket at the same moment.

    Both read generation 0. The compare-and-set means the loser's WHERE clause
    stops matching as soon as the winner commits -- so the ticket has one owner,
    not two operators who each believe they are working it.
    """
    workflow_id, handoff_id = escalated_workflow(db)
    db.commit()

    def grab(conn, operator):
        def run():
            row = handoff_claim(conn, handoff_id, operator, 0)
            conn.commit()
            return row["claimed_by"]

        return run

    conn_a, conn_b = connect_factory(), connect_factory()
    results, errors = run_concurrently(grab(conn_a, "op-a"), grab(conn_b, "op-b"))

    winners = [r for r in results if r is not None]
    losers = [e for e in errors if e is not None]

    assert len(winners) == 1, f"expected one operator to win, got {results}"
    assert len(losers) == 1
    assert getattr(losers[0], "sqlstate", None) == "RE001"

    with db.cursor() as cur:
        cur.execute("SELECT claimed_by, claim_generation FROM handoff WHERE id = %s", (handoff_id,))
        handoff = cur.fetchone()
        cur.execute("SELECT human_owner_id FROM workflow WHERE id = %s", (workflow_id,))
        owner = cur.fetchone()["human_owner_id"]

    assert handoff["claimed_by"] == winners[0]
    assert handoff["claim_generation"] == 1
    assert owner == winners[0]


def test_an_expired_claim_is_reclaimable(db):
    """An operator who walks away does not freeze the workflow forever."""
    _workflow_id, handoff_id = escalated_workflow(db)
    handoff_claim(db, handoff_id, "op-1", 0, lease_seconds=0)

    taken_over = handoff_claim(db, handoff_id, "op-2", 1)

    assert taken_over["claimed_by"] == "op-2"
    assert taken_over["claim_generation"] == 2


def test_claim_generation_cannot_go_backwards(db):
    _workflow_id, handoff_id = escalated_workflow(db)
    handoff_claim(db, handoff_id, "op-1", 0)

    assert_rejects(
        db,
        "UPDATE handoff SET claim_generation = 0 WHERE id = %s",
        (handoff_id,),
        sqlstate="RE001",
        match="stale claim_generation",
    )


def test_only_one_live_ticket_per_workflow(db):
    workflow_id = make_workflow(db)
    raise_ticket(db, workflow_id)

    assert_rejects(
        db,
        "INSERT INTO handoff (workflow_id, ticket_ref, reason)"
        " VALUES (%s, 'T-1001', 'second escalation')",
        (workflow_id,),
        sqlstate="23505",
        match="handoff_one_live_per_workflow",
    )


# ---------------------------------------------------------------------------
# While a human owns it, automation cannot write
# ---------------------------------------------------------------------------


def test_human_ownership_suspends_automation_writes(db):
    """A constraint, not a hope. The workflow guard refuses the write outright,
    so an agent that has already started work cannot land its result."""
    workflow_id, handoff_id = escalated_workflow(db)
    handoff_claim(db, handoff_id, "op-1", 0)

    assert_rejects(
        db,
        "UPDATE workflow SET specialty = 'cardiology' WHERE id = %s",
        (workflow_id,),
        sqlstate="RE003",
        match="automation writes are suspended",
    )


def test_a_stale_worker_cannot_advance_a_human_owned_workflow(db):
    """There are two layers here, and both refuse.

    The claim cleared the machine owner, so `workflow_advance` finds no row to
    update (RE001) and never reaches the guard. A caller that bypasses
    workflow_advance and writes the row directly hits the guard instead (RE003,
    asserted above). Neither path writes.
    """
    workflow_id = make_workflow(db)
    row = claim(db, workflow_id, "worker-1")
    handoff_id = raise_ticket(db, workflow_id)
    handoff_claim(db, handoff_id, "op-1", 0)

    assert_rejects(
        db,
        "SELECT * FROM workflow_advance(%s, %s, %s, %s, %s, %s)",
        (workflow_id, "worker-1", row["generation"], row["state_version"], "PROVIDER_MATCHING", 60),
        sqlstate="RE001",
    )


def test_releasing_a_ticket_returns_the_workflow_to_automation(db):
    workflow_id, handoff_id = escalated_workflow(db)
    handoff_claim(db, handoff_id, "op-1", 0)

    with db.cursor() as cur:
        cur.execute("SELECT * FROM handoff_release(%s, %s, %s)", (handoff_id, "op-1", 1))

    # Automation can write again.
    with db.cursor() as cur:
        cur.execute("UPDATE workflow SET specialty = 'cardiology' WHERE id = %s", (workflow_id,))
        cur.execute("SELECT human_owner_id, specialty FROM workflow WHERE id = %s", (workflow_id,))
        row = cur.fetchone()

    assert row["human_owner_id"] is None
    assert row["specialty"] == "cardiology"


# ---------------------------------------------------------------------------
# Resolving a ticket is not consent
# ---------------------------------------------------------------------------


def test_resolving_a_ticket_cannot_move_a_workflow_into_booking(db):
    """The allow-list in handoff_resolve is the guard: an operator decision is
    an outcome for the ticket, never an authorisation to book."""
    _workflow_id, handoff_id = escalated_workflow(db)
    handoff_claim(db, handoff_id, "op-1", 0)

    assert_rejects(
        db,
        "SELECT * FROM handoff_resolve(%s, %s, %s, %s, %s)",
        (handoff_id, "op-1", 1, "looks fine to me", "BOOKING_IN_FLIGHT"),
        sqlstate="RE004",
        match="cannot move a workflow to",
    )


def test_resolving_a_ticket_cannot_mark_a_workflow_booked(db):
    _workflow_id, handoff_id = escalated_workflow(db)
    handoff_claim(db, handoff_id, "op-1", 0)

    assert_rejects(
        db,
        "SELECT * FROM handoff_resolve(%s, %s, %s, %s, %s)",
        (handoff_id, "op-1", 1, "book it", "BOOKED"),
        sqlstate="RE004",
        match="cannot move a workflow to",
    )


def test_an_operator_cannot_write_consent_by_hand(db):
    """Reaching around handoff_resolve does not help either: the approval guard
    refuses an approval recorded by a human actor, so there is no path from the
    operator console to a live consent."""
    workflow_id, handoff_id = escalated_workflow(db)
    handoff_claim(db, handoff_id, "op-1", 0)
    as_actor(db, "human", "op-1")

    assert_rejects(
        db,
        "INSERT INTO approval (workflow_id, patient_ref, evidence_id, state_version,"
        " booking_payload_hash, channel, approved_by, expires_at)"
        " VALUES (%s, 'P-77', gen_random_uuid(), 1, %s,"
        " 'patient_self_service', 'op-1', now() + interval '1 hour')",
        (workflow_id, "a" * 64),
        sqlstate="RE004",
        match="human actor cannot grant booking consent",
    )


def test_a_confirmed_booking_cannot_be_fabricated_by_an_operator(db):
    """Handoff resolution writes a workflow row. It cannot conjure the confirmed
    booking_request that `booked` also requires."""
    workflow_id, handoff_id = escalated_workflow(db)
    handoff_claim(db, handoff_id, "op-1", 0)

    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM handoff_resolve(%s, %s, %s, %s, %s)",
            (handoff_id, "op-1", 1, "resolved by hand", "PROVIDER_MATCHING"),
        )

    assert_rejects(
        db,
        "UPDATE workflow SET state = 'BOOKED', state_version = state_version + 1 WHERE id = %s",
        (workflow_id,),
        sqlstate="RE004",
        match="no confirmed booking_request",
    )


def test_a_resolution_returns_the_workflow_to_the_allow_listed_state(db):
    """The legitimate path, so the refusals above are meaningful."""
    workflow_id, handoff_id = escalated_workflow(db)
    handoff_claim(db, handoff_id, "op-1", 0)

    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM handoff_resolve(%s, %s, %s, %s, %s)",
            (handoff_id, "op-1", 1, "duplicate referral", "PROVIDER_MATCHING"),
        )
        resolved = cur.fetchone()
        cur.execute("SELECT state, human_owner_id FROM workflow WHERE id = %s", (workflow_id,))
        workflow = cur.fetchone()

    assert resolved["state"] == "resolved"
    assert resolved["resolution"] == "duplicate referral"
    assert workflow["state"] == "PROVIDER_MATCHING"
    assert workflow["human_owner_id"] is None


def test_the_actor_context_is_restored_after_resolution(db):
    """handoff_resolve borrows the human actor identity for the workflow update
    and puts it back, so the rest of the transaction is unaffected."""
    _workflow_id, handoff_id = escalated_workflow(db)
    handoff_claim(db, handoff_id, "op-1", 0)

    with db.cursor() as cur:
        cur.execute("SELECT current_actor_kind() AS before")
        before = cur.fetchone()["before"]

        cur.execute(
            "SELECT * FROM handoff_resolve(%s, %s, %s, %s, %s)",
            (handoff_id, "op-1", 1, "done", "PROVIDER_MATCHING"),
        )

        cur.execute("SELECT current_actor_kind() AS after")
        after = cur.fetchone()["after"]

    assert before == "automation"
    assert after == before, "the borrowed actor identity leaked out of the function"


def test_the_queue_view_resolves_claim_liveness(db):
    _workflow_id, handoff_id = escalated_workflow(db)
    handoff_claim(db, handoff_id, "op-1", 0)

    with db.cursor() as cur:
        cur.execute("SELECT claim_is_live, state FROM handoff_queue WHERE id = %s", (handoff_id,))
        row = cur.fetchone()

    assert row["state"] == "claimed"
    assert row["claim_is_live"] is True
