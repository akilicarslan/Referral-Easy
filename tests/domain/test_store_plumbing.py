"""Unit tests for the shared plumbing: refusal translation and row decoding.

No database, and no driver either. The translation contract is duck-typed — an exception carrying a
`sqlstate` (psycopg) or a `pgcode` (DBAPI 2.0) *is* a database refusal — so a fake exception with
that attribute exercises the real path. That is the same duck-typing `errors.translate` documents,
and testing it this way keeps the claim honest: importing this package must not require a driver.

The other half matters just as much: a refusal is the only thing translated. A Python-level bug
carries no SQLSTATE, and it must stay a bug rather than being laundered into a domain error that a
caller would handle as if the database had refused something.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple
from uuid import UUID, uuid4

import pytest

from referraleasy_domain import (
    CommandLedger,
    HandoffStore,
    IllegalTransitionError,
    OutboxStore,
    PolicyDeniedError,
    PolicyTable,
    StaleOwnershipError,
    WorkflowStore,
)
from referraleasy_domain.errors import (
    SQLSTATE_MAP,
    AppendOnlyViolationError,
    ApprovalRefusedError,
    CommandConflictError,
    DomainError,
    HumanOwnedError,
    OutboxClaimError,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class Column(NamedTuple):
    """The one attribute `_column_names` reads off `cursor.description`."""

    name: str


class DriverRefusal(Exception):
    """A psycopg-shaped error: it carries the SQLSTATE as `sqlstate`."""

    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class DBAPI2Refusal(Exception):
    """A DBAPI 2.0-shaped error: same information, carried as `pgcode`."""

    def __init__(self, message: str, pgcode: str) -> None:
        super().__init__(message)
        self.pgcode = pgcode


class FakeCursor:
    def __init__(
        self,
        *,
        description: list[Column] | None = None,
        rows: list[Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.description = description
        self._rows = rows or []
        self._error = error

    def execute(self, query: str, params: Any = ()) -> None:
        if self._error is not None:
            raise self._error

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> FakeCursor:
        return self._cursor


class ExplodingConnection:
    """A connection whose `cursor()` raises a plain Python bug."""

    def cursor(self) -> Any:
        raise TypeError("a bug, not a refusal")


def failing(sqlstate: str) -> FakeConnection:
    return FakeConnection(FakeCursor(error=DriverRefusal("the database refused", sqlstate)))


# ---------------------------------------------------------------------------
# Every refusal code reaches the caller as its typed exception
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sqlstate", "expected"),
    sorted((code, cls) for code, cls in SQLSTATE_MAP.items()),
)
def test_every_refusal_code_arrives_as_its_typed_exception(
    sqlstate: str, expected: type[DomainError]
) -> None:
    # Driven through a real store call so the assertion covers the plumbing every store shares.
    with pytest.raises(expected) as excinfo:
        WorkflowStore(failing(sqlstate)).load(uuid4())

    assert excinfo.value.sqlstate == sqlstate
    # The original is preserved, so the driver's message is not lost in the translation.
    assert isinstance(excinfo.value.original, DriverRefusal)


def test_the_codes_the_migrations_actually_raise_are_all_covered() -> None:
    # A guard raising a SQLSTATE nothing maps would arrive as a bare DomainError: still an error,
    # but one a caller cannot act on by type.
    assert set(SQLSTATE_MAP) == {
        "RE001",
        "RE002",
        "RE003",
        "RE004",
        "RE005",
        "RE006",
        "RE007",
        "RE008",
    }


def test_a_stale_advance_surfaces_as_stale_ownership() -> None:
    conn = failing("RE001")
    with pytest.raises(StaleOwnershipError):
        WorkflowStore(conn).advance(
            uuid4(), "w-1", expected_generation=1, expected_state_version=1, to_state="BOOKED"
        )


def test_a_human_owned_workflow_surfaces_as_human_owned() -> None:
    conn = failing("RE003")
    with pytest.raises(HumanOwnedError):
        WorkflowStore(conn).advance(
            uuid4(), "w-1", expected_generation=1, expected_state_version=1, to_state="BOOKED"
        )


def test_an_illegal_edge_surfaces_as_an_illegal_transition() -> None:
    conn = failing("RE002")
    with pytest.raises(IllegalTransitionError):
        WorkflowStore(conn).advance(
            uuid4(),
            "w-1",
            expected_generation=1,
            expected_state_version=1,
            to_state="READINESS_CHECK",
        )


def test_a_policy_denial_surfaces_as_policy_denied() -> None:
    conn = failing("RE006")
    with pytest.raises(PolicyDeniedError):
        PolicyTable(conn).assert_allowed("scheduling", "create_booking", "AWAITING_SLOT_CHOICE")


def test_a_command_hash_mismatch_surfaces_as_a_command_conflict() -> None:
    conn = failing("RE005")
    with pytest.raises(CommandConflictError):
        CommandLedger(conn).begin("cmd-1", "a" * 64)


def test_a_lost_outbox_lease_surfaces_as_an_outbox_claim_error() -> None:
    conn = failing("RE007")
    with pytest.raises(OutboxClaimError):
        OutboxStore(conn).settle(uuid4(), "w-1", ok=True)


def test_an_operator_returning_a_workflow_to_a_forbidden_state_surfaces_as_approval_refused() -> (
    None
):
    # The allow-list refusal is RE004 in the database, which is why resolving a ticket reads as a
    # consent-boundary refusal: the state it would have reached is a consent state.
    conn = failing("RE004")
    with pytest.raises(ApprovalRefusedError):
        HandoffStore(conn).resolve(uuid4(), "op-1", expected_generation=1, resolution="done")


def test_an_append_only_violation_still_maps_even_though_no_store_writes_those_tables() -> None:
    # Audit and evidence are append-only, and nothing in this package writes them -- their triggers
    # do. The code is mapped anyway so that an attempt through any future client is actionable.
    with pytest.raises(AppendOnlyViolationError):
        WorkflowStore(failing("RE008")).load(uuid4())


def test_an_unknown_sqlstate_is_still_surfaced_with_its_identity() -> None:
    # A CHECK or a unique index is a real refusal with a real code. It is not one of RE00x, so it
    # arrives as a plain DomainError carrying the code rather than being swallowed.
    with pytest.raises(DomainError) as excinfo:
        WorkflowStore(failing("23505")).load(uuid4())

    assert excinfo.value.sqlstate == "23505"
    assert type(excinfo.value) is DomainError


def test_a_dbapi2_pgcode_is_understood_too() -> None:
    conn = FakeConnection(FakeCursor(error=DBAPI2Refusal("refused", "RE001")))
    with pytest.raises(StaleOwnershipError):
        WorkflowStore(conn).load(uuid4())


# ---------------------------------------------------------------------------
# And nothing else is translated
# ---------------------------------------------------------------------------


def test_a_python_bug_is_not_laundered_into_a_domain_error() -> None:
    """A `TypeError` has no SQLSTATE, so it stays a `TypeError`.

    If this were swallowed, a typo in the client would present to callers as "the database refused
    your transition" — the most expensive possible way to learn about a bug.
    """
    with pytest.raises(TypeError):
        WorkflowStore(ExplodingConnection()).load(uuid4())


# ---------------------------------------------------------------------------
# Row decoding
# ---------------------------------------------------------------------------

WORKFLOW_ROW: dict[str, Any] = {
    "id": uuid4(),
    "ticket_ref": "T-1001",
    "patient_ref": "P-77",
    "specialty": None,
    "state": "PROVIDER_MATCHING",
    "state_version": 4,
    "generation": 7,
    "owner_worker_id": "w-1",
    "lease_expires_at": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    "human_owner_id": None,
    "human_claimed_at": None,
    "payload": {"booking_payload_hash": "a" * 64},
    "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    "updated_at": datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
}


def test_a_tuple_row_is_decoded_through_the_cursor_description() -> None:
    """The default row factory is tuples, so every store must cope with them.

    Reading `description` is what allows the store to accept whatever factory the connection happens
    to have configured instead of requiring `dict_row` from a caller who was never told.
    """
    cursor = FakeCursor(
        description=[Column(name) for name in WORKFLOW_ROW],
        rows=[tuple(WORKFLOW_ROW.values())],
    )
    workflow = WorkflowStore(FakeConnection(cursor)).load(WORKFLOW_ROW["id"])

    assert workflow is not None
    assert workflow.id == WORKFLOW_ROW["id"]
    assert isinstance(workflow.id, UUID)
    assert workflow.state == "PROVIDER_MATCHING"
    assert workflow.generation == 7
    assert workflow.state_version == 4
    assert workflow.owner_worker_id == "w-1"
    assert workflow.payload == {"booking_payload_hash": "a" * 64}
    assert workflow.is_human_owned is False


def test_a_dict_row_is_used_as_it_is() -> None:
    cursor = FakeCursor(
        description=[Column(name) for name in WORKFLOW_ROW],
        rows=[dict(WORKFLOW_ROW)],
    )
    workflow = WorkflowStore(FakeConnection(cursor)).load(WORKFLOW_ROW["id"])

    assert workflow is not None
    assert workflow.id == WORKFLOW_ROW["id"]


def test_no_row_is_none_rather_than_an_error() -> None:
    cursor = FakeCursor(description=[Column("id")], rows=[])
    assert WorkflowStore(FakeConnection(cursor)).load(uuid4()) is None


def test_a_null_scalar_is_read_as_none() -> None:
    # `policy_assert` and other void functions return a single NULL column; reading it must not
    # raise, because a void function that did *not* raise is the success case.
    cursor = FakeCursor(description=[Column("policy_assert")], rows=[(None,)])
    PolicyTable(FakeConnection(cursor)).assert_allowed(
        "system", "capture_consent", "BOOKING_IN_FLIGHT"
    )


def test_a_boolean_scalar_is_read_from_a_dict_row_factory_too() -> None:
    cursor = FakeCursor(
        description=[Column("policy_is_allowed")],
        rows=[{"policy_is_allowed": True}],
    )
    assert PolicyTable(FakeConnection(cursor)).is_allowed("scheduling", "list_slots", "BOOKED")


def test_is_human_owned_reads_the_column() -> None:
    row = {**WORKFLOW_ROW, "human_owner_id": "op-7"}
    cursor = FakeCursor(description=[Column(name) for name in row], rows=[tuple(row.values())])
    workflow = WorkflowStore(FakeConnection(cursor)).load(row["id"])

    assert workflow is not None
    assert workflow.is_human_owned is True


# ---------------------------------------------------------------------------
# The package stays importable without a driver
# ---------------------------------------------------------------------------


def test_importing_the_package_does_not_load_a_driver() -> None:
    """The stated design goal, verified in a fresh interpreter.

    It has to be a subprocess: another test in the session may already have imported psycopg, and
    the claim is about what `import referraleasy_domain` does on its own.
    """
    src = Path(__file__).resolve().parents[2] / "packages" / "domain" / "src"
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(src)!r});"
        "import referraleasy_domain;"
        "raise SystemExit(1 if 'psycopg' in sys.modules else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
