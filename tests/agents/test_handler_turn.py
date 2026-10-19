"""The handler's turn contract, exercised through a **real** ADK `Runner` offline.

Everything here drives the production path end to end: `RoleHandler.__call__` →
`assembly.build_agent`/`build_runner` → a real `google.adk.runners.Runner` → real
tool execution through `ToolSet.invoke` (allowlist, lease, ledger) → the output
schema → the §5 guards → the §8 projection. Only two things are replaced, and both
are seams the runtime itself defines: the provider transport (`StubApi`) and the
model (`ScriptedLlm`, the offline profile).

The claims, one per test, each of them a rule that would otherwise be a comment:

* a turn that retrieves answers **from what it retrieved**, and its trace names the
  calls that did it;
* a decision that cites evidence the turn never fetched is refused — a fabricated
  citation is the §5 failure;
* a malformed answer is terminal, and **no second model call is made to repair it**
  (§7: the retry owner is the runtime, and a repair pass is a retry by another name);
* an input the role's schema forbids is refused **before the first model call**, and
  the refusal does not echo the offending value — the value may be exactly the
  identity §6 keeps out of this agent;
* a question for the patient suspends the turn instead of projecting an outcome;
* `booking_outcome_unknown` projects to `outcome_unknown` and is not retryable;
* the model-call cap is the runtime's, not ADK's willingness to loop;
* a redelivered command resumes its session, and a new command opens a new one;
* the offline script cannot call a tool the agent was not given.

The offline profile is the harness ADR-0033 records; `OFFLINE_SCRIPT` is not read
here because the script is a fixture of each test rather than a deployment input.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import pytest
from agents_fixtures import (
    COMMAND_ID,
    EVIDENCE_ID,
    PLAN_ID,
    PROVIDER_ID,
    WORKFLOW_ID,
    AllowAllPolicy,
    StubApi,
    decision,
    handler_for,
    minimal_output,
    payload_for,
)

from referraleasy_agent_runtime import (
    AgentRole,
    AgentTaskRequest,
    BudgetCaps,
    BudgetExhausted,
    SuspensionReason,
    TurnBudget,
    TurnReport,
    service_name,
)
from referraleasy_agents.errors import UnknownDataError
from referraleasy_agents.handler import RoleHandler
from referraleasy_agents.offline_model import (
    NO_SCRIPT_MESSAGE,
    ScriptStep,
    TextStep,
    ToolCallStep,
    scripted_model,
)
from referraleasy_contracts.generated.common import ResultOutcome

pytestmark = pytest.mark.contract

#: A fabricated citation: a well-formed evidence id that no tool returned.
FABRICATED_EVIDENCE_ID = "0f0f0f0f-0000-4000-8000-0000000000ff"

#: `check_provider_network`'s answer, valid against the frozen `CoverageResult`.
#: The disclaimer is present because the production tool refuses an answer without
#: it; a fixture that omitted it would make every test here a test of the refusal.
COVERAGE_BODY: Mapping[str, object] = {
    "coverage_id": "0f0f0f0f-0000-4000-8000-00000000000a",
    "plan_id": PLAN_ID,
    "accepts_insurance": True,
    "in_network_for_plan": True,
    "status": "in_network",
    "referral_required": True,
    "prior_auth_required": False,
    "freshness": {"state": "fresh", "age_seconds": 60},
    "evidence_ids": [EVIDENCE_ID],
    "disclaimer_key": "coverage_not_a_guarantee",
    "synthetic": True,
}


async def _turn(
    role: AgentRole,
    *,
    script: Sequence[ScriptStep],
    api: StubApi | None = None,
    workflow_state: str | None = None,
    payload: Mapping[str, object] | None = None,
    budget: TurnBudget | None = None,
    sessions: object = None,
    handler: RoleHandler | None = None,
) -> tuple[TurnReport, RoleHandler, TurnBudget]:
    """One turn, with the handler and budget returned so a test can inspect them."""
    handler = handler or handler_for(
        role,
        model=scripted_model(script),
        api=api if api is not None else StubApi(),
        policy=AllowAllPolicy(),
        sessions=sessions,
    )
    budget = budget or TurnBudget(BudgetCaps(), role=role)
    request = AgentTaskRequest(
        workflow_id=WORKFLOW_ID,
        session_id="session-1",
        workflow_state=workflow_state or _state_for(role),
        task="Run one turn.",
        idempotency_key="idem-1",
        trace_id="trace-1",
        payload=dict(payload if payload is not None else payload_for(role)),
    )
    return await handler(request, budget), handler, budget


def _state_for(role: AgentRole) -> str:
    return {
        AgentRole.COVERAGE_NETWORK: "COVERAGE_CHECK",
        AgentRole.SCHEDULING: "BOOKING_IN_FLIGHT",
    }.get(role, "INTAKE")


# ── the turn runs, for real ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_turn_answers_from_what_its_tool_returned() -> None:
    """The whole path: ADK loop, one tool call, the ledger, the schema, the projection."""
    api = StubApi({"/coverage/lookup": dict(COVERAGE_BODY)})
    answer = minimal_output(
        AgentRole.COVERAGE_NETWORK,
        decision=decision(
            action="coverage_network:check_provider_network", evidence_ids=[EVIDENCE_ID]
        ),
    )
    report, _, _ = await _turn(
        AgentRole.COVERAGE_NETWORK,
        api=api,
        script=[
            ToolCallStep(
                tool="check_provider_network", args={"plan_id": PLAN_ID, "provider_id": PROVIDER_ID}
            ),
            TextStep(text=json.dumps(answer)),
        ],
    )

    assert report.result.outcome is ResultOutcome.completed
    assert [entry["tool"] for entry in report.trace["tools"]] == ["check_provider_network"]
    assert report.trace["evidence_ids"] == [EVIDENCE_ID]
    assert api.calls == [
        (
            "POST",
            "/coverage/lookup",
            {"plan_id": PLAN_ID, "provider_id": PROVIDER_ID, "hospital_id": None},
        )
    ]


@pytest.mark.asyncio
async def test_a_decision_that_cites_evidence_the_turn_never_retrieved_is_refused() -> None:
    """§5: a citation must be a fact this turn read, not one the model remembered."""
    api = StubApi({"/coverage/lookup": dict(COVERAGE_BODY)})
    answer = minimal_output(
        AgentRole.COVERAGE_NETWORK,
        decision=decision(
            action="coverage_network:check_provider_network",
            evidence_ids=[FABRICATED_EVIDENCE_ID],
        ),
    )
    with pytest.raises(UnknownDataError) as raised:
        await _turn(
            AgentRole.COVERAGE_NETWORK,
            api=api,
            script=[
                ToolCallStep(
                    tool="check_provider_network",
                    args={"plan_id": PLAN_ID, "provider_id": PROVIDER_ID},
                ),
                TextStep(text=json.dumps(answer)),
            ],
        )
    message = str(raised.value)
    assert FABRICATED_EVIDENCE_ID in message
    assert EVIDENCE_ID in message, "the refusal names what was retrieved, so the gap is findable"


@pytest.mark.asyncio
async def test_a_malformed_answer_is_terminal_and_is_not_repaired_by_a_second_call() -> None:
    """§7: no model call is made to reword — or to repair — a failing turn."""
    report, _, budget = await _turn(
        AgentRole.COVERAGE_NETWORK, script=[TextStep(text="I am not sure how to answer.")]
    )
    assert report.result.outcome is ResultOutcome.terminal_error
    assert report.trace["refused"] == "output"
    assert budget.snapshot().model_calls == 1, (
        "the malformed answer was repaired with another model call: a repair pass is a retry "
        "with a different name, and the retry owner is the runtime (§7)"
    )


@pytest.mark.asyncio
async def test_an_exhausted_script_is_a_refusal_and_not_a_result() -> None:
    """The offline harness fails loudly instead of falling back to a provider."""
    report, _, _ = await _turn(AgentRole.COVERAGE_NETWORK, script=[])
    assert report.result.outcome is ResultOutcome.terminal_error
    assert report.trace["refused"] == "output"
    assert "not a JSON object" in report.trace["error"]
    assert NO_SCRIPT_MESSAGE.split(".")[0] not in json.dumps(report.trace)


@pytest.mark.asyncio
async def test_an_input_the_schema_forbids_is_refused_before_any_model_call() -> None:
    """§6 enforced at the door: a payload the role must not see never reaches the model."""
    payload = payload_for(AgentRole.PROVIDER_MATCHING)
    payload["date_of_birth"] = "1990-04-11"
    report, _, budget = await _turn(AgentRole.PROVIDER_MATCHING, script=[], payload=payload)

    assert report.result.outcome is ResultOutcome.terminal_error
    assert report.trace["refused"] == "input_schema"
    assert "date_of_birth" in report.trace["errors"]
    assert budget.snapshot().model_calls == 0
    assert "1990-04-11" not in json.dumps(report.trace), (
        "the refusal echoed the offending value; the value may be exactly the identity §6 keeps "
        "out of this agent's context"
    )


@pytest.mark.asyncio
async def test_a_question_for_the_patient_suspends_the_turn_rather_than_projecting() -> None:
    """§8: a turn waiting on a person files a suspension instead of an outcome."""
    answer = minimal_output(
        AgentRole.COVERAGE_NETWORK,
        decision=decision(
            action="coverage_network:check_plan",
            reason_code="missing_mandatory_field",
            missing_fields=[{"field": "postal_code", "reason_code": "missing_mandatory_field"}],
        ),
    )
    report, handler, _ = await _turn(
        AgentRole.COVERAGE_NETWORK, script=[TextStep(text=json.dumps(answer))]
    )

    assert report.suspension is not None
    assert report.suspension.reason is SuspensionReason.PATIENT_INPUT
    assert report.result.outcome is ResultOutcome.needs_input
    assert report.trace["suspension"] == report.suspension.id
    assert handler.service.suspensions.load(report.suspension.id) is not None
    assert report.suspension.expected_answer_schema is not None
    assert report.suspension.expected_answer_schema["required"] == ["postal_code"]


@pytest.mark.asyncio
async def test_an_unknown_booking_outcome_is_unknown_and_never_retryable() -> None:
    """§7's one-way difference: `outcome_unknown` is not the retryable failure."""
    answer = minimal_output(
        AgentRole.SCHEDULING,
        patient_message="",
        decision=decision(
            action="scheduling:lookup_request", reason_code="booking_outcome_unknown"
        ),
    )
    report, _, _ = await _turn(AgentRole.SCHEDULING, script=[TextStep(text=json.dumps(answer))])
    assert report.result.outcome is ResultOutcome.outcome_unknown
    assert report.result.a2a_state == "failed"


