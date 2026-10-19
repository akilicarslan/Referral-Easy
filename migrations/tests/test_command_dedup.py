"""Command deduplication: replay the same request, reject a different one.

The rejection branch is the one worth testing. An upsert that quietly keeps the
first row would return the first response for a second, different request --
which is how a retried command turns into the wrong thing happening twice.
"""

from __future__ import annotations

import pytest
from migration_fixtures import SHA_A, SHA_B, assert_rejects, run_concurrently

pytestmark = pytest.mark.integration


def _begin(db, command_id: str = "cmd-1", request_hash: str = SHA_A):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM command_begin(%s, %s)", (command_id, request_hash))
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Same command, same request: replay
# ---------------------------------------------------------------------------


def test_the_first_call_creates_an_in_progress_row(db):
    row = _begin(db)

    assert row["command_id"] == "cmd-1"
    assert row["status"] == "in_progress"
    assert row["request_hash"] == SHA_A


def test_a_retry_replays_the_stored_response(db):
    _begin(db)
    db.cursor().execute(
        "SELECT * FROM command_complete(%s, %s, %s, %s)",
        ("cmd-1", SHA_A, 201, '{"booking_id": "B-1"}'),
    )

    replay = _begin(db, "cmd-1", SHA_A)

    assert replay["status"] == "completed"
    assert replay["response_status"] == 201
    assert replay["response"] == {"booking_id": "B-1"}


def test_an_identical_retry_never_creates_a_second_row(db):
    _begin(db)
    _begin(db)

    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM command_dedup WHERE command_id = 'cmd-1'")
        assert cur.fetchone()["n"] == 1


def test_a_retry_that_arrives_before_the_first_completed_is_not_a_failure(db):
    """In-flight dedup: the second caller is told the first is still running,
    rather than starting a second execution."""
    _begin(db)
    replay = _begin(db)

    assert replay["status"] == "in_progress"
    assert replay["completed_at"] is None


# ---------------------------------------------------------------------------
# Same command, different request: REJECT
# ---------------------------------------------------------------------------


def test_a_different_request_under_the_same_command_id_is_rejected(db):
    _begin(db, "cmd-1", SHA_A)

    assert_rejects(
        db,
        "SELECT * FROM command_begin(%s, %s)",
        ("cmd-1", SHA_B),
        sqlstate="RE005",
        match="different request hash",
    )


def test_the_rejection_holds_even_after_the_command_completed(db):
    _begin(db)
    db.cursor().execute(
        "SELECT * FROM command_complete(%s, %s, %s, %s)",
        ("cmd-1", SHA_A, 201, '{"booking_id": "B-1"}'),
    )

    # The tempting shortcut would be to hand back the stored response here,
    # because the command_id matches. That would answer a question the caller
    # did not ask.
    assert_rejects(
        db,
        "SELECT * FROM command_replay(%s, %s)",
        ("cmd-1", SHA_B),
        sqlstate="RE005",
        match="hash mismatch",
    )


def test_the_stored_request_hash_cannot_be_rewritten(db):
    """Rewriting the hash would retroactively bless the second request."""
    _begin(db)

    assert_rejects(
        db,
        "UPDATE command_dedup SET request_hash = %s WHERE command_id = 'cmd-1'",
        (SHA_B,),
        sqlstate="RE005",
        match="cannot be rewritten",
    )


def test_a_completed_command_cannot_be_returned_to_in_progress(db):
    _begin(db)
    db.cursor().execute(
        "SELECT * FROM command_complete(%s, %s, %s, %s)",
        ("cmd-1", SHA_A, 201, "{}"),
    )

    assert_rejects(
        db,
        "UPDATE command_dedup SET status = 'in_progress', completed_at = NULL"
        " WHERE command_id = 'cmd-1'",
        (),
        sqlstate="RE005",
        match="cannot return to in_progress",
    )


def test_completion_must_name_the_same_request_hash(db):
    _begin(db, "cmd-1", SHA_A)

    assert_rejects(
        db,
        "SELECT * FROM command_complete(%s, %s, %s, %s)",
        ("cmd-1", SHA_B, 200, "{}"),
        sqlstate="RE005",
        match="cannot be completed",
    )


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_arrivals_of_one_command_create_one_row(db, connect_factory):
    """Two clients retry the same command at the same moment. The unique key on
    command_id is what makes that one row and not two."""
    db.commit()

    def begin(conn, request_hash: str):
        def run():
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM command_begin(%s, %s)", ("cmd-race", request_hash))
                row = cur.fetchone()
            conn.commit()
            return row["command_id"]

        return run

    conn_a, conn_b = connect_factory(), connect_factory()
    results, errors = run_concurrently(begin(conn_a, SHA_A), begin(conn_b, SHA_A))

    assert results == ["cmd-race", "cmd-race"], errors
    assert not [e for e in errors if e is not None]

    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM command_dedup WHERE command_id = 'cmd-race'")
        assert cur.fetchone()["n"] == 1
