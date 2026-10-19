"""The HTTP surface: auth at the transport, one budget per request, outcomes over status codes.

Two decisions are being defended. First, **failure is not an HTTP status**: a turn
that ends in ``outcome_unknown`` is a 200 whose task state is ``failed`` with the
application outcome beside it, because an A2A caller that read only the status code
would learn nothing. Second, **the boot path is inspectable**: ``--check`` prints the
deployment it would serve without opening a socket or importing a model SDK, so a
container's configuration can be verified before it takes traffic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from agent_runtime_doubles import SPEC_ENV
from fastapi.testclient import TestClient

from referraleasy_agent_runtime.app import (
    HEALTH_PATH,
    MANIFEST_PATH,
    TASKS_PATH,
    AgentTaskRequest,
    TurnReport,
    asgi_app,
    create_app,
    main,
    preflight,
)
from referraleasy_agent_runtime.auth import AUTH_TOKEN_ENV
from referraleasy_agent_runtime.budget import BudgetCaps, BudgetExhausted, CapKind, TurnBudget
from referraleasy_agent_runtime.errors import ConfigurationError
from referraleasy_agent_runtime.roles import AgentRole, allowed_tools
from referraleasy_agent_runtime.settings import AgentSettings, resolve_port

TOKEN = SPEC_ENV[AUTH_TOKEN_ENV]
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ALL_ROLES = tuple(AgentRole)

TASK_BODY = {
    "workflow_id": "wf-1",
    "session_id": "sess-1",
    "workflow_state": "AWAITING_SLOT_CHOICE",
    "task": "Confirm the booking.",
    "idempotency_key": "wf-1:confirm",
    "trace_id": "trace-1",
}


def _settings(role: AgentRole = AgentRole.SCHEDULING) -> AgentSettings:
    return AgentSettings.from_env(SPEC_ENV, role=role)


class _EchoHandler:
    """A handler that answers: the smallest thing that exercises the whole path."""

    def __init__(self, *, result: object | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self._result = result

    async def __call__(self, request: AgentTaskRequest, budget: TurnBudget) -> TurnReport:
        from referraleasy_agent_runtime import results

        self.calls.append((request.workflow_id, budget.remaining_ms()))
        return TurnReport(
            result=self._result or results.succeeded(),  # type: ignore[arg-type]
            skills=("booking-confirmation",),
            trace={"handler": "echo"},
        )


def _client(handler: object | None = None, role: AgentRole = AgentRole.SCHEDULING) -> TestClient:
    return TestClient(create_app(_settings(role), handler=handler))  # type: ignore[arg-type]


def test_health_is_open_and_does_not_leak_the_role_boundary() -> None:
    """A probe carries no token, so the liveness route must not demand one."""
    with _client() as client:
        response = client.get(HEALTH_PATH)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "role": "scheduling", "service": "agent-scheduling"}


def test_a_task_without_a_token_is_rejected() -> None:
    with _client(_EchoHandler()) as client:
        response = client.post(TASKS_PATH, json=TASK_BODY)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


def test_a_task_with_the_wrong_token_is_rejected() -> None:
    with _client(_EchoHandler()) as client:
        response = client.post(
            TASKS_PATH, json=TASK_BODY, headers={"Authorization": "Bearer not-the-token"}
        )

    assert response.status_code == 401


def test_the_manifest_route_is_guarded_like_the_task_route() -> None:
    with _client() as client:
        assert client.get(MANIFEST_PATH).status_code == 401
        response = client.get(MANIFEST_PATH, headers=AUTH)

    body = response.json()
    assert response.status_code == 200
    assert body["component"] == "agent-scheduling"
    assert body["model"]["model_id"] == "anthropic/claude-haiku-5.5"
    assert body["model"]["overridden"] is False
    assert body["budget"]["max_attempts"] == 3
    assert body["budget"]["transport_retries"] == 0
    assert body["budget"]["retry_owner"] == "agent-runtime"
    assert TOKEN not in response.text


def test_a_task_returns_the_result_beside_its_protocol_state() -> None:
    handler = _EchoHandler()
    with _client(handler) as client:
        response = client.post(TASKS_PATH, json=TASK_BODY, headers=AUTH)

    assert response.status_code == 200, "a shaped failure is still a 200; A2A reads the task state"
    body = response.json()

    assert body["result"]["a2a_state"] == "completed"
    assert body["result"]["outcome"] == "completed"
    assert body["result"]["is_success"] is True
    assert body["result"]["skills"] == ["booking-confirmation"]
    assert handler.calls and handler.calls[0][0] == "wf-1"


def test_the_trace_carries_the_role_the_caller_and_the_budget() -> None:
    with _client(_EchoHandler()) as client:
        body = client.post(TASKS_PATH, json=TASK_BODY, headers=AUTH).json()

    trace = body["trace"]
    assert trace["trace_id"] == "trace-1"
    assert trace["role"] == "scheduling"
    assert trace["skills"] == ["booking-confirmation"]
    assert trace["handler"] == "echo"

    budget = trace["budget"]
    assert budget["role"] == "scheduling"
    # A band, not equality. `remaining_ms` is `deadline - now`, so `== 30_000` holds only if this
    # snapshot lands in the same millisecond the turn was constructed — true in isolation, and it
    # failed in the full suite where 2 500 tests have the CPU. Unlike `test_suspend`, this path
    # offers no clock to pin (`create_app` takes no clock), so the band is the honest fix: it keeps
    # what the assertion is *for* — a fresh top-level turn starts with the full deadline rather than
    # a shrunk or shared one — while being immune to scheduling jitter. A real regression here (a
    # 3 s attempt budget leaking into `remaining_ms`, or a budget shared between turns) moves the
    # value far outside the band.
    assert 29_000 <= budget["remaining_ms"] <= 30_000, budget["remaining_ms"]
    assert budget["model_calls"] == 0
    assert budget["tool_calls"] == 0
    assert budget["delegations"] == 0
    assert budget["depth"] == 0, "a top-level turn is not a delegation"


def test_every_request_gets_its_own_budget() -> None:
    """Two tasks must not share one turn's deadline."""
    handler = _EchoHandler()
    with _client(handler) as client:
        client.post(TASKS_PATH, json=TASK_BODY, headers=AUTH)
        client.post(TASKS_PATH, json=TASK_BODY, headers=AUTH)

    # A tolerance rather than an equality, and the claim being asserted is the one in the name:
    # a shared deadline is what this test exists to catch, and it would show as the second request
    # reading far less than the first — not as a millisecond of drift. `remaining_ms()` is a live
    # reading against a deadline, so the exact value depends on how long the first request took, and
    # an exact assertion held only on an idle machine (it failed in a full-suite run, under load,
    # with 29_999).
    remainings = [remaining for _, remaining in handler.calls]
    assert len(remainings) == 2
    assert all(remaining > 29_000 for remaining in remainings), remainings
    assert abs(remainings[0] - remainings[1]) < 1_000, (
        f"the two requests did not get comparable budgets: {remainings}. A shared deadline would "
        f"leave the second with whatever the first had left."
    )


