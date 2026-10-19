"""Tool authority: two checks, in a fixed order, in one place.

Every tool call is asked twice:

1. **Is this tool on this agent's allowlist?** Local data, from
   :mod:`referraleasy_agent_runtime.roles`, and cheap. This is the check a skill
   cannot influence: adding a skill grants **no** tool permission.
2. **May this agent act on this workflow in its current state?** Answered by the
   database — ``policy_assert(role, tool, workflow_state)`` in
   `migrations/005_policy.sql`, re-derived per §3 in `014_align_domain_vocabulary.sql`.
   The table is default-deny: an absent row is a refusal, and the only way to
   widen it is an auditable INSERT. This module *calls* the authority and
   translates its refusal; it does not keep a second copy of the table, because a
   second copy is a second answer that can drift.

Order matters. The allowlist is checked first so that a tool a role may never call
is refused without a database round-trip, and so the refusal names the role rather
than leaking whether the tool exists in the matrix.

There is no unauthenticated path through the gate. If a state-dependent check is
needed and no policy backend was supplied, the call is refused
(:class:`PolicyUnavailable`) rather than allowed — the same default-deny rule the
table uses.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Final, Protocol, runtime_checkable

from referraleasy_agent_runtime.budget import CallLease, CapKind, TurnBudget
from referraleasy_agent_runtime.errors import PolicyRefused, PolicyUnavailable, ToolNotAllowed
from referraleasy_agent_runtime.roles import BOOKING_WRITE_TOOLS, AgentRole, allowed_tools
from referraleasy_contracts.generated.common import WorkflowState
from referraleasy_domain import PolicyDeniedError

__all__ = [
    "POLICY_ASSERT_SQL",
    "POLICY_DENIED_SQLSTATE",
    "PolicyBackend",
    "SqlPolicyBackend",
    "ToolGate",
    "governed_tool_call",
    "is_write_tool",
    "translate_policy_refusal",
]

#: The database's own assertion function (§4). Naming it exactly, and calling it rather than
#: re-implementing it, is what keeps a single source of truth for role x tool x state.
POLICY_ASSERT_SQL: Final = "SELECT policy_assert(%s, %s, %s)"

#: The SQLSTATE `policy_assert` raises on a denial (see `referraleasy_domain.errors.PolicyDeniedError`).
POLICY_DENIED_SQLSTATE: Final = "RE006"

StateLike = WorkflowState | str


@runtime_checkable
class PolicyBackend(Protocol):
    """The authority on ``role x tool x workflow state``.

    Implemented by the database; typed here so the runtime never depends on how
    the answer is obtained, and so a test can supply the answer directly.
    """

    def policy_assert(self, role: str, tool: str, workflow_state: str) -> None:
        """Return ``None`` when permitted, raise when refused."""
        ...


def translate_policy_refusal(
    error: BaseException, *, role: str, tool: str, workflow_state: str
) -> PolicyRefused | None:
    """Turn the database's refusal into a typed runtime refusal.

    Returns ``None`` for anything that is *not* a policy denial — a connection
    error, for instance — so the caller re-raises it unchanged. A transient
    failure must stay transient; flattening it into "policy denied" would retire a
    tool that is perfectly permitted.
    """
    if isinstance(error, PolicyDeniedError):
        return PolicyRefused(
            f"policy denied: {role} may not call {tool} in state {workflow_state}",
            role=role,
            tool=tool,
            workflow_state=workflow_state,
        )
    if getattr(error, "sqlstate", None) == POLICY_DENIED_SQLSTATE:
        return PolicyRefused(
            f"policy denied: {role} may not call {tool} in state {workflow_state}",
            role=role,
            tool=tool,
            workflow_state=workflow_state,
        )
    return None


class SqlPolicyBackend:
    """Calls ``policy_assert`` on a SQL connection.

    ``execute`` is the only seam: pass a function that runs the statement against
    a psycopg connection and raises the driver's error on failure. Keeping it a
    parameter is what lets the gate be unit-tested without a database, and keeps
    this package free of a database driver.
    """

    def __init__(self, execute: Callable[[str, tuple[str, str, str]], None]) -> None:
        self._execute = execute

    def policy_assert(self, role: str, tool: str, workflow_state: str) -> None:
        try:
            self._execute(POLICY_ASSERT_SQL, (role, tool, workflow_state))
        except Exception as error:
            refusal = translate_policy_refusal(
                error, role=role, tool=tool, workflow_state=workflow_state
            )
            if refusal is None:
                raise
            raise refusal from error


class ToolGate:
    """The two authority checks, bound to one role for one workflow state.

    A gate is cheap and stateless: build it at the top of a turn, or hold one per
    service, and pass it to the tool wrapper. It has no API that adds a tool —
    which is the point.
    """

    def __init__(self, role: AgentRole, *, policy: PolicyBackend | None = None) -> None:
        self.role = role
        self._policy = policy

    @property
    def allows(self) -> frozenset[str]:
        """The tools this role may ever call, in any state (the allowlist half)."""
        return allowed_tools(self.role)

    def check(self, tool: str, *, workflow_state: StateLike | None) -> None:
        """Refuse the call unless *both* checks pass. Returns ``None`` when permitted."""
        if tool not in self.allows:
            raise ToolNotAllowed(
                f"{self.role.value} may not call {tool!r}: it is not on this agent's allowlist. "
                "Adding a skill grants no tool permission; the allowlist in roles.py is the only "
                "source of tool authority.",
                role=self.role.value,
                tool=tool,
                workflow_state=str(workflow_state) if workflow_state is not None else None,
            )
        if workflow_state is None:
            raise PolicyUnavailable(
                f"cannot authorise {tool!r} for {self.role.value}: the workflow state is required "
                "for the state-dependent check, and the gate is default-deny.",
                role=self.role.value,
                tool=tool,
                workflow_state=None,
            )
        if self._policy is None:
            raise PolicyUnavailable(
                f"cannot authorise {tool!r} for {self.role.value} in state {workflow_state}: no "
                "policy backend is configured. Default-deny applies; a call is never allowed "
                "because the authority was unavailable.",
                role=self.role.value,
                tool=tool,
                workflow_state=str(workflow_state),
            )
        try:
            self._policy.policy_assert(self.role.value, tool, str(workflow_state))
        except Exception as error:
            # Translate here rather than only in the SQL adapter: a backend is free
            # to raise the domain's own PolicyDeniedError (the database does), and
            # every refusal must arrive as the same typed error. Anything else —
            # a dropped connection, a bug — is re-raised untouched, because a
            # transient failure must stay transient.
            refusal = translate_policy_refusal(
                error, role=self.role.value, tool=tool, workflow_state=str(workflow_state)
            )
            if refusal is None:
                raise
            raise refusal from error


def is_write_tool(tool: str) -> bool:
    """Whether ``tool`` can commit a booking — i.e. whether its failure is readable as "no effect".

    For these tools the runtime never assumes "nothing happened": see
    `BudgetExhausted.outcome` and the `outcome_unknown` reconciliation path.
    """
    return tool in BOOKING_WRITE_TOOLS


@contextmanager
def governed_tool_call(
    gate: ToolGate,
    budget: TurnBudget,
    tool: str,
    *,
    workflow_state: StateLike | None,
    timeout_ms: int | None = None,
) -> Iterator[CallLease]:
    """Authorise *and* budget one tool call, yielding the lease the call may use.

    This is the only sequence an agent should have to write down: authority first
    (a refused tool never consumes budget), then the quota, then the call itself
    under ``lease.timeout_ms``. A write tool is automatically marked as
    ``after_write_attempt``, so a budget failure inside it projects to
    ``outcome_unknown`` — reconciliation, not a retry with a new key.
    """
    gate.check(tool, workflow_state=workflow_state)
    yield budget.lease(
        CapKind.TOOL_CALLS,
        site=tool,
        timeout_ms=timeout_ms,
        after_write_attempt=is_write_tool(tool),
    )
