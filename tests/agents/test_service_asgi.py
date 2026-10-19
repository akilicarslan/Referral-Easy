"""The agents' service layer over HTTP: what this package adds to the runtime's app.

`tests/agent_runtime/test_app.py` owns the runtime's HTTP surface — the health route
being open, the task route being guarded, one budget per request, a failure never
being an HTTP status. None of that is repeated here. What is tested here is what
`referraleasy_agents.service` contributes, over a real `TestClient`:

* **the policy authority is attached, or nothing runs.** With no `DATABASE_URL`
  there is no backend, and the gate refuses the first tool call — the provider is
  never asked, which is the difference between a refusal and a wrong answer. Two
  tests: one where the authority is absent, one where the real table parsed from
  `migrations/014_align_domain_vocabulary.sql` decides instead, and there §4's
  ordering rule is what refuses (a provider-level network check has no subject in
  `COVERAGE_CHECK`) while the same task one state later completes.
* **a failure leaves the turn shaped and the model un-asked.** A transport fault in a
  tool propagates through the ADK loop and is projected by the runtime. The script
  holds exactly one step, so the count of model calls in the response is a direct
  reading of §7: a failure path that asked the model to reword the failure would show
  a second call, and its answer would land in the output-schema refusal instead of the
  fault's own outcome. Both are asserted.
* **the reference deployment needs more than the offline profile uses.**
  `AgentSettings.from_env` requires `AGENT_MODEL_ID` and a non-empty
  `OPENROUTER_API_KEY` even when `MODEL_BACKEND=offline`, so a deployment that never
  reaches a provider still has to name one. Pinned as it stands: a reviewer should see
  the requirement rather than discover it in a container log.
* **a refusal reaches the wire as a state, without its reason.** Every failure
  envelope this app answers with (a policy refusal, a transport fault, no handler
  wired) carries `a2a_state`, `outcome`, `event` and the flags — and no message. The
  text exists in the raised exception and nowhere else. That is asserted, not
  worked around, because a caller that cannot be told *why* a task was refused is a
  caller whose operator has only the state to go on.

The environment is spelled out per test (dummy values only) rather than read from the
developer's shell, so no test here can pick up a real credential and none can pass
because one happened to be exported.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from agents_fixtures import (
    EVIDENCE_ID,
    PLAN_ID,
    PROVIDER_ID,
    ROLES,
    WORKFLOW_ID,
    AllowAllPolicy,
    StubApi,
    minimal_output,
    payload_for,
)
from fastapi.testclient import TestClient
from policy_table import InMemoryPolicy

from referraleasy_agent_runtime import (
    AgentRole,
    AgentSettings,
    ConfigurationError,
    UpstreamTransient,
    create_app,
    service_name,
)
from referraleasy_agent_runtime.app import HEALTH_PATH, TASKS_PATH
from referraleasy_agent_runtime.roles import AGENT_ROLE_ENV
from referraleasy_agents.offline_model import ScriptStep, TextStep, ToolCallStep, scripted_model
from referraleasy_agents.service import asgi_app, build_app

pytestmark = pytest.mark.contract

#: The environment the harness runs with: `.env.example`'s shape, this checkout's
#: values. No `DATABASE_URL`, which is the point of the first authority test.
ENV: Mapping[str, str] = {
    "APP_ENV": "test",
    AGENT_ROLE_ENV: AgentRole.COVERAGE_NETWORK.value,
    "AGENT_MODEL_PROVIDER": "openrouter",
    "AGENT_MODEL_ID": "synthetic/model-not-called",
    "OPENROUTER_API_KEY": "test-key-not-a-real-credential",
    "A2A_SERVICE_TOKEN": "test-service-token",
    "MODEL_BACKEND": "offline",
}

AUTH = {"Authorization": f"Bearer {ENV['A2A_SERVICE_TOKEN']}"}

#: `check_provider_network`'s answer, valid against the frozen `CoverageResult`. The
#: disclaimer is present because the production tool refuses an answer without it.
COVERAGE_BODY: Mapping[str, Any] = {
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

#: The script: ask the provider API, then answer. Two steps, and the second is the
#: one an unreworded failure must never need.
COVERAGE_SCRIPT: tuple[ScriptStep, ...] = (
    ToolCallStep(
        tool="check_provider_network", args={"plan_id": PLAN_ID, "provider_id": PROVIDER_ID}
    ),
    TextStep(text=json.dumps(minimal_output(AgentRole.COVERAGE_NETWORK))),
)

#: Distinguishes "no policy argument" from "policy=None, please". The first means the
#: wiring decides; the second is a deployment whose authority is genuinely absent, and
#: a falsy check would silently turn it into the permissive stub.
_UNSET: Any = object()


class RefusingApi(StubApi):
    """A provider API whose calls fail in transport, recording that they were made."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    async def post(self, path: str, payload: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("POST", path, dict(payload)))
        raise self.error