def test_a_budget_exhausted_after_a_write_reports_an_unknown_outcome_over_http() -> None:
    """The trap, end to end: a 200 whose state is `failed` while the booking is unresolved."""

    class _Exhausted:
        async def __call__(self, request: AgentTaskRequest, budget: TurnBudget) -> TurnReport:
            raise BudgetExhausted(
                CapKind.ATTEMPTS,
                limit=budget.caps.max_attempts,
                used=budget.caps.max_attempts,
                agent_role=budget.role,
                site="create_booking",
                after_write_attempt=True,
            )

    with _client(_Exhausted()) as client:
        body = client.post(TASKS_PATH, json=TASK_BODY, headers=AUTH).json()

    result = body["result"]
    assert result["a2a_state"] == "failed"
    assert result["protocol_is_terminal"] is True
    assert result["outcome"] == "outcome_unknown", "the outcome must survive beside the state"
    assert result["requires_reconciliation"] is True
    assert "reconcile" in result["reconciliation_hint"].lower()


def test_a_budget_exhausted_before_any_write_is_merely_retryable() -> None:
    class _Exhausted:
        async def __call__(self, request: AgentTaskRequest, budget: TurnBudget) -> TurnReport:
            raise BudgetExhausted(
                CapKind.DEADLINE,
                limit=budget.caps.turn_deadline_ms,
                used=budget.caps.turn_deadline_ms,
                agent_role=budget.role,
                site="model:plan",
            )

    with _client(_Exhausted()) as client:
        result = client.post(TASKS_PATH, json=TASK_BODY, headers=AUTH).json()["result"]

    assert result["outcome"] == "retryable_error"
    assert result["requires_reconciliation"] is False
    assert "reconciliation_hint" not in result


def test_an_unclassified_crash_is_terminal_and_not_a_traceback() -> None:
    class _Broken:
        async def __call__(self, request: AgentTaskRequest, budget: TurnBudget) -> TurnReport:
            raise RuntimeError("a bug nobody planned for")

    with _client(_Broken()) as client:
        response = client.post(TASKS_PATH, json=TASK_BODY, headers=AUTH)

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["outcome"] == "terminal_error"
    assert "a bug nobody planned for" not in response.text, "an internal message is not a response"


