"""WS9 S3 (ADR-0051): a kill switch is a row, written through one audited function.

What these tests pin is what makes a switch a *control* rather than a flag: the change names who made
it (taken from the transaction, not from an argument), lands in the hash-chained audit in the same
transaction, cannot be made by writing the table directly, and leaves a history nobody can rewrite.
"""

from __future__ import annotations

import pytest
from migration_fixtures import as_actor, assert_rejects

pytestmark = pytest.mark.integration


def _set(db, scope: str, target: str, mode: str, reason: str = "incident 42", expires=None):
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM control_switch_set(%s, %s, %s, %s, %s)",
            (scope, target, mode, reason, expires),
        )
        return cur.fetchone()


def _audit(db) -> list[dict]:
    with db.cursor() as cur:
        cur.execute("SELECT * FROM audit WHERE action = 'control.switch' ORDER BY seq")
        return cur.fetchall()


def _chain_problems(db) -> list[dict]:
    with db.cursor() as cur:
        cur.execute("SELECT * FROM audit_verify_chain()")
        return cur.fetchall()


def test_a_switch_change_writes_one_audit_row_with_the_session_actor(db):
    as_actor(db, "human", "admin-7")
    row = _set(db, "tool", "create_booking", "disable")

    assert row["engaged_by"] == "admin-7"
    assert row["engaged_by_kind"] == "human"
    [entry] = _audit(db)
    assert entry["actor_id"] == "admin-7"
    assert entry["actor_kind"] == "human"
    assert entry["entity_id"] == "tool:create_booking"
    assert entry["detail"]["mode"] == "disable"
    assert entry["detail"]["epoch"] == row["epoch"]
    assert _chain_problems(db) == []


def test_the_function_has_no_actor_argument_to_lie_with(db):
    """Attribution is the transaction's: there is no parameter a caller could fill with someone else."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT pg_get_function_arguments(oid) AS args FROM pg_proc "
            "WHERE proname IN ('control_switch_set', 'control_switch_release')"
        )
        signatures = [r["args"] for r in cur.fetchall()]
    assert len(signatures) == 2
    assert not any("actor" in s or "_by" in s for s in signatures), signatures


def test_an_anonymous_change_is_refused(db):
    as_actor(db, "automation")
    assert_rejects(
        db,
        "SELECT control_switch_set('global', '*', 'read_only', 'nobody')",
        sqlstate="RE009",
        match="named actor",
    )


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO control_switch (scope, target, mode, reason, engaged_by, engaged_by_kind, epoch) "
        "VALUES ('global', '*', 'disable', 'x', 'me', 'human', 1)",
        "INSERT INTO control_switch_event (epoch, scope, target, action, mode, reason, actor_kind, actor_id) "
        "VALUES (1, 'global', '*', 'engaged', 'disable', 'x', 'human', 'me')",
    ],
)
def test_the_tables_cannot_be_written_around_the_function(db, sql):
    """Negative: a direct INSERT would skip the audit row, so it is refused."""
    as_actor(db, "human", "admin-7")
    assert_rejects(db, sql, sqlstate="RE009", match="control_switch_set")


def test_an_engaged_switch_cannot_be_deleted_directly(db):
    as_actor(db, "human", "admin-7")
    _set(db, "global", "*", "read_only")
    db.commit()
    assert_rejects(db, "DELETE FROM control_switch", sqlstate="RE009")
    assert_rejects(db, "UPDATE control_switch SET mode = 'disable'", sqlstate="RE009")


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE control_switch_event SET reason = 'nothing happened'",
        "DELETE FROM control_switch_event",
    ],
)
def test_control_switch_event_cannot_be_rewritten(db, sql):
    as_actor(db, "human", "admin-7")
    _set(db, "global", "*", "disable")
    db.commit()
    assert_rejects(db, sql, sqlstate="RE008", match="append-only")


def test_release_removes_the_row_and_records_who_released_it(db):
    as_actor(db, "human", "admin-7")
    engaged = _set(db, "agent_role", "scheduling", "human_only")
    as_actor(db, "human", "admin-9")
    with db.cursor() as cur:
        cur.execute("SELECT control_switch_release('agent_role', 'scheduling', 'fixed') AS epoch")
        released_epoch = cur.fetchone()["epoch"]
        cur.execute("SELECT count(*) AS n FROM control_switch")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT action, actor_id, mode FROM control_switch_event ORDER BY epoch")
        events = cur.fetchall()

    assert released_epoch > engaged["epoch"]
    assert events == [
        {"action": "engaged", "actor_id": "admin-7", "mode": "human_only"},
        {"action": "released", "actor_id": "admin-9", "mode": None},
    ]
    assert [e["actor_id"] for e in _audit(db)] == ["admin-7", "admin-9"]
    assert _chain_problems(db) == []


def test_releasing_a_switch_that_is_not_engaged_writes_nothing(db):
    as_actor(db, "human", "admin-7")
    with db.cursor() as cur:
        cur.execute("SELECT control_switch_release('global', '*', 'noop') AS epoch")
        assert cur.fetchone()["epoch"] is None
    assert _audit(db) == []


def test_changing_the_mode_keeps_one_current_row_and_advances_the_epoch(db):
    as_actor(db, "human", "admin-7")
    first = _set(db, "tool", "create_booking", "read_only")
    second = _set(db, "tool", "create_booking", "disable", reason="escalated")
    with db.cursor() as cur:
        cur.execute("SELECT mode, reason, epoch FROM control_switch")
        rows = cur.fetchall()
        cur.execute("SELECT control_switch_epoch() AS epoch")
        epoch = cur.fetchone()["epoch"]
    assert rows == [{"mode": "disable", "reason": "escalated", "epoch": second["epoch"]}]
    assert second["epoch"] > first["epoch"]
    assert epoch == second["epoch"]


def test_an_expired_switch_is_no_longer_in_force(db):
    as_actor(db, "human", "admin-7")
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM control_switch_set('global', '*', 'read_only', 'maintenance', "
            "clock_timestamp() + interval '200 milliseconds')"
        )
        cur.execute("SELECT count(*) AS n FROM control_switch_active")
        assert cur.fetchone()["n"] == 1
    db.commit()

    with db.cursor() as cur:
        cur.execute("SELECT pg_sleep(0.3)")
    db.commit()
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM control_switch_active")
        assert cur.fetchone()["n"] == 0
        # The row stays as the record of what was in force; it is simply no longer in force.
        cur.execute("SELECT count(*) AS n FROM control_switch")
        assert cur.fetchone()["n"] == 1


@pytest.mark.parametrize(
    ("scope", "target", "mode"),
    [
        ("global", "create_booking", "disable"),
        ("tool", "*", "disable"),
        ("tool", "Create Booking", "disable"),
        ("tenant", "t1", "disable"),
        ("tool", "create_booking", "off"),
    ],
)
def test_a_malformed_switch_is_refused(db, scope, target, mode):
    as_actor(db, "human", "admin-7")
    assert_rejects(
        db,
        "SELECT control_switch_set(%s, %s, %s, 'r')",
        (scope, target, mode),
        sqlstate="23514",
    )


def test_a_change_notifies_listeners_with_the_epoch(db, connect_factory):
    listener = connect_factory(autocommit=True)
    listener.execute("LISTEN control_switch")

    as_actor(db, "human", "admin-7")
    row = _set(db, "global", "*", "disable")
    db.commit()

    notices = list(listener.notifies(timeout=5, stop_after=1))
    assert [n.payload for n in notices] == [str(row["epoch"])]
