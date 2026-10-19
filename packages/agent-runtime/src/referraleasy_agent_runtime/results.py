"""Result envelopes: the application outcome, always beside its protocol state.

The mapping itself lives in ``referraleasy_contracts`` and is **not** restated
here — not as a dict, not as a match statement, not as a default. This module
re-exports it so an agent has exactly one import site for result shaping, and adds
the two things a *runtime* owes on top:

1. :func:`envelope` — the wire shape. It always carries ``outcome`` **next to**
   ``a2a_state``. A response that carried only ``a2a_state`` would reintroduce the
   trap the contracts adapter exists to close: ``outcome_unknown`` projects to the
   protocol state ``failed``, which is terminal, while the booking is still
   unresolved. A caller reading only the protocol state would conclude "no booking
   exists" and could double-book.
2. :func:`project_failure` — a typed runtime failure turned into a shaped result,
   so an exception path cannot invent its own outcome.

The guard test in ``tests/agent_runtime/test_a2a_projection.py`` asserts both that
this package never constructs an ``A2ATaskState`` of its own and that every
outcome projects exactly as ``referraleasy_contracts`` says it does.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Protocol

from referraleasy_agent_runtime.errors import AgentRuntimeError
from referraleasy_contracts import (
    A2A_TASK_STATE_BY_EVENT,
    A2A_TASK_STATE_BY_OUTCOME,
    TERMINAL_A2A_STATES,
    AgentTaskResult,
    ProtocolEvent,
    project_event,
    project_outcome,
)
from referraleasy_contracts.generated.common import ResultOutcome
from referraleasy_telemetry import OutcomeClass

__all__ = [
    "A2A_TASK_STATE_BY_EVENT",
    "A2A_TASK_STATE_BY_OUTCOME",
    "OUTCOME_KEY",
    "PROTOCOL_STATE_KEY",
    "RECONCILIATION_HINT",
    "TERMINAL_A2A_STATES",
    "AgentTaskResult",
    "ProtocolEvent",
    "envelope",
    "needs_input",
    "no_match",
    "outcome_class",
    "project_event",
    "project_failure",
    "project_outcome",
    "retryable",
    "succeeded",
    "terminal_error",
    "unknown_outcome",
]

#: Wire keys. ``outcome`` is mandatory in every envelope this package produces.
OUTCOME_KEY: Final = "outcome"
PROTOCOL_STATE_KEY: Final = "a2a_state"

#: Attached when an unresolved external write is possible, so a human reading a
#: log cannot act on ``failed`` alone.
RECONCILIATION_HINT: Final = (
    "outcome_unknown: the external system may have committed a write. Do not retry with a new "
    "key; reconcile with the original idempotency key first."
)


class _Projectable(Protocol):
    """Anything that knows its own projection but does not own the mapping."""

    @property
    def outcome(self) -> ResultOutcome | None: ...

    @property
    def event(self) -> ProtocolEvent | None: ...


def envelope(
    result: AgentTaskResult,
    *,
    trace_id: str | None = None,
    skills: Sequence[str] = (),
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Shape a result for the wire, with the application outcome and state inseparable.

    ``skills`` is the *effective* skill list — what the turn actually loaded, not
    what was available — and it travels with the result so the trace records it.
    """
    body: dict[str, Any] = {
        PROTOCOL_STATE_KEY: result.a2a_state.value,
        OUTCOME_KEY: result.outcome.value if result.outcome is not None else None,
        "event": result.event.value if result.event is not None else None,
        "is_success": result.is_success,
        "protocol_is_terminal": result.protocol_is_terminal,
        "requires_reconciliation": result.requires_reconciliation,
    }
    if result.requires_reconciliation:
        body["reconciliation_hint"] = RECONCILIATION_HINT
    if trace_id is not None:
        body["trace_id"] = trace_id
    body["skills"] = list(skills)
    if extra is not None:
        body.update(extra)
    return body


def project_failure(error: BaseException) -> AgentTaskResult:
    """Project a raised failure onto the protocol, through the contracts table.

    A typed runtime error brings its own projection. Anything else is an
    unclassified failure and projects to ``terminal_error`` — the conservative
    choice: an unknown crash must not be advertised as retryable.
    """
    if isinstance(error, AgentRuntimeError):
        return error.to_result()
    return project_outcome(ResultOutcome.terminal_error)


