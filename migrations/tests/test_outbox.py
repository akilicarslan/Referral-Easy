"""The queue is this table.

There is no broker, so the properties a broker would provide have to be tested
here: a row is handed to exactly one worker, a lease that lapses is reclaimable,
and a row that keeps failing reaches a terminal state instead of looping.
"""

from __future__ import annotations

import pytest
from migration_fixtures import assert_rejects, run_concurrently

pytestmark = pytest.mark.integration


def enqueue(db, dedupe_key: str = "d-1", kind: str = "send_notification") -> str:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO outbox (dedupe_key, kind) VALUES (%s, %s) RETURNING id",
            (dedupe_key, kind),
        )
        return str(cur.fetchone()["id"])


def claim(conn, worker: str, limit: int = 10, lease_seconds: int = 60) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM outbox_claim(%s, %s, %s)", (worker, limit, lease_seconds))
        return cur.fetchall()


# ---------------------------------------------------------------------------
# SKIP LOCKED: one row, one worker
# ---------------------------------------------------------------------------


def test_skip_locked_does_not_hand_the_same_row_to_two_workers(db, connect_factory):
    """The core retrieval guarantee.

    Worker A takes the row and holds its transaction open. Worker B, running the
    same claim concurrently, must not receive it -- and must not sit waiting for
    it either. SKIP LOCKED is what makes both true at once; a plain FOR UPDATE
    would deliver the row once but block every other worker behind the lease.
    """
    enqueue(db)
    db.commit()

    worker_a, worker_b = connect_factory(), connect_factory()
    # If B blocks on A's lock instead of skipping it, this turns the hang into
    # a failure rather than a stuck test run.
    worker_b.execute("SET statement_timeout = '3s'")

    claimed_a = claim(worker_a, "worker-a")
    claimed_b = claim(worker_b, "worker-b")

    assert len(claimed_a) == 1
    assert claimed_a[0]["claimed_by"] == "worker-a"
    assert claimed_b == [], "the same row was handed to a second worker"


def test_a_row_already_leased_is_not_claimable(db, connect_factory):
    """Retrieval is exclusive for the whole lease, not just while the claim
    transaction happens to be open."""
    enqueue(db)
    db.commit()

    worker_a = connect_factory()
    claimed_a = claim(worker_a, "worker-a")
    worker_a.commit()

    worker_b = connect_factory()
    assert claim(worker_b, "worker-b") == []
    assert claimed_a[0]["claimed_until"] > claimed_a[0]["next_attempt_at"]


def test_an_expired_lease_is_reclaimable(db, connect_factory):
    """A worker that dies mid-delivery must not wedge the row forever."""
    enqueue(db)
    db.commit()

    worker_a = connect_factory()
    claim(worker_a, "worker-a", lease_seconds=0)
    worker_a.commit()

    worker_b = connect_factory()
    reclaimed = claim(worker_b, "worker-b")

    assert len(reclaimed) == 1
    assert reclaimed[0]["claimed_by"] == "worker-b"
    assert reclaimed[0]["attempt_count"] == 2


def test_two_workers_claiming_concurrently_each_get_a_different_row(db, connect_factory):
    enqueue(db, dedupe_key="d-1")
    enqueue(db, dedupe_key="d-2")
    db.commit()

    def grab(conn, worker):
        def run():
            rows = claim(conn, worker, limit=1)
            conn.commit()
            return {row["dedupe_key"] for row in rows}

        return run

    conn_a, conn_b = connect_factory(), connect_factory()
    results, errors = run_concurrently(grab(conn_a, "worker-a"), grab(conn_b, "worker-b"))

    assert not [e for e in errors if e is not None], errors
    # Which worker gets which row is genuinely up to the scheduler; that both
    # rows were handed out exactly once is not.
    handed_out = sorted(key for batch in results for key in batch)
    assert handed_out == ["d-1", "d-2"], f"rows were duplicated or dropped: {results}"


def test_terminal_rows_are_never_claimed_again(db):
    """Both end states are excluded from retrieval: a delivered notification is
    not re-sent, and a dead one is not retried."""
    delivered = enqueue(db, dedupe_key="d-delivered")
    dead = enqueue(db, dedupe_key="d-dead")

    with db.cursor() as cur:
        cur.execute(
            "UPDATE outbox SET status = 'delivered', attempt_count = max_attempts,"
            " delivered_at = now(), terminal_at = now() WHERE id = %s",
            (delivered,),
        )
        cur.execute(
            "UPDATE outbox SET status = 'dead', attempt_count = max_attempts,"
            " terminal_at = now(), last_error = 'gave up' WHERE id = %s",
            (dead,),
        )
        cur.execute("SELECT * FROM outbox_claim('worker-a', 10, 60)")
        assert cur.fetchall() == []


def test_an_exhausted_row_is_not_handed_out(db):
    """An exhausted row is not claimable, so a permanently failing delivery
    cannot be picked up for one more doomed attempt."""
    enqueue(db)
    db.cursor().execute(
        "UPDATE outbox SET attempt_count = max_attempts, status = 'pending' WHERE dedupe_key = 'd-1'"
    )

    with db.cursor() as cur:
        cur.execute("SELECT * FROM outbox_claim('worker-a', 10, 60)")
        assert cur.fetchall() == []


# ---------------------------------------------------------------------------
# Settlement, backoff, terminal state
# ---------------------------------------------------------------------------