def settings(role: AgentRole = AgentRole.COVERAGE_NETWORK) -> AgentSettings:
    return AgentSettings.from_env(ENV, role=role)


def client(
    *,
    role: AgentRole = AgentRole.COVERAGE_NETWORK,
    api: Any = None,
    policy: Any = _UNSET,
    script: Sequence[ScriptStep] = COVERAGE_SCRIPT,
) -> TestClient:
    """This role's wired app, with the transport, the authority and the model replaced."""
    overrides: dict[str, Any] = {
        "api": api if api is not None else StubApi({"/coverage/lookup": dict(COVERAGE_BODY)}),
        "policy": AllowAllPolicy() if policy is _UNSET else policy,
        "model": scripted_model(tuple(script)),
    }
    return TestClient(build_app(settings(role), env=ENV, **overrides))


def task_body(role: AgentRole, *, workflow_state: str) -> dict[str, Any]:
    """One task request: the role's own input, plus the command the turn is keyed by."""
    return {
        "workflow_id": WORKFLOW_ID,
        "session_id": f"session-{role.value}",
        "workflow_state": workflow_state,
        "task": "Run one turn of this specialist.",
        "idempotency_key": "idem-1",
        "trace_id": "trace-1",
        "payload": payload_for(role),
    }


def turn(http: TestClient, role: AgentRole, *, workflow_state: str) -> dict[str, Any]:
    """POST one task, and hand back the shaped response after asserting the round trip."""
    response = http.post(
        TASKS_PATH, json=task_body(role, workflow_state=workflow_state), headers=AUTH
    )
    assert response.status_code == 200, "a refusal is a protocol answer, not a transport error"
    body: dict[str, Any] = response.json()
    assert set(body) == {"trace", "result"}, "the response is one trace beside one shaped result"
    return body


# ── identity ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ROLES, ids=lambda role: role.value)
def test_health_reports_the_role_and_the_service_this_app_is(role: AgentRole) -> None:
    """Unguarded, and it names the deployment: `service_name(role)`, not the card name."""
    with client(role=role) as http:
        body = http.get(HEALTH_PATH).json()
    assert body == {"status": "ok", "role": role.value, "service": service_name(role)}


def test_the_agents_task_route_is_guarded_like_the_runtime_s() -> None:
    """A peer must present the service token; without one the turn never starts."""
    with client() as http:
        response = http.post(
            TASKS_PATH, json=task_body(AgentRole.COVERAGE_NETWORK, workflow_state="COVERAGE_CHECK")
        )
    assert response.status_code == 401


# ── a whole turn ─────────────────────────────────────────────────────────────────────────────


def test_a_wired_service_runs_one_turn_of_its_own_role() -> None:
    """The end-to-end shape a peer sees: the tool call, the evidence, the session, the decision."""
    api = StubApi({"/coverage/lookup": dict(COVERAGE_BODY)})
    with client(api=api) as http:
        body = turn(http, AgentRole.COVERAGE_NETWORK, workflow_state="AWAITING_DOCTOR_CHOICE")

    assert api.calls == [
        (
            "POST",
            "/coverage/lookup",
            {"plan_id": PLAN_ID, "provider_id": PROVIDER_ID, "hospital_id": None},
        )
    ]
    assert body["result"]["a2a_state"] == "completed"
    assert body["result"]["outcome"] == "completed"
    assert body["result"]["is_success"] is True
    assert body["result"]["requires_reconciliation"] is False
    assert body["result"]["skills"] == ["coverage-interpretation"]
    assert body["trace"]["tools"] == [
        {
            "tool": "check_provider_network",
            "timeout_ms": 1500,
            "attempt": 1,
            "evidence_ids": [EVIDENCE_ID],
        }
    ]
    assert body["trace"]["evidence_ids"] == [EVIDENCE_ID]
    assert body["trace"]["decision"]["action"] == "coverage_network:check_plan"
    assert body["trace"]["session"] == {
        "app_name": service_name(AgentRole.COVERAGE_NETWORK),
        "user_id": WORKFLOW_ID,
        "session_id": f"{WORKFLOW_ID}:{payload_for(AgentRole.COVERAGE_NETWORK)['command_id']}",
    }


