"""Integration tests for the policy matrix (§10, §13).

The rule under test: **policy is default-deny, and the matrix is the only source of truth.** No
Python in this package knows which role may call which tool in which state -- a list here would be a
second copy of the policy, and the copy that disagreed with the database would be the one that let
something through. What the client does is ask, and translate the refusal.

The triples used below are real seeded rows, quoted from the migration rather than invented, so a
test that passes proves something about the shipped policy and not about a fixture.
"""

from __future__ import annotations

import pytest

from referraleasy_domain import MalformedActionRefError, PolicyDeniedError, PolicyTable

pytestmark = pytest.mark.integration


@pytest.fixture
def policy(db) -> PolicyTable:
    return PolicyTable(db)


# ---------------------------------------------------------------------------
# Explicit allow, explicit deny, and absence
# ---------------------------------------------------------------------------

#: `('scheduling', 'list_slots', 'AWAITING_SLOT_CHOICE', true)` -- seeded.
ALLOWED = ("scheduling", "list_slots", "AWAITING_SLOT_CHOICE")

#: `('scheduling', 'list_slots', 'BOOKING_IN_FLIGHT', false)` -- an explicit prohibition. The row
#: exists to *document* a decision, which is why absence and explicit denial must both be denied but
#: are not the same thing.
EXPLICITLY_DENIED = ("scheduling", "list_slots", "BOOKING_IN_FLIGHT")

#: No row at all.
UNKNOWN = ("scheduling", "list_slots", "INTAKE")


def test_an_allowed_row_is_allowed(policy) -> None:
    assert policy.is_allowed(*ALLOWED)


def test_an_explicitly_denied_row_is_denied(policy) -> None:
    assert not policy.is_allowed(*EXPLICITLY_DENIED)


def test_a_triple_with_no_row_is_denied(policy) -> None:
    """Absence is denial, which is what makes adding a tool a deliberate act."""
    assert not policy.is_allowed(*UNKNOWN)


def test_an_unknown_role_is_denied(policy) -> None:
    # Not "unknown role -> error": the matrix answers false for anything it does not know, so a
    # typo in a role name fails closed rather than open.
    assert not policy.is_allowed("nobody_at_all", "list_slots", "AWAITING_SLOT_CHOICE")


def test_the_role_and_the_tool_are_not_interchangeable(policy) -> None:
    """Swapping the two arguments must not accidentally find the allow row.

    They are both `text` and both usually lowercase identifiers, so nothing but a test stops a
    caller from passing them the wrong way round.
    """
    assert policy.is_allowed(*ALLOWED)
    assert not policy.is_allowed("list_slots", "scheduling", "AWAITING_SLOT_CHOICE")


# ---------------------------------------------------------------------------
# The refusal the caller acts on
# ---------------------------------------------------------------------------


def test_assert_allowed_raises_the_typed_refusal(policy) -> None:
    with pytest.raises(PolicyDeniedError) as excinfo:
        policy.assert_allowed(*EXPLICITLY_DENIED)

    assert excinfo.value.sqlstate == "RE006"
    # The message names the triple, so an operator reading a log knows which rule is missing.
    assert "scheduling" in str(excinfo.value)
    assert "BOOKING_IN_FLIGHT" in str(excinfo.value)


def test_assert_allowed_passes_silently_when_it_is_allowed(policy) -> None:
    # A void function returning NULL is the success case; reading its result must not raise.
    assert policy.assert_allowed(*ALLOWED) is None


# ---------------------------------------------------------------------------
# The state is what makes the same action legal or not
# ---------------------------------------------------------------------------

#: `('scheduling', 'create_booking', 'BOOKING_IN_FLIGHT', true, max_calls 1)` -- the only place a
#: booking may be created.
BOOKING_MAY_BE_CREATED = "BOOKING_IN_FLIGHT"

#: `('scheduling', 'create_booking', 'AWAITING_FINAL_APPROVAL', false)` -- §9: consent is being
#: collected, so booking is explicitly forbidden. This pair is the whole point of the matrix.
CONSENT_IS_BEING_COLLECTED = "AWAITING_FINAL_APPROVAL"


@pytest.mark.parametrize(
    ("workflow_state", "expected"),
    [(BOOKING_MAY_BE_CREATED, True), (CONSENT_IS_BEING_COLLECTED, False), ("INTAKE", False)],
)
def test_the_same_action_depends_on_the_state(
    policy, workflow_state: str, *, expected: bool
) -> None:
    assert policy.is_allowed("scheduling", "create_booking", workflow_state) is expected


def test_the_action_ref_form_agrees_with_the_three_argument_form(policy) -> None:
    """`owner:action` is the shape the contract carries; it must resolve to the same rule."""
    assert policy.is_action_allowed("scheduling:create_booking", BOOKING_MAY_BE_CREATED)
    assert not policy.is_action_allowed("scheduling:create_booking", CONSENT_IS_BEING_COLLECTED)
    assert policy.is_allowed("scheduling", "create_booking", BOOKING_MAY_BE_CREATED)


def test_assert_action_allowed_raises_the_same_refusal(policy) -> None:
    with pytest.raises(PolicyDeniedError):
        policy.assert_action_allowed("scheduling:create_booking", CONSENT_IS_BEING_COLLECTED)


def test_a_malformed_action_ref_never_reaches_the_database(policy) -> None:
    """A ref the contract cannot have produced is a caller bug, not a policy decision.

    It is a `ValueError` rather than a `PolicyDeniedError` on purpose: reporting it as a denial
    would send the caller looking for a policy rule that was never the problem.
    """
    with pytest.raises(MalformedActionRefError):
        policy.assert_action_allowed("scheduling", BOOKING_MAY_BE_CREATED)

    with pytest.raises(MalformedActionRefError):
        policy.assert_action_allowed("scheduling:create:booking", BOOKING_MAY_BE_CREATED)


# ---------------------------------------------------------------------------
# The matrix is the policy
# ---------------------------------------------------------------------------


def test_adding_a_rule_permits_the_call_with_no_code_change(policy, db) -> None:
    """The claim this package rests on: policy lives in a table, not in Python.

    Nothing about "scheduling may create a booking from INTAKE" is expressible here without a row,
    and once the row exists the client honours it -- the same binary, no redeploy. If this test ever
    needed a code change to pass, the policy would have leaked out of the database.
    """
    assert not policy.is_allowed(*UNKNOWN)

    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO policy_rule (agent_role, tool_name, workflow_state, allowed, note)
            VALUES (%s, %s, %s, true, 'granted by a test, deliberately')
            """,
            UNKNOWN,
        )

    assert policy.is_allowed(*UNKNOWN)
    assert policy.assert_allowed(*UNKNOWN) is None

    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM policy_rule WHERE agent_role = %s AND tool_name = %s"
            " AND workflow_state = %s",
            UNKNOWN,
        )

    # Removed, and the default answers again: a permission that cannot be withdrawn is not a policy.
    assert not policy.is_allowed(*UNKNOWN)


def test_a_denied_rule_cannot_be_reached_by_an_undeclared_tool(db) -> None:
    """The matrix cannot be extended by inventing a tool: the FK to `tool_registry` refuses it.

    A policy row is only meaningful for a tool the system actually knows about, so "allow
    everything" has to be written as the explicit, reviewable list of tools that exist.
    """
    import psycopg

    with pytest.raises(psycopg.errors.ForeignKeyViolation), db.transaction(), db.cursor() as cur:
        cur.execute(
            "INSERT INTO policy_rule (agent_role, tool_name, workflow_state, allowed)"
            " VALUES ('scheduling', 'invent_a_tool', 'BOOKED', true)"
        )
