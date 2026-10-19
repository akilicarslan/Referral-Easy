"""Integration tests for the outbox (§11).

The rule under test: **a notification is a delivery, not a transaction.** The queue is claimed in
batches, one attempt at a time, and a failure is recorded rather than thrown -- because the failure
that matters is the one that would otherwise travel backwards into a booking that has already been
made at the provider.

Two properties are load-bearing and are tested here as such:

* a claimed row belongs to exactly one worker until its lease ends, so two drains cannot deliver the
  same message twice;
* the attempt budget is bounded, so a permanently failing notification reaches a terminal `dead`
  state and stops being claimed. A queue that retried forever would be indistinguishable from an
  outage.
"""

from __future__ import annotations

import pytest

from referraleasy_domain import (
    DrainReport,
    OutboxClaimError,
    OutboxStatus,
    OutboxStore,
)

pytestmark = pytest.mark.integration


def _always_fails(message) -> None:
    """A delivery that cannot succeed, whatever it is handed."""
    raise RuntimeError("smtp down")


@pytest.fixture
def outbox(db) -> OutboxStore:
    return OutboxStore(db)


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


def test_claim_leases_due_rows_to_the_worker(seed, outbox) -> None:
    first = seed.outbox(dedupe_key="notify-1")
    second = seed.outbox(dedupe_key="notify-2")

    claimed = outbox.claim("w-1", limit=5)

    # Compared as a set: every row here was inserted in one transaction, so `next_attempt_at` and
    # `created_at` are the same instant for all of them and the claim order is not a promise.
    assert sorted(str(message.id) for message in claimed) == sorted([first, second])
    assert all(message.status == OutboxStatus.CLAIMED for message in claimed)
    assert all(message.claimed_by == "w-1" for message in claimed)
    assert all(message.claimed_until is not None for message in claimed)
    # Claiming spends an attempt: the budget counts deliveries, not failures.
    assert all(message.attempt_count == 1 for message in claimed)
    assert all(message.attempts_remaining == 4 for message in claimed)
    assert not any(message.is_terminal for message in claimed)


def test_claim_honours_the_limit(seed, outbox) -> None:
    for index in range(3):
        seed.outbox(dedupe_key=f"notify-{index}")

    assert len(outbox.claim("w-1", limit=1)) == 1
    assert len(outbox.claim("w-2", limit=2)) == 2


def test_a_row_with_a_live_lease_is_not_claimed_again(seed, outbox) -> None:
    seed.outbox()

    outbox.claim("w-1")
    second = outbox.claim("w-2")

    # Two workers, one message: the second sees nothing rather than a duplicate delivery.
    assert second == []


def test_a_row_whose_lease_expired_is_reclaimable(seed, outbox) -> None:
    """A worker that died mid-delivery must not strand its message.

    The lease is fabricated as already expired, because within one transaction `now()` does not
    advance -- waiting would make the test slow without making it more real.
    """
    seed.outbox()
    outbox.claim("w-1", lease_seconds=-1)

    reclaimed = outbox.claim("w-2")

    assert len(reclaimed) == 1
    assert reclaimed[0].claimed_by == "w-2"
    assert reclaimed[0].attempt_count == 2


def test_a_limit_below_one_is_refused_by_the_database(seed, outbox) -> None:
    """The client passes the limit through; the function is what insists it is meaningful."""
    seed.outbox()

    with pytest.raises(OutboxClaimError) as excinfo:
        outbox.claim("w-1", limit=0)

    assert excinfo.value.sqlstate == "RE007"
    assert "limit must be >= 1" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Settling
# ---------------------------------------------------------------------------


def test_settling_successfully_delivers_the_row_for_good(seed, outbox) -> None:
    seed.outbox()
    claimed = outbox.claim("w-1")[0]

    settled = outbox.settle(claimed.id, "w-1", ok=True)

    assert settled.status == OutboxStatus.DELIVERED
    assert settled.delivered_at is not None
    assert settled.is_terminal
    assert settled.claimed_by is None
    # Delivered is terminal: this message is never handed out again.
    assert outbox.claim("w-2") == []