def test_the_effective_skills_reach_the_wire_as_the_selection_that_made_them() -> None:
    """The runtime seeds `trace["skills"]` with a list; the handler's entry replaces it.

    Deliberate here, and pinned: a reader who wants the list must read
    `effective_skills`, and the entry beside it says which role and state it was made
    for and why the other manifests were rejected. A bare list would lose that.
    """
    with client() as http:
        body = turn(http, AgentRole.COVERAGE_NETWORK, workflow_state="AWAITING_DOCTOR_CHOICE")

    skills = body["trace"]["skills"]
    assert isinstance(skills, dict), "the handler's selection entry must not be flattened to a list"
    assert skills["effective_skills"] == ["coverage-interpretation"]
    assert skills["role"] == "coverage_network"
    assert skills["workflow_state"] == "AWAITING_DOCTOR_CHOICE"
    assert set(skills["rejected"]) == set(skills["considered"]) - {"coverage-interpretation"}


# ── the authority ────────────────────────────────────────────────────────────────────────────


def test_with_no_policy_authority_the_first_tool_call_is_refused_and_nothing_is_asked() -> None:
    """Default-deny at the service layer: no backend is no authority, not an unknown.

    The deployment here has no `DATABASE_URL`, so `build_service` wires `policy=None`
    and the gate refuses the call. `rejected` is the protocol projection of a policy
    refusal — an application outcome of `None`, because no agent call produced a
    result.
    """
    api = StubApi({"/coverage/lookup": dict(COVERAGE_BODY)})
    with client(api=api, policy=None) as http:
        body = turn(http, AgentRole.COVERAGE_NETWORK, workflow_state="COVERAGE_CHECK")

    assert body["result"]["a2a_state"] == "rejected"
    assert body["result"]["outcome"] is None
    assert body["result"]["event"] == "policy_refused"
    assert api.calls == [], "the provider was asked despite the turn having no authority to ask"
    assert "no policy backend" not in json.dumps(body), (
        "the gate's reason reached the wire; if that is now intended, this test should "
        "assert the text instead of its absence (see the reason-text test below)"
    )


def test_the_policy_table_refuses_a_provider_check_before_a_doctor_is_known() -> None:
    """§4's ordering, decided by the migration's own table rather than by a fixture.

    `check_provider_network` asks about *a provider's* network status. In
    `COVERAGE_CHECK` nobody has been chosen yet, so the row is `allowed = false` with
    the note "a provider-level check before a provider is chosen has no subject".
    """
    api = StubApi({"/coverage/lookup": dict(COVERAGE_BODY)})
    with client(api=api, policy=InMemoryPolicy()) as http:
        body = turn(http, AgentRole.COVERAGE_NETWORK, workflow_state="COVERAGE_CHECK")

    assert body["result"]["a2a_state"] == "rejected"
    assert api.calls == []


def test_the_same_task_is_permitted_once_the_check_has_a_subject() -> None:
    """One state later, with the same table: the tool runs and the turn completes."""
    api = StubApi({"/coverage/lookup": dict(COVERAGE_BODY)})
    with client(api=api, policy=InMemoryPolicy()) as http:
        body = turn(http, AgentRole.COVERAGE_NETWORK, workflow_state="AWAITING_DOCTOR_CHOICE")

    assert body["result"]["outcome"] == "completed"
    assert [call[1] for call in api.calls] == ["/coverage/lookup"]


# ── failures ─────────────────────────────────────────────────────────────────────────────────


def test_a_failed_tool_is_projected_as_its_own_fault_with_no_second_model_call() -> None:
    """§7 at the boundary: the fault's outcome travels, and the model is not asked again.

    The script holds one step — the tool call. A failure path that asked the model to
    reword the failure would spend a second model call, and since the script has no
    second step, the answer would be the offline profile's "no scripted step left"
    text, which fails output validation and projects as a *terminal* error with
    `trace["refused"] == "output"`. Neither the count nor the projection does that.
    """
    api = RefusingApi(UpstreamTransient("the provider registry did not answer", status_code=503))
    with client(api=api, policy=AllowAllPolicy(), script=COVERAGE_SCRIPT[:1]) as http:
        body = turn(http, AgentRole.COVERAGE_NETWORK, workflow_state="AWAITING_DOCTOR_CHOICE")

    assert body["result"]["outcome"] == "retryable_error"
    assert body["result"]["a2a_state"] == "failed"
    assert body["result"]["is_success"] is False
    assert body["trace"]["budget"]["model_calls"] == 1, (
        "the failing turn spent a second model call; the only reason to make one here "
        "would be to reword the failure (§7)"
    )
    assert body["trace"]["budget"]["tool_calls"] == 1
    assert "tools" not in body["trace"], (
        "a failed turn is answered with the request's trace, not the handler's: there "
        "is no tool detail on this path, so a reader cannot see which call failed"
    )
    assert "refused" not in body["trace"], (
        "the turn was refused by a stage rather than failing in transport; a rewording "
        "pass lands here"
    )
    assert body["trace"]["trace_id"] == "trace-1"
    assert body["trace"]["workflow_state"] == "AWAITING_DOCTOR_CHOICE"
    assert [call[1] for call in api.calls] == ["/coverage/lookup"]


