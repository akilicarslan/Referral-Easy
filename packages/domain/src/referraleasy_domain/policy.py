"""The policy table (§4) as the application sees it: default deny, answered by the database.

`policy_rule` is the only source of truth for who may do what, in which state. This module does not
cache it, mirror it, or second-guess it — a mirrored matrix is exactly the artefact that goes stale
the moment a row is added, and the stale copy would be the one the coordinator is allowed to exceed.

Two calls, one meaning: `is_allowed` asks (for planning, for explaining a refusal, for a dry run),
and `assert_allowed` **commits** to the answer by asking the database to refuse. Only the second is
a gate, and it is a gate precisely because it cannot be true while the table says otherwise.

An action is written `owner:action` — `scheduling:create_booking` — and that single string carries
both arguments `policy_assert` wants. Splitting it here rather than at each call site keeps the one
place that knows the convention next to the one place that knows the pattern in the contract.
"""

from __future__ import annotations

import re
from typing import Final, Protocol

from referraleasy_domain.workflow import ConnectionLike, execute_function, fetch_scalar

__all__ = [
    "ACTION_REF_PATTERN",
    "MalformedActionRefError",
    "PolicyTable",
    "RootModelLike",
    "parse_action_ref",
]


class RootModelLike(Protocol):
    """Anything shaped like a pydantic `RootModel[str]` — in practice a contract `ActionRef`.

    Structural rather than an import: the contracts package ships no `py.typed` marker, so importing
    its types would make `mypy --strict` refuse to analyse this module. A `root: str` attribute is
    also the whole of what `parse_action_ref` actually uses, so the protocol documents the real
    requirement instead of a nominal one.
    """

    root: str


#: The same shape `ActionRef` in the contracts package constrains: `owner:action`, lower-case with
#: underscores. Kept as a literal here so a malformed ref is refused locally, with the offending
#: text in the message, instead of travelling to PostgreSQL and coming back as an opaque type error.
ACTION_REF_PATTERN: Final[re.Pattern[str]] = re.compile(r"([a-z][a-z0-9_]*):([a-z][a-z0-9_]*)")


class MalformedActionRefError(ValueError):
    """An action reference is not of the form `owner:action`.

    A `ValueError`, not a :class:`~referraleasy_domain.errors.DomainError`: nothing was refused by
    the database here. The ref is malformed before any statement is sent, which usually means a
    caller — often a model — invented an action name, so it is worth catching and handling
    separately from a policy denial (a real action that this role may not take in this state).
    """


def parse_action_ref(action: str | RootModelLike) -> tuple[str, str]:
    """Split an `owner:action` reference into the `(agent_role, tool_name)` pair the SQL takes.

    Accepts a plain string or a contract `ActionRef` (a `RootModel[str]`), so callers can pass
    whichever they hold.

    >>> parse_action_ref("scheduling:create_booking")
    ('scheduling', 'create_booking')

    Anything that is not exactly one owner, one colon and one action raises
    :class:`MalformedActionRefError` — including the near misses that would otherwise reach the
    database as a silently-denied policy lookup, such as `"create_booking"` (no owner) or
    `"scheduling:create_booking:extra"`.
    """
    text = getattr(action, "root", action)
    if not isinstance(text, str):
        raise MalformedActionRefError(
            f"action ref must be a string or ActionRef, got {type(action).__name__}"
        )

    match = ACTION_REF_PATTERN.fullmatch(text)
    if match is None:
        raise MalformedActionRefError(
            f"{text!r} is not an 'owner:action' reference "
            f"(expected to match {ACTION_REF_PATTERN.pattern})"
        )
    return match.group(1), match.group(2)


class PolicyTable:
    """Typed access to `policy_is_allowed` / `policy_assert` over one connection."""

    def __init__(self, conn: ConnectionLike) -> None:
        self._conn = conn

    def is_allowed(self, agent_role: str, tool_name: str, workflow_state: str) -> bool:
        """Whether the matrix explicitly permits this triple.

        Default deny: a missing row, an unknown role, an unknown tool and an unknown state all
        evaluate to False, so only an explicit decision can return True.
        """
        return bool(
            fetch_scalar(
                self._conn,
                "SELECT policy_is_allowed(%s, %s, %s)",
                (agent_role, tool_name, workflow_state),
            )
        )

    def assert_allowed(self, agent_role: str, tool_name: str, workflow_state: str) -> None:
        """Ask the database to refuse if the triple is not permitted.

        Raises :class:`~referraleasy_domain.errors.PolicyDeniedError` (RE006) when it is not. The
        refusal is raised by `policy_assert` itself rather than by a comparison here, so a denial
        that happens is a denial the table produced — and a caller that never calls this is still
        bound by the `task_policy_gate` trigger on every task insert.
        """
        execute_function(
            self._conn,
            "SELECT policy_assert(%s, %s, %s)",
            (agent_role, tool_name, workflow_state),
        )

    def is_action_allowed(self, action: str | RootModelLike, workflow_state: str) -> bool:
        """`is_allowed` for an `owner:action` reference."""
        role, tool = parse_action_ref(action)
        return self.is_allowed(role, tool, workflow_state)

    def assert_action_allowed(self, action: str | RootModelLike, workflow_state: str) -> None:
        """`assert_allowed` for an `owner:action` reference."""
        role, tool = parse_action_ref(action)
        self.assert_allowed(role, tool, workflow_state)
