"""Unit tests for `owner:action` parsing.

No database and no driver: splitting an action reference is pure string handling, and the point of
doing it locally is that a malformed reference is refused with the offending text in the message
rather than travelling to PostgreSQL to fail as a type error, or arriving as a policy lookup that
finds no row and looks like a denial.
"""

from __future__ import annotations

import pytest

from referraleasy_domain import MalformedActionRefError, parse_action_ref


def test_splits_into_the_pair_the_sql_takes() -> None:
    # The two halves are exactly the arguments policy_is_allowed/assert wants.
    assert parse_action_ref("scheduling:create_booking") == ("scheduling", "create_booking")
    assert parse_action_ref("system:await_patient_choice") == ("system", "await_patient_choice")
    assert parse_action_ref("operator:resolve_ticket") == ("operator", "resolve_ticket")


def test_parsing_is_syntactic_and_does_not_pass_judgement() -> None:
    """A well-formed but nonsensical pair parses; deciding it is the policy table's job.

    `create_booking:scheduling` is backwards — a tool in the role position — and this must still
    split cleanly. Refusing it here would mean this module held an opinion about which roles and
    tools exist, which is precisely the second source of truth it exists to avoid. The table denies
    it by having no such row, which is an integration test (`test_policy_table.py`).
    """
    assert parse_action_ref("create_booking:scheduling") == ("create_booking", "scheduling")


def test_accepts_a_root_model_shaped_object() -> None:
    """A contract `ActionRef` is a `RootModel[str]`, so the value lives on `.root`."""

    class StubActionRef:
        root = "provider_matching:search_directory"

    assert parse_action_ref(StubActionRef()) == ("provider_matching", "search_directory")


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("", id="empty"),
        pytest.param("create_booking", id="no-owner"),
        pytest.param(":create_booking", id="empty-owner"),
        pytest.param("scheduling:", id="empty-action"),
        pytest.param("scheduling:create_booking:extra", id="two-colons"),
        pytest.param("Scheduling:create_booking", id="capitalised-owner"),
        pytest.param("scheduling:Create_booking", id="capitalised-action"),
        pytest.param("scheduling-create_booking", id="wrong-separator"),
        pytest.param("scheduling:create-booking", id="hyphen-in-action"),
        pytest.param("1scheduling:create_booking", id="owner-starts-with-a-digit"),
        pytest.param(" scheduling:create_booking", id="leading-space"),
        pytest.param("scheduling:create_booking ", id="trailing-space"),
    ],
)
def test_malformed_refs_are_refused_locally(bad: str) -> None:
    with pytest.raises(MalformedActionRefError):
        parse_action_ref(bad)


def test_the_error_names_the_offending_text_and_the_expected_shape() -> None:
    with pytest.raises(MalformedActionRefError) as excinfo:
        parse_action_ref("create_booking")

    message = str(excinfo.value)
    assert "create_booking" in message
    assert "owner:action" in message


def test_it_is_a_value_error_so_ordinary_handling_still_works() -> None:
    assert issubclass(MalformedActionRefError, ValueError)


def test_a_non_string_is_refused_rather_than_stringified() -> None:
    # `str(42)` would produce "42", which is not a valid ref and would be far more confusing than
    # being told the argument itself was the wrong type.
    with pytest.raises(MalformedActionRefError):
        parse_action_ref(42)  # type: ignore[arg-type]