def test_a_handlerless_app_answers_every_task_with_a_terminal_failure() -> None:
    """`create_app` without a handler: up, healthy, and refusing to pretend it works."""
    with TestClient(create_app(settings())) as http:
        assert http.get(HEALTH_PATH).status_code == 200
        body = turn(http, AgentRole.COVERAGE_NETWORK, workflow_state="COVERAGE_CHECK")

    assert body["result"]["a2a_state"] == "failed"
    assert body["result"]["outcome"] == "terminal_error"


def test_no_failure_envelope_carries_the_reason_it_failed() -> None:
    """A gap, pinned three ways: the outcome reaches the caller, the reason does not.

    A policy refusal, a transport fault and a missing handler each produce a distinct,
    correct projection — and each discards its message at the HTTP boundary. A caller
    therefore cannot tell "we refused you" from "we refused you because the database
    was not configured", and nothing logs the difference either: the text lives in the
    exception for the length of one request. Asserting the absence keeps this visible;
    if a reason field is added, this test is the one that should change.
    """
    refusal = RefusingApi(UpstreamTransient("the provider registry did not answer"))
    with client(api=refusal, policy=AllowAllPolicy(), script=COVERAGE_SCRIPT[:1]) as http:
        transport_fault = json.dumps(
            turn(http, AgentRole.COVERAGE_NETWORK, workflow_state="AWAITING_DOCTOR_CHOICE")
        )
    with client(api=StubApi({"/coverage/lookup": dict(COVERAGE_BODY)}), policy=None) as http:
        refusal_body = json.dumps(
            turn(http, AgentRole.COVERAGE_NETWORK, workflow_state="COVERAGE_CHECK")
        )
    with TestClient(create_app(settings())) as http:
        unwired = json.dumps(
            turn(http, AgentRole.COVERAGE_NETWORK, workflow_state="COVERAGE_CHECK")
        )

    for body, text in (
        (transport_fault, "the provider registry did not answer"),
        (refusal_body, "no policy backend is configured"),
        (unwired, "no task handler wired"),
    ):
        assert text not in body


# ── the boot path ────────────────────────────────────────────────────────────────────────────


def test_the_asgi_factory_serves_a_role_without_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """A container with no `DATABASE_URL` still boots and reports itself."""
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with TestClient(asgi_app()) as http:
        body = http.get(HEALTH_PATH).json()
    assert body["role"] == AgentRole.COVERAGE_NETWORK.value
    assert body["service"] == service_name(AgentRole.COVERAGE_NETWORK)


def test_the_asgi_factory_takes_its_role_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One image, seven services: `AGENT_ROLE` is the only thing that decides which."""
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(AGENT_ROLE_ENV, AgentRole.SCHEDULING.value)

    with TestClient(asgi_app()) as http:
        body = http.get(HEALTH_PATH).json()
    assert body == {
        "status": "ok",
        "role": "scheduling",
        "service": service_name(AgentRole.SCHEDULING),
    }


@pytest.mark.parametrize(
    ("missing", "expected"),
    [
        ("A2A_SERVICE_TOKEN", "A2A_SERVICE_TOKEN is not set"),
        ("AGENT_MODEL_ID", "AGENT_MODEL_ID is not set"),
        ("OPENROUTER_API_KEY", "OPENROUTER_API_KEY is not set"),
    ],
)
def test_the_boot_path_requires_more_than_the_offline_profile_needs(
    monkeypatch: pytest.MonkeyPatch, missing: str, expected: str
) -> None:
    """A gap, pinned: `MODEL_BACKEND=offline` does not relax the provider variables.

    A deployment that never reaches a provider still has to name a model id and a
    non-empty key, because the model reference is resolved before any backend is
    consulted — the key is then only a label. Asserted rather than worked around: a
    silent default here would let a container boot into a configuration nobody chose.
    """
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(missing, raising=False)

    with pytest.raises(ConfigurationError) as raised:
        asgi_app()
    assert expected in str(raised.value)
