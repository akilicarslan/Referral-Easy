"""Integration tests for the operator handoff (§12).

The rule under test: **a human taking over suspends automation, and a human resolving a ticket is
not booking consent.**

Those two sentences are what the schema encodes, and this file is where the client is shown to carry
them rather than to weaken them:

* claiming sets `workflow.human_owner_id`, and every worker write is then refused (`HumanOwnedError`)
  -- automation does not have to remember to stand down, and cannot forget;
* `handoff_resolve` bounds the state an operator may produce with an allow-list that contains no
  pre-booking state, so a ticket cannot be resolved *into* consent. The list is not reproduced in
  Python on purpose: a second copy would be the copy that got it wrong.
"""

from __future__ import annotations

import pytest

from referraleasy_domain import (
    ApprovalRefusedError,
    Handoff,
    HandoffStore,
    StaleOwnershipError,
)

pytestmark = pytest.mark.integration

#: Seeded independently of each other, so a case can be refused by one and not the other.
OPEN = 0

# The `refusals` fixture asserts the type of a refusal and contains it in a savepoint. Where the
# *reason* is part of the claim -- which guard refused, and the hint it carries -- the savepoint is
# written out instead, because inspecting the exception needs `pytest.raises` to be the outer block:
# the transaction block is what rolls back to the savepoint, so it has to see the exception. Caught
# inside it, the block would release the savepoint of an aborted transaction and leave the
# connection unusable.


@pytest.fixture
def handoffs(db) -> HandoffStore:
    return HandoffStore(db)


@pytest.fixture
def ticket(seed, store):
    """A workflow owned by a worker, with an open ticket against it.

    Returns `(workflow_id, handoff_id)`. The workflow is claimed first because that is the situation
    a handoff arises from: automation was working on it and had to stop.
    """
    workflow_id = seed.workflow()
    store.claim(workflow_id, "w-1", expected_generation=0)
    return workflow_id, seed.handoff(workflow_id)


# ---------------------------------------------------------------------------
# Taking the ticket
# ---------------------------------------------------------------------------


def test_claim_takes_the_ticket_and_human_ownership(ticket, handoffs, store) -> None:
    workflow_id, handoff_id = ticket

    claimed = handoffs.claim(handoff_id, "op-1", expected_generation=OPEN)

    assert isinstance(claimed, Handoff)
    assert claimed.state == "claimed"
    assert claimed.claimed_by == "op-1"
    assert claimed.claimed_at is not None
    assert claimed.claim_expires_at is not None
    # The generation is the ticket's own compare-and-set counter, not the workflow's.
    assert claimed.claim_generation == 1
    assert claimed.is_open

    # And with the ticket comes the workflow: the human owns both.
    workflow = store.load(workflow_id)
    assert workflow is not None
    assert workflow.human_owner_id == "op-1"
    assert workflow.is_human_owned
    # Automation's lease is cleared, so no worker can claim to still be holding it.
    assert workflow.owner_worker_id is None
    assert workflow.lease_expires_at is None


def test_automation_cannot_write_a_workflow_a_human_owns(ticket, handoffs, store, db) -> None:
    """The suspension is total, and it arrives as "you do not own this" (RE001).

    Worth pinning exactly, because the refusal code is not the one a reader expects. Both
    `workflow_claim` and `workflow_advance` require `human_owner_id IS NULL` inside their
    compare-and-set, so the write matches no row and the workflow guard never sees it -- the
    suspension is applied before the row is even handed over. RE003 (the guard's own "automation
    writes are suspended") is for the writes that *do* match, which is why `handoff_claim` has to
    attribute its own workflow update to the operator; see the test below.

    Either code means the same thing to a caller: the write did not happen.
    """
    workflow_id, handoff_id = ticket
    handoffs.claim(handoff_id, "op-1", expected_generation=OPEN)
    held = store.load(workflow_id)
    assert held is not None

    with pytest.raises(StaleOwnershipError) as refusal, db.transaction():
        store.advance(
            workflow_id,
            "w-1",
            expected_generation=held.generation,
            expected_state_version=held.state_version,
            to_state="COVERAGE_CHECK",
        )

    assert refusal.value.sqlstate == "RE001"
    assert "owner differs" in str(refusal.value)
    # The caller read the row correctly and still could not write it: the human owns it now.
    unchanged = store.load(workflow_id)
    assert unchanged is not None
    assert unchanged.state == "INTAKE"
    assert unchanged.human_owner_id == "op-1"