def test_settling_a_failure_backs_off_and_keeps_the_row_pending(seed, outbox) -> None:
    seed.outbox()
    claimed = outbox.claim("w-1")[0]

    settled = outbox.settle(claimed.id, "w-1", ok=False, error="smtp: connection refused")

    assert settled.status == OutboxStatus.PENDING
    assert settled.last_error == "smtp: connection refused"
    assert settled.delivered_at is None
    assert settled.terminal_at is None
    assert settled.attempts_remaining == 4
    # The backoff is real, not decorative: the row is not due again yet.
    assert settled.next_attempt_at > claimed.next_attempt_at
    assert outbox.claim("w-2") == []


def test_a_permanently_failing_notification_stops_looping(seed, outbox) -> None:
    """The terminal path. Without it, a message nobody can deliver is claimed forever.

    `max_attempts` is the bound, and `dead` is where the row ends up: it is not pending (nothing
    will pick it up) and not delivered (nothing happened), and it never comes back.
    """
    seed.outbox(max_attempts=1)
    claimed = outbox.claim("w-1")
    assert len(claimed) == 1

    buried = outbox.settle(claimed[0].id, "w-1", ok=False, error="address rejected")

    assert buried.status == OutboxStatus.DEAD
    assert buried.is_terminal
    assert buried.terminal_at is not None
    assert buried.attempts_remaining == 0
    # And the queue agrees: the row is never offered again, to anyone.
    assert outbox.claim("w-1") == []
    assert outbox.claim("w-2") == []


def test_an_exhausted_row_is_not_claimed_even_when_it_looks_due(seed, outbox) -> None:
    """The budget is checked before the due time, not after: `attempt_count < max_attempts`."""
    seed.outbox(max_attempts=2, attempt_count=2)

    assert outbox.claim("w-1") == []


def test_settling_a_row_held_by_somebody_else_is_refused(seed, outbox, db) -> None:
    seed.outbox()
    claimed = outbox.claim("w-1")[0]

    # A savepoint rather than the `refusals` fixture, because this test wants the exception itself.
    # The raise has to be *outside* the block: the refused statement poisons the transaction, and the
    # block is what rolls it back to the savepoint -- so it must see the exception to do that.
    with pytest.raises(OutboxClaimError) as refusal, db.transaction():
        outbox.settle(claimed.id, "w-2", ok=True)

    assert refusal.value.sqlstate == "RE007"
    # The hint is the important part: the caller is told not to force the write.
    assert "another worker owns the row" in str(refusal.value)
    # The row is untouched: still claimed, still w-1's, still undelivered.
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, claimed_by, delivered_at FROM outbox WHERE id = %s", (claimed.id,)
        )
        assert cur.fetchone() == ("claimed", "w-1", None)


def test_settling_a_row_that_was_never_claimed_is_refused(seed, outbox) -> None:
    pending = seed.outbox()

    with pytest.raises(OutboxClaimError):
        outbox.settle(pending, "w-1", ok=True)


# ---------------------------------------------------------------------------
# Draining
# ---------------------------------------------------------------------------


def test_drain_delivers_the_queue_and_reports_what_it_did(seed, outbox) -> None:
    for index in range(3):
        seed.outbox(dedupe_key=f"notify-{index}")
    delivered: list[str] = []

    report = outbox.drain("w-1", lambda message: delivered.append(message.dedupe_key))

    assert sorted(delivered) == ["notify-0", "notify-1", "notify-2"]
    assert report.claimed == 3
    assert report.delivered == 3
    assert report.retried == 0
    assert report.dead == 0
    assert report.lost == 0
    assert report.settled == 3
    assert report.batches == 1


def test_drain_on_an_empty_queue_is_a_no_op(seed, outbox) -> None:
    report = outbox.drain("w-1", lambda message: None)

    assert report == DrainReport()
    assert report.claimed == 0