def succeeded() -> AgentTaskResult:
    """A call that ran and produced an answer."""
    return project_outcome(ResultOutcome.completed)


def no_match() -> AgentTaskResult:
    """A **successful** call whose answer is "nothing matched" (§8).

    Projected as the protocol state ``completed``. Not an error, and never retried.
    """
    return project_outcome(ResultOutcome.no_match)


def needs_input() -> AgentTaskResult:
    """The turn suspended: a patient or a human must answer before it continues."""
    return project_outcome(ResultOutcome.needs_input)


def unknown_outcome() -> AgentTaskResult:
    """A write may have committed and its fate is unknown.

    The protocol state is ``failed`` while the booking is unresolved; see
    :attr:`AgentTaskResult.requires_reconciliation`.
    """
    return project_outcome(ResultOutcome.outcome_unknown)


def terminal_error() -> AgentTaskResult:
    """A failure no retry can fix in this turn."""
    return project_outcome(ResultOutcome.terminal_error)


def retryable() -> AgentTaskResult:
    """A failure that a fresh attempt — and, at the top, a fresh turn — may clear."""
    return project_outcome(ResultOutcome.retryable_error)


#: §5's result → §15's outcome class.
#:
#: **Two tables exist and they must agree.** The gateway keeps its own copy, because its version has
#: to answer for a value this one cannot see: a refused turn never produces a `ResultOutcome` at all
#: — §9's guardrail raises out of the command — so `blocked` is decided from the reason code there
#: and from nothing here. Rather than pretend the two are one, the duplication is declared and
#: `tests/telemetry/test_outcome_mapping_parity.py` fails the suite the moment the shared rows
#: disagree. That is the arrangement `referraleasy_gateway.guardrails` already uses for the layer it
#: once duplicated, and the reason is the same: a mapping that silently drifts produces a chart that
#: is wrong in one service and right in the other.
_OUTCOME_CLASS: Final[Mapping[ResultOutcome, OutcomeClass]] = {
    # The agent answered the question it was asked.
    ResultOutcome.completed: OutcomeClass.SUCCESS_FULL,
    # §15: an empty result is a valid answer, not a failure. A payer network with no in-network
    # specialist is the correct answer to a question that had one.
    ResultOutcome.no_match: OutcomeClass.SUCCESS_EMPTY,
    # The agent answered by asking something back. That is what it set out to do, so it is not an
    # error; the work it started is still open, so it is not a full success either.
    ResultOutcome.needs_input: OutcomeClass.SUCCESS_PARTIAL,
    ResultOutcome.retryable_error: OutcomeClass.ERROR,
    ResultOutcome.terminal_error: OutcomeClass.ERROR,
    # Not an unknown *class* — §5 knows the protocol state it projected to — but a run whose result
    # nobody can stand behind. `error` is closer to that than either success class.
    ResultOutcome.outcome_unknown: OutcomeClass.ERROR,
}


def outcome_class(outcome: ResultOutcome | None) -> OutcomeClass | None:
    """The §15 class for one agent result, or ``None`` when the result carries no outcome.

    ``None`` is reachable and is **not** a missing row in the table: a failure this runtime cannot
    name projects to a protocol state whose result has no §5 outcome at all, which is exactly the
    gap `envelope` documents — ``outcome_unknown`` and "there is no outcome" are different facts, and
    the second is what a bare exception becomes. Inventing a class for it would put a number on a
    chart for a request nobody can classify; the invocation is still counted and still contributes
    its latency.

    Every value that *is* present maps, and there is no default branch — a new `ResultOutcome` fails
    here with a `KeyError` rather than silently landing in a catch-all.
    """
    if outcome is None:
        return None
    return _OUTCOME_CLASS[outcome]


def assert_projection_is_shared() -> dict[str, str]:
    """Prove the projection is the contracts module's, by asking it.

    Used by ``main() --check`` and by the test suite: if someone ever reintroduces
    a local table, the values returned here stop matching the adapter's table only
    if the table itself moved, and the AST guard test catches the local copy.
    """
    return {outcome.value: A2A_TASK_STATE_BY_OUTCOME[outcome].value for outcome in ResultOutcome}


# Deliberately absent: any helper returning a bare `A2ATaskState`. The contracts adapter
# exists so that a protocol state is only ever reachable through an `AgentTaskResult`,
# which carries the application outcome beside it; handing one out here would undo that.