def test_the_guard_refuses_any_automation_write_that_reaches_the_row(ticket, handoffs, db) -> None:
    """The RE003 tripwire, shown to be live even though no client method can trip it.

    The client's own paths are already stopped by their WHERE clauses, so this test writes the way a
    future caller might: a plain UPDATE, which matches the row and reaches the guard. That is the
    point -- the rule holds for writers this package has never heard of, so it cannot be lost by
    someone forgetting a predicate.
    """
    import psycopg

    workflow_id, handoff_id = ticket
    handoffs.claim(handoff_id, "op-1", expected_generation=OPEN)

    with pytest.raises(psycopg.DatabaseError) as refusal, db.transaction(), db.cursor() as cur:
        cur.execute("UPDATE workflow SET state = 'COVERAGE_CHECK' WHERE id = %s", (workflow_id,))

    assert refusal.value.sqlstate == "RE003"
    assert "automation writes are suspended" in str(refusal.value)


def test_a_second_operator_at_the_same_generation_loses(ticket, handoffs, db) -> None:
    """Two operators, one ticket, one UPDATE that matches: exactly one of them gets it."""
    _workflow_id, handoff_id = ticket
    handoffs.claim(handoff_id, "op-1", expected_generation=OPEN)

    with pytest.raises(StaleOwnershipError) as refusal, db.transaction():
        handoffs.claim(handoff_id, "op-2", expected_generation=OPEN)

    assert refusal.value.sqlstate == "RE001"
    assert "another operator holds a live claim" in str(refusal.value)


def test_an_expired_claim_can_be_taken_over(ticket, handoffs, store) -> None:
    """An operator who walks away must not strand the ticket.

    This is also the path that writes a workflow already owned by a human: the second operator takes
    over the workflow's ownership, which a non-human writer would be refused for. It is worth
    pinning, because it is the one place the client ends up on the wrong side of its own guard.
    """
    workflow_id, handoff_id = ticket
    handoffs.claim(handoff_id, "op-1", expected_generation=OPEN, lease_seconds=-1)

    second = handoffs.claim(handoff_id, "op-2", expected_generation=1)

    assert second.claimed_by == "op-2"
    assert second.claim_generation == 2
    workflow = store.load(workflow_id)
    assert workflow is not None
    assert workflow.human_owner_id == "op-2"


# ---------------------------------------------------------------------------
# Giving it back
# ---------------------------------------------------------------------------


def test_release_hands_the_ticket_back_and_resumes_automation(ticket, handoffs, store) -> None:
    workflow_id, handoff_id = ticket
    handoffs.claim(handoff_id, "op-1", expected_generation=OPEN)

    released = handoffs.release(handoff_id, "op-1", expected_generation=1)

    assert released.state == "open"
    assert released.claimed_by is None
    assert released.claim_expires_at is None
    assert released.claim_generation == 2

    # Automation may write again -- after re-claiming, because releasing returns the workflow to the
    # pool rather than to the worker that had it before.
    returned = store.load(workflow_id)
    assert returned is not None
    assert returned.human_owner_id is None
    reclaimed = store.claim(workflow_id, "w-1", expected_generation=returned.generation)
    advanced = store.advance(
        reclaimed.id,
        "w-1",
        expected_generation=reclaimed.generation,
        expected_state_version=reclaimed.state_version,
        to_state="COVERAGE_CHECK",
    )
    assert advanced.state == "COVERAGE_CHECK"


def test_somebody_else_cannot_release_your_claim(ticket, handoffs, db) -> None:
    _workflow_id, handoff_id = ticket
    handoffs.claim(handoff_id, "op-1", expected_generation=OPEN)

    with pytest.raises(StaleOwnershipError) as refusal, db.transaction():
        handoffs.release(handoff_id, "op-2", expected_generation=1)

    assert "not claimed by" in str(refusal.value)


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------


def test_resolve_closes_the_ticket_and_returns_the_workflow_to_an_allowed_state(
    ticket, handoffs, store
) -> None:
    workflow_id, handoff_id = ticket
    handoffs.claim(handoff_id, "op-1", expected_generation=OPEN)
    before = store.load(workflow_id)
    assert before is not None

    # The default target, which the migration's own signature also defaults to.
    resolved = handoffs.resolve(
        handoff_id,
        "op-1",
        expected_generation=1,
        resolution="Referred back to provider matching",
        note="Two candidates declined; directory refreshed.",
    )

    assert resolved.state == "resolved"
    assert resolved.resolved_at is not None
    assert resolved.resolution == "Referred back to provider matching"
    assert resolved.resolution_note == "Two candidates declined; directory refreshed."
    assert not resolved.is_open

    after = store.load(workflow_id)
    assert after is not None
    assert after.state == "PROVIDER_MATCHING"
    assert after.human_owner_id is None
    assert after.state_version == before.state_version + 1
    # The workflow moved on, so a worker holding the old epoch has to re-read before writing.
    assert after.generation > before.generation


