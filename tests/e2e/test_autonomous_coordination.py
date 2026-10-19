"""Patient commands drive real agent services and the isolated mock hospital API.

The test supplies patient choices. It supplies no specialist result or transition.
"""

from __future__ import annotations

import os
import time
from contextlib import ExitStack
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from referraleasy_agent_runtime import AgentRole, AgentSettings, BudgetCaps
from referraleasy_agents.service import HttpDelegation, build_app, build_policy
from referraleasy_agents.tools.client import DirectoryApi
from referraleasy_contracts.generated.common import (
    GuardrailCategory,
    GuardrailDecision,
    GuardrailLayer,
)
from referraleasy_gateway.app import create_app
from referraleasy_gateway.config import GatewaySettings
from referraleasy_gateway.dispatch import CoordinatorDispatch
from referraleasy_gateway.guardrails import (
    DecisionModelGuardrail,
    GuardrailPipeline,
    LayerVerdict,
    LocalClassifierGuardrail,
    RegexGuardrail,
)
from referraleasy_gateway.policy import TablePolicy
from referraleasy_gateway.timing import SystemClock

pytestmark = pytest.mark.e2e
PATIENT = "b0000000-0000-4000-8000-000000000001"


class SourcePolicy:
    def policy_assert(self, role: str, tool: str, workflow_state: str) -> None:
        TablePolicy.from_seed().assert_allowed(f"{role}:{tool}", workflow_state)


def _guardrails() -> GuardrailPipeline:
    def allow(layer: GuardrailLayer) -> Any:
        def screen(*args: Any) -> LayerVerdict:
            return LayerVerdict(
                layer=layer,
                decision=GuardrailDecision.allow,
                category=GuardrailCategory.benign,
                reason="Test classification.",
            )

        return screen

    return GuardrailPipeline(
        layers=(
            RegexGuardrail(max_chars=2000),
            DecisionModelGuardrail("test", screen=allow(GuardrailLayer.decision_model)),
            LocalClassifierGuardrail("test", screen=allow(GuardrailLayer.local_classifier)),
        ),
        clock=SystemClock(),
    )


