"""The policy table bounds what an agent can do, and the bound is enforced.

A coordinator that "decides" to call the booking tool is not stopped by its
prompt; it is stopped here, by a trigger that consults the matrix on every task
insert and denies by default.
"""

from __future__ import annotations

import pytest
from migration_fixtures import assert_rejects, make_workflow, walk

pytestmark = pytest.mark.integration


def open_session(db, workflow_id: str, agent_role: str, *, actor_kind: str = "automation") -> str:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO session (workflow_id, agent_role, actor_kind) VALUES (%s, %s, %s)"
            " RETURNING id",
            (workflow_id, agent_role, actor_kind),
        )
        return str(cur.fetchone()["id"])


def open_task(
    db, session_id: str, workflow_id: str, kind: str, idempotency_key: str = "k-1"
) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO task (session_id, workflow_id, kind, idempotency_key)"
            " VALUES (%s, %s, %s, %s)",
            (session_id, workflow_id, kind, idempotency_key),
        )


# ---------------------------------------------------------------------------
# Default deny
# ---------------------------------------------------------------------------


def test_an_unknown_role_tool_or_state_is_denied(db):
    """Default deny: an absent rule is a refusal, not an unspecified value. The
    three lookups below cover a role nobody registered, a tool nobody
    registered, and a state where this role has no business acting."""
    with db.cursor() as cur:
        cur.execute("SELECT policy_is_allowed('nobody', 'validate_referral', 'INTAKE') AS allowed")
        assert cur.fetchone()["allowed"] is False

        cur.execute(
            "SELECT policy_is_allowed('referral_intake', 'no_such_tool', 'INTAKE') AS allowed"
        )
        assert cur.fetchone()["allowed"] is False

        cur.execute(
            "SELECT policy_is_allowed('referral_intake', 'validate_referral', 'BOOKED') AS allowed"
        )
        assert cur.fetchone()["allowed"] is False


# ---------------------------------------------------------------------------
# The gate on task inserts
# ---------------------------------------------------------------------------


def test_a_permitted_tool_is_accepted(db):
    workflow_id = make_workflow(db)
    session_id = open_session(db, workflow_id, "referral_intake")

    open_task(db, session_id, workflow_id, "validate_referral")

    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM task")
        assert cur.fetchone()["n"] == 1


def test_a_tool_the_role_may_not_use_is_refused(db):
    """The consent agent cannot book; that is the booking agent's job, in a
    different state."""
    workflow_id = make_workflow(db)
    session_id = open_session(db, workflow_id, "system")

    assert_rejects(
        db,
        "INSERT INTO task (session_id, workflow_id, kind, idempotency_key)"
        " VALUES (%s, %s, 'create_booking', 'k-1')",
        (session_id, workflow_id),
        sqlstate="RE006",
        match="policy denies",
    )


def test_a_tool_permitted_elsewhere_is_still_refused_here(db):
    """The coordinator may not book *because of the workflow's state*, even
    though a booking agent may in booking_pending. Policy is evaluated against
    the live state, not against the caller's last read of it."""
    workflow_id = make_workflow(db)
    session_id = open_session(db, workflow_id, "scheduling")

    assert_rejects(
        db,
        "INSERT INTO task (session_id, workflow_id, kind, idempotency_key)"
        " VALUES (%s, %s, 'create_booking', 'k-1')",
        (session_id, workflow_id),
        sqlstate="RE006",
        match="in workflow state INTAKE",
    )


def test_an_unregistered_tool_cannot_be_invoked(db):
    workflow_id = make_workflow(db)
    session_id = open_session(db, workflow_id, "referral_intake")

    assert_rejects(
        db,
        "INSERT INTO task (session_id, workflow_id, kind, idempotency_key)"
        " VALUES (%s, %s, 'delete_everything', 'k-1')",
        (session_id, workflow_id),
        sqlstate="RE006",
    )


def test_the_gate_follows_the_workflow_state(db):
    """The same task, by the same role, becomes permitted once the workflow has
    moved -- because the gate reads the current state at insert time."""
    workflow_id = make_workflow(db)
    session_id = open_session(db, workflow_id, "scheduling")

    # The refusal aborts the transaction, so the setup above is committed first
    # and the workflow still exists afterwards.
    db.commit()
    assert_rejects(
        db,
        "INSERT INTO task (session_id, workflow_id, kind, idempotency_key)"
        " VALUES (%s, %s, 'list_slots', 'k-1')",
        (session_id, workflow_id),
        sqlstate="RE006",
    )

    # Move the workflow to awaiting_approval, where the rule exists.
    walk(
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

    open_task(db, session_id, workflow_id, "list_slots", idempotency_key="k-2")
    with db.cursor() as cur:
        cur.execute("SELECT kind FROM task")
        assert cur.fetchone()["kind"] == "list_slots"


# ---------------------------------------------------------------------------
# The matrix itself
# ---------------------------------------------------------------------------


def test_an_explicit_prohibition_is_a_recorded_decision(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT allowed FROM policy_rule"
            " WHERE agent_role = 'operator' AND tool_name = 'create_booking'"
            "   AND workflow_state = 'HUMAN_HANDOFF'"
        )
        assert cur.fetchone()["allowed"] is False


def test_an_unknown_role_cannot_be_written_into_a_rule(db):
    assert_rejects(
        db,
        "INSERT INTO policy_rule (agent_role, tool_name, workflow_state, allowed)"
        " VALUES ('ghost_agent', 'validate_referral', 'INTAKE', true)",
        (),
        sqlstate="23503",
    )


def test_a_max_calls_ceiling_must_be_positive(db):
    assert_rejects(
        db,
        "INSERT INTO policy_rule (agent_role, tool_name, workflow_state, allowed, max_calls)"
        " VALUES ('scheduling', 'list_slots', 'PROVIDER_MATCHING', true, 0)",
        (),
        sqlstate="23514",
        match="policy_rule_max_calls_positive",
    )
