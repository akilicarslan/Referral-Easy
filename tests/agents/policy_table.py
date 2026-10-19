"""The policy table, read from the migration that owns it — and a backend that obeys it.

The refusal tests need a `PolicyBackend`. There are three ways to get one:

1. a hand-written dict in the test, which would be a *second copy* of the table and
   would drift from the first;
2. PostgreSQL, which the tests may not require (and which would make a refusal test
   fail when a container is down rather than when the policy is wrong);
3. read `migrations/014_align_domain_vocabulary.sql` — the table's real source —
   and answer from that.

This module does the third. The rows are parsed out of the migration's own
`INSERT INTO policy_rule ... VALUES` block, including the `allowed = false` rows
that encode §3's forbidden authority; the backend is **default-deny**, so a
`(role, tool, state)` triple the migration does not mention is a refusal, exactly as
the database's `policy_assert` behaves. A test that passes here passes against the
real table, and a test that would pass only against a stale copy cannot be written.

`PolicyDeniedError` is raised rather than the runtime's `PolicyRefused`, on purpose:
the database raises the domain error, and the runtime's `ToolGate` translates it. A
test that raised `PolicyRefused` directly would skip that translation and stop
covering the path a deployment actually takes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from referraleasy_agent_runtime import AgentRole
from referraleasy_contracts.generated.common import WorkflowState
from referraleasy_domain import PolicyDeniedError

__all__ = [
    "MIGRATION_PATH",
    "POLICY_ROWS",
    "POLICY_ROW_RE",
    "HardDenial",
    "InMemoryPolicy",
    "hard_denials",
    "load_policy_rows",
    "rows_for",
]

#: The migration whose `policy_rule` block is the authority on role x tool x state.
MIGRATION_PATH: Final = Path("migrations/014_align_domain_vocabulary.sql")

#: One `('role', 'tool', 'STATE', true|false, NULL|n, NULL|'note')` tuple. `\s` rather
#: than a space because two rows wrap their note onto the next line, and the note
#: pattern allows SQL's doubled quote (`''`) because one note contains an apostrophe —
#: a hole this pattern had until a test noticed that a row was missing from the parse.
POLICY_ROW_RE: Final = re.compile(
    r"\(\s*'(?P<role>[a-z_]+)'\s*,\s*'(?P<tool>[a-z_]+)'\s*,\s*'(?P<state>[A-Z_]+)'\s*,"
    r"\s*(?P<allowed>true|false)\s*,\s*(?P<max_calls>NULL|\d+)\s*,\s*(?P<note>NULL|'(?:[^']|'')*')\s*\)"
)

#: How a row starts, counted to prove the parse lost none of them.
ROW_START_RE: Final = re.compile(r"\(\s*'")


@dataclass(frozen=True, slots=True)
class HardDenial:
    """A row that says `allowed = false`: a capability §3 took away by name."""

    role: str
    tool: str
    state: str
    note: str | None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.role, self.tool, self.state)


@dataclass(frozen=True, slots=True)
class PolicyRow:
    role: str
    tool: str
    state: str
    allowed: bool
    max_calls: int | None
    note: str | None


def load_policy_rows(
    path: Path | str = MIGRATION_PATH, *, root: Path | None = None
) -> tuple[PolicyRow, ...]:
    """Parse the policy rows out of the migration. Raises when it finds none.

    An empty parse is a failure rather than an empty table: a migration that stopped
    containing rows would silently turn every refusal test into a vacuous pass.
    """
    resolved = Path(path)
    if not resolved.is_absolute():
        base = root or Path(__file__).resolve().parents[2]
        resolved = base / resolved
    text = resolved.read_text(encoding="utf-8")
    start = text.find("INSERT INTO policy_rule (agent_role, tool_name, workflow_state")
    if start == -1:
        raise AssertionError(f"{resolved} has no policy_rule INSERT: the row list moved.")
    end = text.find(";\n", start)
    block = text[start : end if end != -1 else len(text)]
    rows = tuple(
        PolicyRow(
            role=match.group("role"),
            tool=match.group("tool"),
            state=match.group("state"),
            allowed=match.group("allowed") == "true",
            max_calls=None if match.group("max_calls") == "NULL" else int(match.group("max_calls")),
            note=None
            if match.group("note") == "NULL"
            else match.group("note").strip("'").replace("''", "'"),
        )
        for match in POLICY_ROW_RE.finditer(block)
    )
    if not rows:
        raise AssertionError(f"no policy rows parsed from {resolved}; the tuple format changed.")
    # A row the pattern cannot match is a row the tests cannot see, and a silent hole in a
    # default-deny table is the one kind of hole this module must not have: one row's note
    # contained an escaped apostrophe and was dropped from every run until it was noticed.
    started = len(ROW_START_RE.findall(block))
    if started != len(rows):
        raise AssertionError(
            f"{resolved} has {started} policy rows and {len(rows)} parsed; {started - len(rows)} "
            "row(s) do not match POLICY_ROW_RE and would be invisible to every refusal test."
        )
    return rows


POLICY_ROWS: Final[tuple[PolicyRow, ...]] = load_policy_rows()

#: Every explicit `false` row, keyed by `(role, tool, state)`. §3's forbidden
#: capabilities, as the database states them.
_EXPLICIT: Final[Mapping[tuple[str, str, str], PolicyRow]] = MappingProxyType(
    {(row.role, row.tool, row.state): row for row in POLICY_ROWS}
)


def rows_for(*, role: str | None = None, tool: str | None = None) -> tuple[PolicyRow, ...]:
    return tuple(
        row
        for row in POLICY_ROWS
        if (role is None or row.role == role) and (tool is None or row.tool == tool)
    )


def hard_denials() -> tuple[HardDenial, ...]:
    """Every explicit denial, so a test can name one without a literal."""
    return tuple(
        HardDenial(role=row.role, tool=row.tool, state=row.state, note=row.note)
        for row in POLICY_ROWS
        if not row.allowed
    )


class InMemoryPolicy:
    """The migration's table, default-deny, raising the domain's own error."""

    def __init__(self, rows: Iterable[PolicyRow] = POLICY_ROWS) -> None:
        self._rows = {(row.role, row.tool, row.state): row for row in rows}
        self.asked: list[tuple[str, str, str]] = []

    def policy_assert(self, role: str, tool: str, workflow_state: str) -> None:
        """Return when permitted; raise :class:`PolicyDeniedError` when not.

        Absent row, `allowed = false`, unknown role, unknown tool, unknown state —
        every one of them is the same refusal, which is what "default-deny" means and
        what the database's function does.
        """
        self.asked.append((role, tool, workflow_state))
        row = self._rows.get((role, tool, workflow_state))
        if row is None:
            raise PolicyDeniedError(
                f"no policy row for {role}/{tool}/{workflow_state}: the table is default-deny"
            )
        if not row.allowed:
            raise PolicyDeniedError(row.note or f"{role} may not call {tool} in {workflow_state}")

    # ── what a test asks of the table ────────────────────────────────────────────────────────

    @staticmethod
    def permitted(role: str, tool: str, state: str) -> bool:
        """Whether the table permits the triple, without raising."""
        row = _EXPLICIT.get((role, tool, state))
        return bool(row is not None and row.allowed)

    @staticmethod
    def states_for(role: str, tool: str) -> list[str]:
        return sorted(row.state for row in rows_for(role=role, tool=tool) if row.allowed)


def all_roles() -> Sequence[AgentRole]:
    return tuple(AgentRole)


def all_states() -> Sequence[str]:
    return tuple(state.value for state in WorkflowState)