def test_an_operator_cannot_resolve_the_workflow_into_a_booking_state(
    ticket, handoffs, store, db
) -> None:
    """THE rule: resolving a ticket is not consent.

    `BOOKING_IN_FLIGHT` and `AWAITING_FINAL_APPROVAL` are the states on the way to a booking, and
    they are absent from the allow-list by design -- an operator who could reach them could book
    without the patient ever agreeing. `BOOKED` is refused for the same reason: the route to a
    booking is a live approval, and there is none here.
    """
    workflow_id, handoff_id = ticket
    handoffs.claim(handoff_id, "op-1", expected_generation=OPEN)

    with pytest.raises(ApprovalRefusedError) as refusal, db.transaction():
        handoffs.resolve(
            handoff_id,
            "op-1",
            expected_generation=1,
            resolution="booked it for them",
            workflow_state="BOOKING_IN_FLIGHT",
        )

    assert refusal.value.sqlstate == "RE004"
    # The refusal explains itself, so the operator learns the route they are missing.
    assert "Booking consent comes from the patient" in str(refusal.value)
    # And it is total: the ticket is still open, and the workflow is exactly where it was.
    still_claimed = store.load(workflow_id)
    assert still_claimed is not None
    assert still_claimed.state == "INTAKE"
    assert still_claimed.human_owner_id == "op-1"


@pytest.mark.parametrize("forbidden", ["BOOKING_IN_FLIGHT", "AWAITING_FINAL_APPROVAL", "BOOKED"])
def test_every_state_on_the_way_to_a_booking_is_refused(
    ticket, handoffs, refusals, forbidden: str
) -> None:
    _workflow_id, handoff_id = ticket
    handoffs.claim(handoff_id, "op-1", expected_generation=OPEN)

    with refusals(ApprovalRefusedError):
        handoffs.resolve(
            handoff_id,
            "op-1",
            expected_generation=1,
            resolution="closing it out",
            workflow_state=forbidden,
        )


def test_the_allow_list_is_checked_before_anything_that_could_leak_state(
    ticket, handoffs, refusals
) -> None:
    """A forbidden target is refused even when the claim is stale.

    The order matters: if the ticket were inspected first, an operator could learn which tickets are
    held by whom by passing an illegal state and reading the error. The allow-list is a property of
    the queue, so it answers first.
    """
    _workflow_id, handoff_id = ticket
    handoffs.claim(handoff_id, "op-1", expected_generation=OPEN)

    with refusals(ApprovalRefusedError):
        handoffs.resolve(
            handoff_id,
            "op-2",  # not the holder
            expected_generation=0,  # and stale
            resolution="booked it",
            workflow_state="BOOKED",
        )


def test_somebody_else_cannot_resolve_your_ticket(ticket, handoffs, refusals) -> None:
    _workflow_id, handoff_id = ticket
    handoffs.claim(handoff_id, "op-1", expected_generation=OPEN)

    with refusals(StaleOwnershipError):
        handoffs.resolve(handoff_id, "op-2", expected_generation=1, resolution="handled offline")


def test_resolving_at_a_generation_the_ticket_has_moved_past_is_refused(
    ticket, handoffs, refusals
) -> None:
    _workflow_id, handoff_id = ticket
    handoffs.claim(handoff_id, "op-1", expected_generation=OPEN)

    with refusals(StaleOwnershipError):
        handoffs.resolve(handoff_id, "op-1", expected_generation=OPEN, resolution="stale read")


def test_a_resolved_ticket_stays_resolved(ticket, handoffs, db) -> None:
    """Closed is closed: a resolution is not a state an operator can undo by re-claiming."""
    _workflow_id, handoff_id = ticket
    handoffs.claim(handoff_id, "op-1", expected_generation=OPEN)
    resolved = handoffs.resolve(handoff_id, "op-1", expected_generation=1, resolution="done")

    with pytest.raises(StaleOwnershipError) as refusal, db.transaction():
        handoffs.claim(handoff_id, "op-2", expected_generation=resolved.claim_generation)

    assert "the ticket is closed" in str(refusal.value)