def test_a_payload_identity_claim_is_dropped_and_recorded() -> None:
    with _client(_EchoHandler()) as client:
        body = client.post(
            TASKS_PATH,
            json={**TASK_BODY, "payload": {"user_id": "attacker", "note": "keep me"}},
            headers=AUTH,
        ).json()

    assert body["trace"]["dropped_untrusted_identity"] == ["user_id"]
    assert "attacker" not in json.dumps(body["trace"])


def test_a_payload_without_identity_claims_records_no_drop() -> None:
    with _client(_EchoHandler()) as client:
        body = client.post(
            TASKS_PATH, json={**TASK_BODY, "payload": {"note": "ok"}}, headers=AUTH
        ).json()

    assert "dropped_untrusted_identity" not in body["trace"]


def test_a_payload_may_not_smuggle_a_field_the_schema_does_not_have() -> None:
    with _client(_EchoHandler()) as client:
        response = client.post(TASKS_PATH, json={**TASK_BODY, "is_admin": True}, headers=AUTH)

    assert response.status_code == 422


def test_an_unwired_handler_refuses_the_task_rather_than_inventing_a_result() -> None:
    """A container that is up but not wired is easier to spot than one that will not boot."""
    with _client() as client:
        health = client.get(HEALTH_PATH)
        manifest = client.get(MANIFEST_PATH, headers=AUTH)
        response = client.post(TASKS_PATH, json=TASK_BODY, headers=AUTH)

    assert health.status_code == 200, "liveness must still report the container is up"
    assert manifest.status_code == 200, "and it must still say which agent it is"

    result = response.json()["result"]
    assert response.status_code == 200
    assert result["outcome"] == "terminal_error"
    assert result["a2a_state"] == "failed"
    assert result["is_success"] is False
    # The reason is deliberately not echoed: the runtime never puts an exception's
    # text into a response body. `--check` and /manifest are how an operator sees why.


def test_the_runtime_never_echoes_an_exception_message_into_a_response() -> None:
    """Same rule for every failure path, so a fix in one place cannot miss a second."""

    class _Leaky:
        async def __call__(self, request: AgentTaskRequest, budget: TurnBudget) -> TurnReport:
            raise RuntimeError("connection string postgres://user:pw@host/db")

    with _client(_Leaky()) as client:
        response = client.post(TASKS_PATH, json=TASK_BODY, headers=AUTH)

    assert "postgres://" not in response.text
    assert "pw" not in response.text


def test_a_suspending_handler_surfaces_the_input_required_state() -> None:
    from referraleasy_agent_runtime.suspend import InMemorySuspensionStore, SuspensionReason

    store = InMemorySuspensionStore()

    class _Suspender:
        async def __call__(self, request: AgentTaskRequest, budget: TurnBudget) -> TurnReport:
            from referraleasy_agent_runtime.suspend import suspend_turn

            suspension, result = suspend_turn(
                store=store,
                role=AgentRole.SCHEDULING,
                reason=SuspensionReason.PATIENT_CHOICE,
                question="Which hospital?",
                workflow_id=request.workflow_id,
                session_id=request.session_id,
                workflow_state=request.workflow_state,
                idempotency_key=request.idempotency_key,
                trace_id=request.trace_id,
            )
            return TurnReport(result=result, suspension=suspension)

    with _client(_Suspender()) as client:
        body = client.post(TASKS_PATH, json=TASK_BODY, headers=AUTH).json()

    assert body["result"]["a2a_state"] == "input-required"
    assert body["result"]["protocol_is_terminal"] is False
    assert body["result"]["suspension_id"]
    assert body["trace"]["suspension"]["reason"] == "patient_choice"


# --- startup ---------------------------------------------------------------------------------


def test_the_preflight_reports_the_deployment_without_side_effects(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        ["--check"], env={**SPEC_ENV, "AGENT_ROLE": "referral_intake", "AGENT_PORT": "9001"}
    )

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["service"] == "agent-referral_intake"
    assert report["role"] == "referral_intake"
    assert report["port"] == 9001
    assert report["manifest"]["model"]["model_id"] == "anthropic/claude-haiku-5.5"


def test_the_preflight_reads_the_port_from_the_environment_it_was_given() -> None:
    settings = AgentSettings.from_env({**SPEC_ENV, "AGENT_ROLE": "coverage_network"})

    assert preflight(settings, env={"AGENT_PORT": "7000"})["port"] == 7000
    assert preflight(settings, env={"PORT": "7001"})["port"] == 7001, "compose sets PORT"
    assert preflight(settings, env={})["port"] == 8080


def test_resolve_port_rejects_a_value_that_is_not_a_port() -> None:
    with pytest.raises(ConfigurationError, match="AGENT_PORT"):
        resolve_port({"AGENT_PORT": "http"})


def test_a_missing_role_fails_at_startup_with_a_named_variable() -> None:
    with pytest.raises(ConfigurationError, match="AGENT_ROLE"):
        main(
            ["--check"], env={key: value for key, value in SPEC_ENV.items() if key != "AGENT_ROLE"}
        )