def test_success_is_terminal(db):
    enqueue(db)
    row = claim(db, "worker-a")[0]

    with db.cursor() as cur:
        cur.execute("SELECT * FROM outbox_settle(%s, %s, true)", (row["id"], "worker-a"))
        settled = cur.fetchone()

    assert settled["status"] == "delivered"
    assert settled["delivered_at"] is not None
    assert settled["terminal_at"] is not None
    assert settled["claimed_by"] is None


def test_a_failure_backs_off_rather_than_retrying_immediately(db):
    enqueue(db)
    row = claim(db, "worker-a")[0]

    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM outbox_settle(%s, %s, false, %s)",
            (row["id"], "worker-a", "smtp refused"),
        )
        settled = cur.fetchone()

    assert settled["status"] == "pending"
    assert settled["last_error"] == "smtp refused"
    assert settled["attempt_count"] == 1
    assert settled["claimed_by"] is None
    assert settled["terminal_at"] is None, "a retryable row is not terminal"
    assert settled["next_attempt_at"] > settled["created_at"], (
        "the retry was scheduled in the future"
    )


def test_a_row_dies_once_its_attempt_budget_is_spent(db):
    """The terminal state: a notification that keeps failing stops."""
    enqueue(db)
    with db.cursor() as cur:
        cur.execute("UPDATE outbox SET max_attempts = 2 WHERE dedupe_key = 'd-1'")

    # Attempt 1: fails, goes back to pending with a backoff.
    first = claim(db, "worker-a")[0]
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM outbox_settle(%s, %s, false, %s)", (first["id"], "worker-a", "boom 1")
        )
        assert cur.fetchone()["status"] == "pending"

    # The backoff elapses.
    db.cursor().execute(
        "UPDATE outbox SET next_attempt_at = now() - interval '1 second' WHERE dedupe_key = 'd-1'"
    )

    # Attempt 2: the budget is now spent, so the failure is terminal.
    second = claim(db, "worker-a")[0]
    assert second["attempt_count"] == 2
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM outbox_settle(%s, %s, false, %s)", (second["id"], "worker-a", "boom 2")
        )
        dead = cur.fetchone()

    assert dead["status"] == "dead"
    assert dead["terminal_at"] is not None

    # And it stays dead.
    with db.cursor() as cur:
        cur.execute("SELECT * FROM outbox_claim('worker-b', 10, 60)")
        assert cur.fetchall() == []


def test_only_the_holding_worker_may_settle(db, connect_factory):
    # Imported here, not at module scope: test modules are imported during
    # collection even when their tests are deselected, and the unit tests must
    # keep working in an environment with no database driver at all.
    import psycopg

    enqueue(db)
    db.commit()

    worker_a = connect_factory()
    row = claim(worker_a, "worker-a")[0]
    worker_a.commit()

    worker_b = connect_factory()
    with pytest.raises(psycopg.Error) as excinfo, worker_b.cursor() as cur:
        cur.execute("SELECT * FROM outbox_settle(%s, %s, true)", (row["id"], "worker-b"))
    worker_b.rollback()

    assert excinfo.value.sqlstate == "RE007"


# ---------------------------------------------------------------------------
# Enqueue idempotency and shape
# ---------------------------------------------------------------------------


def test_the_same_notification_cannot_be_enqueued_twice(db):
    enqueue(db, dedupe_key="d-1")

    assert_rejects(
        db,
        "INSERT INTO outbox (dedupe_key, kind) VALUES ('d-1', 'send_notification')",
        (),
        sqlstate="23505",
    )


def test_a_claim_must_be_owner_and_deadline_together(db):
    """A half claim would look permanently leased, or permanently free. Both
    halves of the rule are enforced."""
    # Claimed, but with no deadline: nobody could ever tell it had lapsed.
    assert_rejects(
        db,
        "INSERT INTO outbox (dedupe_key, kind, status, claimed_by)"
        " VALUES ('d-9', 'send_notification', 'claimed', 'worker-a')",
        (),
        sqlstate="23514",  # check_violation
        match="outbox_claimed_has_owner",
    )

    # Not claimed, but carrying claim metadata: a stale owner left behind.
    assert_rejects(
        db,
        "INSERT INTO outbox (dedupe_key, kind, status, claimed_by)"
        " VALUES ('d-10', 'send_notification', 'pending', 'worker-a')",
        (),
        sqlstate="23514",
        match="outbox_claim_is_whole",
    )


def test_a_row_cannot_be_declared_dead_before_its_budget_is_spent(db):
    enqueue(db)

    assert_rejects(
        db,
        "UPDATE outbox SET status = 'dead', terminal_at = now(), last_error = 'nope'"
        " WHERE dedupe_key = 'd-1'",
        (),
        sqlstate="23514",
        match="outbox_dead_only_when_exhausted",
    )


def test_attempts_cannot_exceed_the_budget(db):
    enqueue(db)

    assert_rejects(
        db,
        "UPDATE outbox SET attempt_count = max_attempts + 1 WHERE dedupe_key = 'd-1'",
        (),
        sqlstate="23514",
        match="outbox_attempt_count_range",
    )


def test_health_view_surfaces_expired_leases(db):
    """A non-zero expired_leases count means a worker died holding deliveries,
    which is the one outbox condition an operator has to act on."""
    enqueue(db)
    claim(db, "worker-a", lease_seconds=0)

    with db.cursor() as cur:
        cur.execute("SELECT expired_leases FROM outbox_health WHERE status = 'claimed'")
        assert cur.fetchone()["expired_leases"] == 1