@pytest.mark.asyncio
async def test_the_model_call_cap_is_the_runtime_s_not_the_loop_s() -> None:
    """A script that needs two calls cannot have them when the budget allows one."""
    caps = BudgetCaps(max_model_calls=1)
    answer = minimal_output(AgentRole.COVERAGE_NETWORK)
    with pytest.raises(BudgetExhausted):
        await _turn(
            AgentRole.COVERAGE_NETWORK,
            script=[
                ToolCallStep(
                    tool="check_provider_network",
                    args={"plan_id": PLAN_ID, "provider_id": PROVIDER_ID},
                ),
                TextStep(text=json.dumps(answer)),
            ],
            api=StubApi({"/coverage/lookup": dict(COVERAGE_BODY)}),
            budget=TurnBudget(caps, role=AgentRole.COVERAGE_NETWORK),
        )


@pytest.mark.asyncio
async def test_the_offline_script_cannot_call_a_tool_the_agent_was_not_given() -> None:
    """The script fails where the name is still the script's, not inside the agent loop."""
    with pytest.raises(ValueError) as raised:
        await _turn(
            AgentRole.COVERAGE_NETWORK,
            script=[ToolCallStep(tool="create_booking", args={"idempotency_key": "x" * 8})],
        )
    assert "create_booking" in str(raised.value)
    assert "was not given" in str(raised.value)


