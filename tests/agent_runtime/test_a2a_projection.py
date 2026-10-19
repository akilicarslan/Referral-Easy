"""The A2A projection is the contracts module's, and this package may not restate it.

The whole point of delegating the mapping is that there is exactly one table in the
system that says what ``outcome_unknown`` looks like on the wire. The first half of
this module asserts the delegation holds (by identity, and by behaviour for all six
outcomes); the second half is a structural guard — an AST walk over the package that
fails if anyone ever writes a second table, or constructs a protocol state directly.
A behavioural test alone would not catch that: a local copy agrees with the real
table on the day it is written, which is precisely why the guard reads the source.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from referraleasy_agent_runtime import results
from referraleasy_agent_runtime.errors import (
    ConfigurationError,
    PolicyRefused,
    ServiceAuthError,
    ToolNotAllowed,
    UnknownRoleError,
    UpstreamRejected,
    UpstreamTransient,
)
from referraleasy_agent_runtime.results import (
    OUTCOME_KEY,
    PROTOCOL_STATE_KEY,
    RECONCILIATION_HINT,
    assert_projection_is_shared,
    envelope,
    project_failure,
)
from referraleasy_contracts import (
    A2A_TASK_STATE_BY_OUTCOME,
    ProtocolEvent,
)
from referraleasy_contracts import (
    project_outcome as contracts_project_outcome,
)
from referraleasy_contracts.generated.common import A2ATaskState, ResultOutcome

PACKAGE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "agent-runtime"
    / "src"
    / "referraleasy_agent_runtime"
)

#: The protocol states, as the contracts module spells them. Used to detect a local
#: table written with string literals rather than enum members.
STATE_VALUES = {state.value for state in A2ATaskState}
OUTCOME_VALUES = {outcome.value for outcome in ResultOutcome}

#: The expected projection, written out **in the test** on purpose. If this ever stops
#: matching `referraleasy_contracts`, the runtime and the contract have drifted apart
#: and one of them is wrong about what a caller will observe.
EXPECTED_STATE_BY_OUTCOME = {
    "completed": "completed",
    "no_match": "completed",
    "needs_input": "input-required",
    "retryable_error": "failed",
    "terminal_error": "failed",
    "outcome_unknown": "failed",
}


def _package_sources() -> dict[str, str]:
    """Every module in the runtime, by file name, as source text."""
    paths = sorted(PACKAGE_ROOT.rglob("*.py"))
    assert paths, f"no package sources found under {PACKAGE_ROOT}"
    return {path.name: path.read_text(encoding="utf-8") for path in paths}


def test_there_are_exactly_six_outcomes_and_they_are_all_projected() -> None:
    assert set(EXPECTED_STATE_BY_OUTCOME) == OUTCOME_VALUES
    assert {
        outcome.value: A2A_TASK_STATE_BY_OUTCOME[outcome].value for outcome in ResultOutcome
    } == (EXPECTED_STATE_BY_OUTCOME)


@pytest.mark.parametrize(("outcome_name", "state"), sorted(EXPECTED_STATE_BY_OUTCOME.items()))
def test_each_outcome_projects_as_the_contract_says(outcome_name: str, state: str) -> None:
    outcome = ResultOutcome(outcome_name)
    result = contracts_project_outcome(outcome)

    assert result.a2a_state.value == state
    assert results.project_outcome(outcome) == result


def test_the_tables_are_the_contracts_objects_not_copies() -> None:
    """Identity, not equality: a copy would pass an equality check and still drift."""
    assert results.A2A_TASK_STATE_BY_OUTCOME is A2A_TASK_STATE_BY_OUTCOME
    assert results.project_outcome is contracts_project_outcome
    assert assert_projection_is_shared() == EXPECTED_STATE_BY_OUTCOME


def test_an_unknown_write_outcome_reports_failed_but_asks_for_reconciliation() -> None:
    """The trap this whole delegation exists to close.

    The protocol state is ``failed`` — terminal — and the booking may nonetheless
    exist. A caller that reads only the state concludes "nothing was booked" and
    books again. The outcome, kept beside the state, is what prevents that.
    """
    result = results.unknown_outcome()

    assert result.a2a_state is A2ATaskState.failed
    assert result.protocol_is_terminal is True, "at the protocol level this task is done"
    assert result.outcome is ResultOutcome.outcome_unknown
    assert result.requires_reconciliation is True


def test_the_envelope_never_ships_a_state_without_its_outcome() -> None:
    body = envelope(results.unknown_outcome(), trace_id="trace-1", skills=["booking-wizard"])

    assert body[OUTCOME_KEY] == "outcome_unknown"
    assert body[PROTOCOL_STATE_KEY] == "failed"
    assert body["requires_reconciliation"] is True
    assert body["reconciliation_hint"] == RECONCILIATION_HINT
    assert "reconcile" in str(body["reconciliation_hint"]).lower()
    assert body["trace_id"] == "trace-1"
    assert body["skills"] == ["booking-wizard"]


def test_the_envelope_omits_the_reconciliation_hint_when_there_is_nothing_to_reconcile() -> None:
    body = envelope(results.no_match())

    assert body[OUTCOME_KEY] == "no_match"
    assert body[PROTOCOL_STATE_KEY] == "completed"
    assert body["is_success"] is True
    assert "reconciliation_hint" not in body


def test_a_valid_empty_result_is_a_success_that_is_not_an_error() -> None:
    result = results.no_match()

    assert result.is_success is True
    assert result.a2a_state is A2ATaskState.completed
    assert result.requires_reconciliation is False


def test_needs_input_is_not_terminal_so_the_task_can_be_resumed() -> None:
    result = results.needs_input()

    assert result.a2a_state is A2ATaskState.input_required
    assert result.protocol_is_terminal is False, "a suspended turn must remain resumable"


def test_a_refusal_projects_as_an_event_not_as_a_call_outcome() -> None:
    """No agent call happened, so there is no outcome to invent."""
    refusal = ToolNotAllowed(
        "not on the allowlist", role="scheduling", tool="x", workflow_state=None
    )
    result = refusal.to_result()

    assert result.outcome is None
    assert result.event is ProtocolEvent.POLICY_REFUSED
    assert result.a2a_state is A2ATaskState.rejected


def test_an_auth_failure_projects_as_a_protocol_event() -> None:
    error = ServiceAuthError("no token", www_authenticate='Bearer realm="referraleasy"')
    result = error.to_result()

    assert result.outcome is None
    assert result.event is ProtocolEvent.AUTH_FAILED
    assert result.a2a_state is A2ATaskState.auth_required


@pytest.mark.parametrize(
    ("error", "expected_outcome"),
    [
        (UpstreamTransient("timed out"), ResultOutcome.retryable_error),
        (UpstreamRejected("422", status_code=422), ResultOutcome.terminal_error),
        (PolicyRefused("denied", role="scheduling", tool="x", workflow_state="INTAKE"), None),
        (ConfigurationError("no role"), ResultOutcome.terminal_error),
        (UnknownRoleError("nope"), ResultOutcome.terminal_error),
    ],
)
def test_a_typed_failure_brings_its_own_projection(
    error: BaseException, expected_outcome: ResultOutcome | None
) -> None:
    result = project_failure(error)

    if expected_outcome is None:
        assert result.outcome is None
        assert result.event is ProtocolEvent.POLICY_REFUSED
    else:
        assert result.outcome is expected_outcome
        assert result.a2a_state.value == EXPECTED_STATE_BY_OUTCOME[expected_outcome.value]


def test_an_unclassified_crash_is_terminal_never_retryable() -> None:
    """Advertising an unknown bug as retryable would invite a loop that re-runs it."""
    result = project_failure(RuntimeError("something nobody planned for"))

    assert result.outcome is ResultOutcome.terminal_error
    assert result.requires_reconciliation is False


def test_a_retryable_error_is_still_terminal_at_the_protocol_level() -> None:
    """Retrying is a decision above the protocol; a task in `failed` is a task in `failed`."""
    result = results.retryable()

    assert result.a2a_state is A2ATaskState.failed
    assert result.protocol_is_terminal is True
    assert result.requires_reconciliation is False, "a retryable failure is read as 'no effect'"


def test_the_package_never_constructs_a_protocol_state() -> None:
    """A protocol state is reachable only through an AgentTaskResult.

    Constructing an ``A2ATaskState`` here would let a response carry a state with no
    outcome beside it — the shape that makes ``outcome_unknown`` dangerous.
    """
    offenders = [
        f"{name}:{lineno} references A2ATaskState"
        for name, source in _package_sources().items()
        for lineno in _references_protocol_state(source)
    ]
    assert offenders == [], (
        "the runtime must never name a protocol state itself; project through "
        f"referraleasy_contracts instead. Found: {offenders}"
    )


def test_the_package_never_writes_a_second_outcome_table() -> None:
    """No dict in this package may pair an outcome with a state — in any spelling."""
    offenders = [
        f"{name}:{lineno} looks like a local outcome→state table"
        for name, source in _package_sources().items()
        for lineno in _local_outcome_tables(source)
    ]
    assert offenders == []


def test_the_package_never_defines_its_own_projection_functions() -> None:
    """`def project_outcome` here would shadow the contracts adapter at import sites."""
    offenders = [
        f"{name}:{lineno} defines {function}"
        for name, source in _package_sources().items()
        for lineno, function in _local_projection_definitions(source)
    ]
    assert offenders == []


# --- the guards themselves -------------------------------------------------------------------
# Each guard above is a scanner. A scanner that silently matches nothing is worse than no
# scanner, so each one is run here against a source that *does* contain the thing it hunts.

PLANTED_STATE_REFERENCE = """
from referraleasy_contracts.generated.common import A2ATaskState


