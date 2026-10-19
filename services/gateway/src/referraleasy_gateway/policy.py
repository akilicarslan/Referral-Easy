"""The per-action policy check (§4, §9): a table is consulted, no role is branched on.

§9: *"Workflow ownership and scope are checked for every tool; for example, only Scheduling uses
booking:write."* Every tool call carries a workflow-ownership and scope check — only
Scheduling may use the booking-write action. That sentence describes two different things, and this
module keeps them apart:

* **which (role, tool, state) triple is permitted** is `policy_rule`'s business. Here it is an
  `ActionPolicy` over rows. Nothing in the gateway asks "is this an admin?" or "is this the
  scheduler?" and branches; it asks the table, and the table's answer is data. Adding a row permits a
  call with no code change, which is the property §4 needs and the property
  `tests/gateway/test_policy_table.py` checks by *editing the table* in the middle of a test.
* **what an action writes** is `tool_registry.writes_control_state`'s business. `WRITE_TOOLS` below
  is that column, because the guardrail gate (§9: booking cannot be written while the guardrail
  dependency is off) has to ask "is this a write?" before it asks "may it run?".

The action string is parsed with `referraleasy_domain.parse_action_ref` rather than by splitting on
`:` here. One convention, one parser: a second splitter is a second chance to disagree about
`"scheduling:create:booking"`.

Two implementations of `ActionPolicy` exist and they are the same rules read from different places:
`TablePolicy` over rows (the memory profile, carrying `policy_seed`'s transcription of migration 014)
and `DomainPolicy` over `policy_assert` (the PostgreSQL profile, where the database *refuses* the
call rather than answering a question about it).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final, NamedTuple, Protocol

from referraleasy_domain import PolicyDeniedError, PolicyTable, parse_action_ref
from referraleasy_gateway.policy_seed import SEED_ROWS

__all__ = [
    "BOOKING_WRITE_ACTION",
    "WRITE_TOOLS",
    "ActionPolicy",
    "DomainPolicy",
    "PolicyRow",
    "TablePolicy",
    "allowed_actions_for",
    "is_booking_write",
    "is_write_action",
]

#: The one action that creates a booking request, and therefore the one action §9 names.
BOOKING_WRITE_ACTION: Final = "scheduling:create_booking"

#: The role that holds it. Read as a pair with the line above: "only Scheduling may use the
#: booking-write action" is a statement about an action *and* its owner.
BOOKING_WRITE_ROLE: Final = "scheduling"

#: `tool_registry.writes_control_state = true`, transcribed from `migrations/014`. A write is what the
#: guardrail gate refuses when the guardrail dependency is unavailable.
WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "create_booking",
        "cancel_booking",
        "reschedule_booking",
        "create_handoff",
        "resolve_ticket",
        "capture_consent",
    }
)


class PolicyRow(NamedTuple):
    """One `policy_rule` row, in column order.

    `note` is carried because a denial is much easier to read when the table says why the row exists
    — `'§9: booking requires a live approval bound to the payload.'` explains a refusal better than
    the triple does.
    """

    agent_role: str
    tool_name: str
    workflow_state: str
    allowed: bool
    note: str | None = None


def allowed_actions_for(
    rows: Iterable[PolicyRow], *, role: str, workflow_state: str
) -> tuple[str, ...]:
    """Every action the §4 table permits ``role`` in ``workflow_state``, as `owner:action` refs.

    The caller builds a §5 `TaskEnvelope` needs exactly this list, and the contract's field is the
    *intersection of the caller's intent and the policy table* — for a root task the caller's intent
    is "this role may do what the table says", so the answer is the table's own set.

    Computed from rows rather than asked of a backend on purpose: `ActionPolicy` is a one-action
    question (`is_allowed`/`assert_allowed`), and the Postgres profile has no enumeration query. The
    rows here are the ones `policy_seed` transcribes from migration 014, which
    `tests/gateway/test_policy_table.py` keeps equal to the database's own table.

    An empty tuple is a real answer, not a failure: the coordinator has no permitted action in a
    state like `AWAITING_HOSPITAL_CHOICE`, and an envelope that says so is telling the truth.
    """
    return tuple(
        sorted(
            f"{row.agent_role}:{row.tool_name}"
            for row in rows
            if row.allowed and row.agent_role == role and row.workflow_state == workflow_state
        )
    )


class ActionPolicy(Protocol):
    """The question every command asks before it does anything."""

    def is_allowed(self, action: str, workflow_state: str) -> bool:
        """Whether the table explicitly permits `owner:action` in `workflow_state`."""
        ...

    def assert_allowed(self, action: str, workflow_state: str) -> None:
        """Refuse with :class:`~referraleasy_domain.errors.PolicyDeniedError` when it does not."""
        ...


class TablePolicy:
    """`ActionPolicy` over rows. Default deny, exactly like the database's `policy_is_allowed`.

    Default deny is the whole design: a tool that is not named in a row is not permitted, so making
    something possible is a deliberate act with an author. An unknown role, an unknown tool and an
    unknown state all evaluate to False rather than raising, which is what makes a typo fail closed.
    """

    def __init__(self, rows: Iterable[PolicyRow]) -> None:
        self._rows: Mapping[tuple[str, str, str], PolicyRow] = {
            (row.agent_role, row.tool_name, row.workflow_state): row for row in rows
        }

    @classmethod
    def from_seed(
        cls, rows: Sequence[tuple[str, str, str, bool, str | None]] = SEED_ROWS
    ) -> TablePolicy:
        """The offline profile's table: `policy_seed`'s transcription of the shipped seed rows."""
        return cls(PolicyRow(*row) for row in rows)

    @property
    def rows(self) -> tuple[PolicyRow, ...]:
        return tuple(self._rows.values())

    def row_for(self, action: str, workflow_state: str) -> PolicyRow | None:
        """The row that decides this action in this state, or None when the table is silent."""
        role, tool = parse_action_ref(action)
        return self._rows.get((role, tool, workflow_state))

    def is_allowed(self, action: str, workflow_state: str) -> bool:
        row = self.row_for(action, workflow_state)
        return bool(row and row.allowed)

    def assert_allowed(self, action: str, workflow_state: str) -> None:
        if self.is_allowed(action, workflow_state):
            return
        role, tool = parse_action_ref(action)
        row = self.row_for(action, workflow_state)
        if row is None:
            detail = "there is no policy_rule row for it, and policy is default deny"
        else:
            detail = f"policy_rule denies it{': ' + row.note if row.note else ''}"
        raise PolicyDeniedError(
            f"{role} may not call {tool} in state {workflow_state}: {detail} (RE006, §4)"
        )


class DomainPolicy:
    """`ActionPolicy` answered by the database itself (the PostgreSQL profile).

    `policy_assert` is a *gate* rather than an answer: it raises RE006 when the triple is not
    permitted. `is_allowed` is the question form, for planning and for explaining a refusal.
    """

    def __init__(self, table: PolicyTable) -> None:
        self._table = table

    def is_allowed(self, action: str, workflow_state: str) -> bool:
        return self._table.is_action_allowed(action, workflow_state)

    def assert_allowed(self, action: str, workflow_state: str) -> None:
        self._table.assert_action_allowed(action, workflow_state)


def is_booking_write(action: str) -> bool:
    """Whether this action is the booking write §9 reserves for Scheduling."""
    role, tool = parse_action_ref(action)
    return role == BOOKING_WRITE_ROLE and tool == "create_booking"


def is_write_action(action: str) -> bool:
    """Whether this action writes control state, per `tool_registry.writes_control_state`."""
    _, tool = parse_action_ref(action)
    return tool in WRITE_TOOLS
