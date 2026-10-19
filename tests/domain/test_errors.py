"""SQLSTATE translation.

Each refusal the migrations raise carries a custom code, so the gateway can map a refusal to a
status and a patient-facing reason without parsing a message string. These tests pin the mapping and
the fallback, because a mis-translated refusal would be reported as something it is not.
"""

from __future__ import annotations

import pytest

from referraleasy_domain import (
    SQLSTATE_MAP,
    AppendOnlyViolationError,
    ApprovalRefusedError,
    CommandConflictError,
    DomainError,
    HumanOwnedError,
    IllegalTransitionError,
    OutboxClaimError,
    PolicyDeniedError,
    StaleOwnershipError,
    translate,
)

pytestmark = pytest.mark.contract


class FakeDatabaseError(Exception):
    """Stands in for a driver exception: the translation is duck-typed on `sqlstate`."""

    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    ("sqlstate", "expected"),
    [
        ("RE001", StaleOwnershipError),
        ("RE002", IllegalTransitionError),
        ("RE003", HumanOwnedError),
        ("RE004", ApprovalRefusedError),
        ("RE005", CommandConflictError),
        ("RE006", PolicyDeniedError),
        ("RE007", OutboxClaimError),
        ("RE008", AppendOnlyViolationError),
    ],
)
def test_each_code_maps_to_its_typed_error(sqlstate: str, expected: type[DomainError]) -> None:
    translated = translate(FakeDatabaseError("refused", sqlstate))
    assert type(translated) is expected
    assert translated.sqlstate == sqlstate


def test_the_map_covers_all_eight_declared_codes() -> None:
    """A guard raising a code with no mapping would surface as an untyped error."""
    assert set(SQLSTATE_MAP) == {f"RE00{n}" for n in range(1, 9)}


def test_message_and_cause_are_preserved() -> None:
    original = FakeDatabaseError(
        "stale generation for workflow abc: row is at 4, writer supplied 2", "RE001"
    )
    translated = translate(original)
    assert "row is at 4" in str(translated)
    assert translated.original is original


def test_unknown_codes_keep_their_identity() -> None:
    """An unrecognised refusal must still be surfaced with its code, not swallowed."""
    translated = translate(FakeDatabaseError("something new", "RE099"))
    assert type(translated) is DomainError
    assert translated.sqlstate == "RE099"


def test_a_driver_error_without_a_code_is_still_translated() -> None:
    translated = translate(RuntimeError("connection reset"))
    assert isinstance(translated, DomainError)
    assert translated.sqlstate == ""


def test_errors_are_catchable_as_one_family() -> None:
    for cls in SQLSTATE_MAP.values():
        assert issubclass(cls, DomainError)
        assert issubclass(cls, RuntimeError)


def test_each_subclass_declares_a_distinct_summary() -> None:
    """Summaries reach the patient; two classes sharing one would be indistinguishable."""
    summaries = [cls.summary for cls in SQLSTATE_MAP.values()]
    assert len(summaries) == len(set(summaries))


def test_human_owned_is_not_confused_with_stale_ownership() -> None:
    """§11 suspends automation while a human owns the workflow — a distinct outcome from a stale
    lease, and it must not be retried as one."""
    assert not issubclass(HumanOwnedError, StaleOwnershipError)
    assert not issubclass(StaleOwnershipError, HumanOwnedError)