def state_for(outcome: str) -> A2ATaskState:
    return A2ATaskState.failed
"""

PLANTED_OUTCOME_TABLE_ENUM = """
from referraleasy_contracts import A2A_TASK_STATE_BY_OUTCOME
from referraleasy_contracts.generated.common import A2ATaskState, ResultOutcome

STATES = {
    ResultOutcome.outcome_unknown: A2ATaskState.failed,
    ResultOutcome.completed: A2ATaskState.completed,
}
"""

PLANTED_OUTCOME_TABLE_STRINGS = """
STATES = {"outcome_unknown": "failed", "needs_input": "input-required"}
"""

PLANTED_PROJECTION = """
def project_outcome(outcome):
    return {"completed": "completed"}[outcome.value]
"""


def test_the_protocol_state_guard_catches_a_planted_reference() -> None:
    assert _references_protocol_state(PLANTED_STATE_REFERENCE) != []


def test_the_outcome_table_guard_catches_a_planted_table_in_both_spellings() -> None:
    assert _local_outcome_tables(PLANTED_OUTCOME_TABLE_ENUM) != []
    assert _local_outcome_tables(PLANTED_OUTCOME_TABLE_STRINGS) != []


def test_the_projection_guard_catches_a_planted_local_projection() -> None:
    assert _local_projection_definitions(PLANTED_PROJECTION) != []


def test_the_guards_do_not_fire_on_the_real_contracts_adapter() -> None:
    """The other half of a useful guard: it must not fire on correct code either."""
    assert _references_protocol_state("state = result.a2a_state\n") == []
    assert _local_outcome_tables("STATE_BY_OUTCOME = dict(A2A_TASK_STATE_BY_OUTCOME)\n") == []
    assert (
        _local_projection_definitions("from referraleasy_contracts import project_outcome\n") == []
    )


def _references_protocol_state(source: str) -> list[int]:
    """Line numbers where a module names ``A2ATaskState`` (the state class itself)."""
    tree = ast.parse(source)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "A2ATaskState"
    ]


def _local_outcome_tables(source: str) -> list[int]:
    """Line numbers of dict literals that pair an outcome name with a state name."""
    tree = ast.parse(source)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        and {_literal_or_attr(key) for key in node.keys} & OUTCOME_VALUES
        and {_literal_or_attr(value) for value in node.values} & STATE_VALUES
    ]


def _local_projection_definitions(source: str) -> list[tuple[int, str]]:
    """``(line, name)`` for a locally defined projection that would shadow the adapter."""
    forbidden = {"project_outcome", "project_event", "project_task_state"}
    tree = ast.parse(source)
    return [
        (node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in forbidden
    ]


def _literal_or_attr(node: ast.expr | None) -> str:
    """The name a dict key/value denotes: a string literal, or ``Enum.member``."""
    if node is None:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""