@pytest.mark.parametrize(
    "hospital_tail,expected,fault",
    [
        ("001", "BOOKED", None),
        ("002", "BOOKING_UNVERIFIED", None),
        ("001", "BOOKING_UNVERIFIED", "timeout_after_commit"),
        ("001", "BOOKING_UNVERIFIED", "timeout_before_commit"),
        ("001", "HUMAN_HANDOFF", "halt"),
        ("001", "HUMAN_HANDOFF", "missing_document"),
        ("001", "HUMAN_HANDOFF", "three_transient_errors"),
        ("001", "BOOKED", "restart"),
    ],
)
def test_patient_choices_complete_the_autonomous_journey(
    monkeypatch: Any, hospital_tail: str, expected: str, fault: str | None
) -> None:
    import referraleasy_agent_runtime.app as runtime_module
    import referraleasy_gateway.app as gateway_module
    from referraleasy_gateway.coordination import CoordinationService

    workers = []

    def worker_factory(**kwargs: Any) -> CoordinationService:
        worker = CoordinationService(**kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(gateway_module, "CoordinationService", worker_factory)

    failures: list[str] = []
    original_projection = runtime_module.project_failure

    def record_failure(error: Exception) -> Any:
        failures.append(f"{type(error).__name__}: {error}")
        return original_projection(error)

    monkeypatch.setattr(runtime_module, "project_failure", record_failure)

    apps: dict[str, Any] = {}
    hospital_api = httpx.Client(base_url=os.environ["MOCK_API_URL"], timeout=3)
    hospital_api.post("/_faults/arm", json={"mode": "off"}).raise_for_status()
    hospital_api.post("/_faults/reset", json={}).raise_for_status()

    async def router(scope: Any, receive: Any, send: Any) -> None:
        host = dict(scope["headers"])[b"host"].decode().split(":")[0]
        await apps[host](scope, receive, send)

    peer_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=router))
    delegation = HttpDelegation(
        base_url="http://{role}", token="test-service-token", caps=BudgetCaps(), client=peer_client
    )
    for role in AgentRole:
        env = {
            "APP_ENV": "test",
            "MODEL_BACKEND": "offline",
            "AGENT_ROLE": role.value,
            "AGENT_MODEL_PROVIDER": "openrouter",
            "AGENT_MODEL_ID": "test/unused",
            "OPENROUTER_API_KEY": "test-unused",
            "A2A_SERVICE_TOKEN": "test-service-token",
        }
        apps[role.value] = build_app(
            AgentSettings.from_env(env),
            env=env,
            policy=(
                build_policy(env={"DATABASE_URL": os.environ["TEST_DATABASE_URL"]})
                if os.environ.get("TEST_DATABASE_URL")
                else SourcePolicy()
            ),
            delegation=delegation if role is AgentRole.CARE_COORDINATOR else None,
            api=DirectoryApi(
                os.environ["MOCK_API_URL"], token="test-service-token", caps=BudgetCaps()
            ),
        )
    with TestClient(apps[AgentRole.CARE_COORDINATOR.value]) as coordinator_client:

        def dispatcher(settings: Any, **kwargs: Any) -> CoordinatorDispatch:
            return CoordinatorDispatch(
                base_url="http://testserver",
                token="test-service-token",
                release_manifest_id="rel-20261016-8e00275",
                log=kwargs["log"],
                clock=kwargs["clock"],
                client=coordinator_client,
            )

        monkeypatch.setattr(gateway_module, "_dispatcher", dispatcher)
        settings = GatewaySettings.from_env(
            {
                "APP_ENV": "test",
                "JWT_PATIENT_SECRET": "fixture-patient-secret-0123456789abcdef",
                "JWT_ADMIN_SECRET": "fixture-admin-secret-0123456789abcdef",
                "GATEWAY_STREAM_HOLD_MS": "0",
                **(
                    {
                        "DATABASE_URL": os.environ["TEST_DATABASE_URL"],
                        "GATEWAY_CONTROL_BACKEND": "postgres",
                    }
                    if os.environ.get("TEST_DATABASE_URL")
                    else {}
                ),
            }
        )
        with (
            TestClient(create_app(settings=settings, guardrails=_guardrails())) as gateway,
            ExitStack() as cleanup,
        ):
            admin = gateway.post("/v1/auth/admin-session", json={"subject": "test-admin"}).json()
            ah = {"Authorization": f"Bearer {admin['access_token']}"}
            patient = gateway.post(
                "/v1/auth/patient-session", json={"subject": PATIENT, "patient_ref": PATIENT}
            ).json()
            ph = {"Authorization": f"Bearer {patient['access_token']}"}
            from referraleasy_gateway.referral_sources import demo_sources

            sources = demo_sources(PATIENT)
            if fault == "missing_document":
                sources["referral"]["document_ids"] = []
            seeded = gateway.post(
                "/v1/demo/workflows",
                headers=ah,
                json={
                    "patient_ref": PATIENT,
                    "ticket_ref": f"AUTO-{uuid4()}",
                    "payload": {"source_data": sources},
                },
            )
            assert seeded.status_code == 200, seeded.text
            workflow_id = seeded.json()["workflow_id"]
            path = f"/v1/workflows/{workflow_id}"

            def wait(state: str) -> dict[str, Any]:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    response = gateway.get(path, headers=ph)
                    assert response.status_code == 200, response.text
                    snapshot = response.json()
                    if (
                        snapshot["state"] == state
                        and snapshot["components"]
                        and all(
                            c["workflow_state_version"] == snapshot["state_version"]
                            for c in snapshot["components"]
                        )
                    ):
                        return snapshot
                    if snapshot["state"] in {"RECOVERY", "HUMAN_HANDOFF"}:
                        detail = gateway.get(f"/v1/admin/workflows/{workflow_id}", headers=ah)
                        pytest.fail(
                            f"Unexpected recovery: {detail.json()['workflow']['payload'].get('coordination_error')}. Agent failures: {failures}"
                        )
                    time.sleep(0.05)
                pytest.fail(f"Expected {state}, received {snapshot}")

            def choose(snapshot: dict[str, Any], selected: str | None = None) -> httpx.Response:
                card = snapshot["components"][0]
                # Use the browser payload shape, including nullable context IDs.
                value = {"consented": True}
                if selected:
                    key = {
                        "AWAITING_HOSPITAL_CHOICE": "hospital_id",
                        "AWAITING_DOCTOR_CHOICE": "provider_id",
                        "AWAITING_SLOT_CHOICE": "slot_id",
                    }[snapshot["state"]]
                    value = {"hospital_id": None, "provider_id": None, key: selected}
                command_body = {
                    "command_id": str(uuid4()),
                    "workflow_state_version": snapshot["state_version"],
                    "instance_id": card["instance_id"],
                    "action": card["action"],
                    "value": value,
                }
                response = gateway.post(
                    f"{path}/actions",
                    headers=ph,
                    json=command_body,
                )
                assert response.status_code == 200, response.text
                replay = gateway.post(f"{path}/actions", headers=ph, json=command_body)
                assert replay.status_code == 200, replay.text
                assert replay.json()["replayed"] is True
                return response

            message = gateway.post(
                f"{path}/messages",
                headers=ph,
                json={
                    "command_id": str(uuid4()),
                    "workflow_state_version": 1,
                    "text": "Schedule a cardiology consultation.",
                },
            )
            assert message.status_code == 202, message.text
            hospital = wait("AWAITING_HOSPITAL_CHOICE")
            target = next(
                o["id"]
                for o in hospital["components"][0]["props"]["options"]
                if o["id"].endswith(hospital_tail)
            )
            if fault == "restart":
                workers[0].close()
            choose(hospital, target)
            if fault == "restart":
                previous = workers[0]
                assert str(previous.continuations.pending()[0]) == workflow_id
                replacement = CoordinationService(
                    control=previous.control,
                    commands=previous.commands,
                    coordinator=previous.coordinator,
                    clock=previous.clock,
                )
                replacement.start()
                cleanup.callback(replacement.close)
            doctors = wait("AWAITING_DOCTOR_CHOICE")
            if fault == "three_transient_errors":
                hospital_api.post("/_faults/arm", json={"scenario_id": fault}).raise_for_status()
            choose(doctors, doctors["components"][0]["props"]["options"][0]["id"])
            if fault in {"missing_document", "three_transient_errors"}:
                handoff = wait("HUMAN_HANDOFF")
                assert handoff["components"][0]["props"]["consent_required"] is True
                choose(handoff)
                receipt = gateway.get(path, headers=ph).json()["components"]
                assert len(receipt) == 1
                assert receipt[0]["props"]["consent_required"] is False
                assert receipt[0]["action"] is None
                record = gateway.get(f"/v1/admin/workflows/{workflow_id}", headers=ah).json()
                assert record["workflow"]["payload"]["handoff"]["status"] == "queued"
                assert record["bookings"] == []
                hospital_api.post("/_faults/arm", json={"mode": "off"})
                return
            slots = wait("AWAITING_SLOT_CHOICE")
            choose(slots, slots["components"][0]["props"]["options"][0]["id"])
            review = wait("AWAITING_FINAL_APPROVAL")
            assert (
                gateway.get(f"/v1/admin/workflows/{workflow_id}", headers=ah).json()["bookings"]
                == []
            )
            if fault in {"timeout_after_commit", "timeout_before_commit"}:
                hospital_api.post("/_faults/arm", json={"scenario_id": fault}).raise_for_status()
            if fault == "halt":
                stopped = gateway.post(
                    "/v1/admin/control/booking-writes",
                    headers=ah,
                    json={"engage": True, "reason": "Test booking pause.", "by": "test-admin"},
                )
                assert stopped.status_code == 200, stopped.text
            choose(review)
            result = wait(expected)
            assert result["components"]
            record = gateway.get(f"/v1/admin/workflows/{workflow_id}", headers=ah).json()
            assert len(record["bookings"]) == (0 if fault == "halt" else 1)
            if fault in {"timeout_after_commit", "timeout_before_commit"}:
                hospital_api.post("/_faults/arm", json={"mode": "off"}).raise_for_status()
                choose(result)
                resolved = wait("BOOKED" if fault == "timeout_after_commit" else "HUMAN_HANDOFF")
                assert resolved["components"]
                after = gateway.get(f"/v1/admin/workflows/{workflow_id}", headers=ah).json()
                assert len(after["bookings"]) == 1