# ── session identity (§7, ADR-0033) ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_redelivered_command_resumes_its_session_and_a_new_command_opens_one() -> None:
    """The session is keyed by workflow *and* command, which is what makes redelivery safe."""
    from google.adk.sessions import InMemorySessionService

    sessions = InMemorySessionService()
    answer = minimal_output(AgentRole.COVERAGE_NETWORK)
    script = [TextStep(text=json.dumps(answer))]
    handler = handler_for(
        AgentRole.COVERAGE_NETWORK,
        model=scripted_model(script),
        api=StubApi(),
        policy=AllowAllPolicy(),
        sessions=sessions,
    )

    await _turn(AgentRole.COVERAGE_NETWORK, script=script, sessions=sessions, handler=handler)
    await _turn(AgentRole.COVERAGE_NETWORK, script=script, sessions=sessions, handler=handler)

    listing = await sessions.list_sessions(app_name=service_name(AgentRole.COVERAGE_NETWORK))
    assert len(listing.sessions) == 1, "a redelivered command opened a second session"
    only = listing.sessions[0]
    assert only.id == f"{WORKFLOW_ID}:{COMMAND_ID}"
    assert only.user_id == WORKFLOW_ID

    other = payload_for(AgentRole.COVERAGE_NETWORK)
    other["command_id"] = "0f0f0f0f-0000-4000-8000-0000000000aa"
    await _turn(
        AgentRole.COVERAGE_NETWORK, script=script, sessions=sessions, handler=handler, payload=other
    )
    listing = await sessions.list_sessions(app_name=service_name(AgentRole.COVERAGE_NETWORK))
    assert len(listing.sessions) == 2, "a new command must not resume another command's session"
