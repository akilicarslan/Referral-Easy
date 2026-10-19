"""Cross the real HTTP adapter and receiving ADK service, rather than matching two stubs."""

from __future__ import annotations

import json

import httpx
import pytest
from agents_fixtures import WORKFLOW_ID, AllowAllPolicy, StubApi, minimal_output, payload_for

from referraleasy_agent_runtime import AgentRole, AgentSettings, BudgetCaps, TurnBudget
from referraleasy_agents.errors import UnknownDataError
from referraleasy_agents.offline_model import TextStep, scripted_model
from referraleasy_agents.service import HttpDelegation, build_app


@pytest.mark.asyncio
async def test_http_delegation_reads_the_actual_service_envelope() -> None:
    role = AgentRole.REFERRAL_INTAKE
    output = minimal_output(role)
    env = {
        "APP_ENV": "test",
        "AGENT_ROLE": role.value,
        "AGENT_MODEL_PROVIDER": "openrouter",
        "AGENT_MODEL_ID": "synthetic/not-called",
        "OPENROUTER_API_KEY": "test-not-a-key",
        "A2A_SERVICE_TOKEN": "fixture-token",
        "MODEL_BACKEND": "offline",
    }
    app = build_app(
        AgentSettings.from_env(env, role=role),
        env=env,
        api=StubApi(),
        policy=AllowAllPolicy(),
        model=scripted_model([TextStep(text=json.dumps(output))]),
    )
    async with httpx.AsyncClient(
        base_url="http://peer", transport=httpx.ASGITransport(app=app)
    ) as client:
        port = HttpDelegation(
            base_url="http://peer", token="fixture-token", caps=BudgetCaps(), client=client
        )
        answer = await port.send(
            target_role=role,
            budget=TurnBudget(BudgetCaps(), role=AgentRole.CARE_COORDINATOR),
            request={
                "workflow_id": WORKFLOW_ID,
                "session_id": "delegated",
                "workflow_state": "INTAKE",
                "task": "Validate this referral",
                "idempotency_key": "delegated-1",
                "trace_id": "trace-1",
                "payload": payload_for(role),
            },
        )
    assert answer["state"] == "completed"
    assert answer["outcome"] == "completed"
    assert answer["is_success"] is True
    assert answer["output"]["draft"].items() >= output["draft"].items()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"result": []}, {"result": {"a2a_state": "completed"}}])
async def test_a_malformed_peer_answer_is_refused(body: object) -> None:
    async with httpx.AsyncClient(
        base_url="http://peer",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    ) as client:
        port = HttpDelegation(
            base_url="http://peer", token="fixture-token", caps=BudgetCaps(), client=client
        )
        with pytest.raises(UnknownDataError):
            await port.send(
                target_role=AgentRole.REFERRAL_INTAKE,
                request={},
                budget=TurnBudget(BudgetCaps(), role=AgentRole.CARE_COORDINATOR),
            )