def test_drain_contains_a_failing_handler(seed, outbox, db) -> None:
    """A handler that raises must not become the caller's exception.

    `deliver` is arbitrary caller code -- an HTTP call, a template render -- and every way it fails
    means the same thing to the queue. Letting it escape would abort the batch, and the rows that
    *could* have been delivered would be left for the next run.
    """
    good = seed.outbox(dedupe_key="notify-good")
    bad = seed.outbox(dedupe_key="notify-bad")
    seen: list[str] = []

    def deliver(message) -> None:
        if message.dedupe_key == "notify-bad":
            raise RuntimeError("provider returned 503")
        seen.append(message.dedupe_key)

    report = outbox.drain("w-1", deliver)

    assert seen == ["notify-good"]
    assert report.delivered == 1
    assert report.retried == 1
    assert report.settled == 2
    # The failure is recorded where a human can find it, with the exception that caused it. Read
    # directly rather than re-claimed: the backoff means the row is deliberately not due yet.
    with db.cursor() as cur:
        cur.execute("SELECT status, last_error FROM outbox WHERE id = %s", (bad,))
        assert cur.fetchone() == ("pending", "RuntimeError: provider returned 503")
        cur.execute("SELECT status, delivered_at FROM outbox WHERE id = %s", (good,))
        assert cur.fetchone()[0] == "delivered"


def test_drain_buries_a_row_whose_budget_is_spent(seed, outbox) -> None:
    seed.outbox(max_attempts=1)

    report = outbox.drain("w-1", _always_fails)

    assert report.dead == 1
    assert report.retried == 0
    assert outbox.claim("w-2") == []


def test_drain_stops_at_max_batches(seed, outbox) -> None:
    """One call is one batch: the cap is what lets a caller share the worker with other work."""
    for index in range(3):
        seed.outbox(dedupe_key=f"notify-{index}")

    report = outbox.drain("w-1", lambda message: None, limit=1, max_batches=1)

    assert report.batches == 1
    assert report.claimed == 1
    # The rest are still pending, so nothing was lost by stopping early.
    assert len(outbox.claim("w-2", limit=10)) == 2


def test_a_lost_lease_is_counted_rather_than_settled(seed, outbox) -> None:
    """A row taken over mid-delivery belongs to the other worker now; this drain must not write it.

    The stolen lease is real: the drain claims with an already-expired lease, and the handler -- the
    slow part -- is where the second worker takes over. The settle then fails its `claimed_by`
    compare-and-set, which is the outbox's version of "a stale worker cannot write".
    """
    seed.outbox()
    report = _drain_with_a_thief(outbox)

    assert report.claimed == 1
    assert report.lost == 1
    assert report.settled == 0
    # Every claimed row is accounted for: delivered, retried, buried, or lost to another worker.
    assert report.claimed == report.settled + report.lost

    held = outbox.claim("w-1")
    assert held == []  # w-2 still holds a live lease


def _drain_with_a_thief(outbox: OutboxStore) -> DrainReport:
    def deliver(message) -> None:
        # A second worker claims the row between this drain's claim and its settle.
        assert len(outbox.claim("w-2")) == 1

    return outbox.drain("w-1", deliver, lease_seconds=-1)


# ---------------------------------------------------------------------------
# The invariant: a notification failure is not a booking failure
# ---------------------------------------------------------------------------


def test_a_failed_notification_cannot_undo_a_booking(seed, store, outbox, db) -> None:
    """The booking is committed work; the notification is a message about it.

    The two are deliberately not one transaction. The handler here fails while the booking is
    already recorded, and the result is a pending notification with an error on it -- not a lost
    booking, and not an exception that would tempt a caller into rolling one back.
    """
    workflow, approval_id, _payload_hash = seed.consent_ready()
    entered = store.advance(
        workflow.id,
        "w-1",
        expected_generation=workflow.generation,
        expected_state_version=workflow.state_version,
        to_state="BOOKING_IN_FLIGHT",
    )
    booking = store.record_booking(approval_id, "scheduling")
    seed.outbox(dedupe_key=f"booking:{booking.id}")

    report = outbox.drain("w-1", _always_fails)

    assert report.retried == 1
    # The booking survived the notification's failure, and is still exactly where it was.
    reloaded = store.load(entered.id)
    assert reloaded is not None
    assert reloaded.state == "BOOKING_IN_FLIGHT"
    assert reloaded.state_version == entered.state_version
    with db.cursor() as cur:
        cur.execute("SELECT state FROM booking_request WHERE id = %s", (booking.id,))
        assert cur.fetchone()[0] == "pending"
        cur.execute(
            "SELECT status, last_error FROM outbox WHERE dedupe_key = %s",
            (f"booking:{booking.id}",),
        )
        assert cur.fetchone() == ("pending", "RuntimeError: smtp down")