def test_a_missing_service_token_fails_at_startup() -> None:
    env = {key: value for key, value in SPEC_ENV.items() if key != AUTH_TOKEN_ENV}

    with pytest.raises(ConfigurationError, match=AUTH_TOKEN_ENV):
        AgentSettings.from_env(env, role=AgentRole.SCHEDULING)

    with pytest.raises(ConfigurationError, match=AUTH_TOKEN_ENV):
        AgentSettings.from_env({**env, "AGENT_ROLE": "scheduling"})


def test_the_preflight_never_starts_a_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--check` is a container preflight: it must not bind a port, even by accident."""
    import uvicorn

    def _serve(*args: object, **kwargs: object) -> None:
        pytest.fail("--check must not start a server")

    monkeypatch.setattr(uvicorn, "run", _serve)

    assert main(["--check"], env={**SPEC_ENV, "AGENT_ROLE": "scheduling"}) == 0


def test_the_preflight_does_not_import_the_model_sdk() -> None:
    """`LiteLlm` is imported inside `build_model`, which nothing on the `--check` path calls.

    Asserted in a fresh interpreter: by the time this test runs, other tests have
    already built a model in this process, so `sys.modules` here proves nothing.
    """
    script = (
        "import contextlib, io, json, sys\n"
        "from referraleasy_agent_runtime.app import main\n"
        "captured = io.StringIO()\n"
        "with contextlib.redirect_stdout(captured):\n"
        "    code = main(['--check'])\n"
        "report = json.loads(captured.getvalue())\n"
        "print(json.dumps({\n"
        "    'code': code,\n"
        "    'port': report['port'],\n"
        "    'adk_imported': any(n.startswith('google.adk') for n in sys.modules),\n"
        "}))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, **SPEC_ENV, "AGENT_ROLE": "scheduling"},
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["code"] == 0
    assert report["port"] == 8080, "the preflight still resolved a real port"
    assert report["adk_imported"] is False, "the preflight pulled in the model SDK"


def test_the_asgi_factory_builds_an_app_from_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in {**SPEC_ENV, "AGENT_ROLE": "recovery_handoff"}.items():
        monkeypatch.setenv(key, value)

    app = asgi_app()

    with TestClient(app) as client:
        assert client.get(HEALTH_PATH).json()["role"] == "recovery_handoff"


@pytest.mark.parametrize("role", ALL_ROLES, ids=lambda role: role.value)
def test_one_image_serves_every_role_with_that_role_authority(role: AgentRole) -> None:
    """The role changes the name, the manifest and the allowlist — nothing else."""
    settings = AgentSettings.from_env(SPEC_ENV, role=role)

    with TestClient(create_app(settings)) as client:
        health = client.get(HEALTH_PATH).json()
        manifest = client.get(MANIFEST_PATH, headers=AUTH).json()

    assert health["role"] == role.value
    assert health["service"] == f"agent-{role.value}"
    assert manifest["component"] == f"agent-{role.value}"
    assert manifest["model"]["model_id"] == "anthropic/claude-haiku-5.5"


def test_the_caps_are_the_spec_numbers_and_are_identical_for_every_role() -> None:
    expected = BudgetCaps.from_env(SPEC_ENV)

    assert expected.attempt_timeout_ms == 3000
    assert expected.max_attempts == 3
    assert expected.turn_deadline_ms == 30_000
    assert expected.max_delegations == 8
    assert expected.max_model_calls == 12
    assert expected.max_tool_calls == 16
    assert expected.http_tool_timeout_ms == 1500
    assert expected.fallback_timeout_ms == 3000

    for role in ALL_ROLES:
        assert AgentSettings.from_env(SPEC_ENV, role=role).caps == expected


def test_the_coordinator_keeps_only_delegation_whichever_way_the_image_is_configured() -> None:
    """One image, seven roles: the coordinator's authority is not a deployment choice."""
    settings = AgentSettings.from_env(SPEC_ENV, role=AgentRole.CARE_COORDINATOR)

    assert settings.role is AgentRole.CARE_COORDINATOR
    assert allowed_tools(settings.role) == {"delegate_task"}


def test_the_service_token_is_not_reachable_by_repr_or_logging() -> None:
    settings = _settings()

    assert TOKEN not in repr(settings)
    assert TOKEN not in json.dumps(settings.manifest_entry())
    assert settings.service_token.get_secret_value() == TOKEN, "the runtime can still use it"


def test_an_unknown_role_is_refused_from_the_environment() -> None:
    with pytest.raises(ConfigurationError, match="booking_agent"):
        AgentSettings.from_env({**SPEC_ENV, "AGENT_ROLE": "booking_agent"})
