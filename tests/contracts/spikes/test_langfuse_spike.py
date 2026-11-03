"""Spike S-10: an agent turn actually reaching Langfuse - proven by readback (ADR-0042).

An OTLP export that answers 200 is acceptance, not ingestion. This spike emits one turn through the
runtime's own seam (:func:`configure_llm_tracing` and :meth:`LlmTracing.turn`, with a workflow-path
tool call under it) and then **reads the observations back** through the Langfuse public API,
checking the shape the rest of the integration relies on: the turn is an ``agent`` in the
workflow's session, and the tool is a ``tool`` under it.

Requires LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_BASE_URL. Run with ``make spike``.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
from _spike_env import require_env

pytestmark = pytest.mark.spike

ADR = "ADR-0042-langfuse-tracing-for-adk.md"


def _requirements() -> dict[str, str]:
    where = "Langfuse project -> Settings -> API Keys"
    env = {
        "LANGFUSE_PUBLIC_KEY": require_env(
            "LANGFUSE_PUBLIC_KEY", purpose="authenticates export and readback", where=where, adr=ADR
        ),
        "LANGFUSE_SECRET_KEY": require_env(
            "LANGFUSE_SECRET_KEY", purpose="authenticates export and readback", where=where, adr=ADR
        ),
        "LANGFUSE_BASE_URL": require_env(
            "LANGFUSE_BASE_URL",
            purpose="the data region or self-hosted instance traces are sent to",
            where="https://cloud.langfuse.com (EU), https://us.cloud.langfuse.com (US), or "
            "self-hosted; required explicitly so a project in one region is not written to another",
            adr=ADR,
        ),
    }
    return {**env, "MODEL_BACKEND": "live", "APP_ENV": "spike"}


def test_a_turn_is_exported_and_readable() -> None:
    from referraleasy_agent_runtime.llm_tracing import configure_llm_tracing
    from referraleasy_contracts import AgentTaskRequest
    from referraleasy_telemetry import TelemetryConfig, configure_telemetry

    env = _requirements()
    workflow_id = f"spike-s-10-{uuid.uuid4()}"
    telemetry = configure_telemetry(
        "referraleasy-spike",
        config=TelemetryConfig(service_name="referraleasy-spike"),
        set_global=False,
    )
    tracing = configure_llm_tracing(telemetry, release="spike", env=env)
    assert tracing.active and tracing.client is not None, tracing.disabled_reason
    request = AgentTaskRequest(
        workflow_id=workflow_id,
        session_id=f"{workflow_id}:cmd",
        workflow_state="PROVIDER_MATCHING",
        task="Spike S-10 readback.",
        idempotency_key=str(uuid.uuid4()),
        trace_id="",
        payload={"command_id": "cmd", "specialty": "cardiology"},
    )
    with tracing.turn("provider_matching", request, request.payload) as observation:
        with tracing.tool("search_directory", {"query": "cardiology"}) as tool:
            tool.finish(output={"candidates": []}, outcome="ok")
        observation.finish(output={"candidates": []}, outcome="no_match")
    tracing.client.flush()

    deadline = time.monotonic() + float(os.environ.get("SPIKE_LANGFUSE_READBACK_TIMEOUT_S", "90"))
    found: dict[str, object] = {}
    while time.monotonic() < deadline:
        page = tracing.client.api.observations.get_many(
            session_id=workflow_id, fields="core,basic", limit=10
        )
        found = {item.name: item for item in page.data}
        if {"run-provider-matching", "search_directory"} <= set(found):
            break
        time.sleep(3)
    else:
        pytest.fail(
            "EXPORT WAS ACCEPTED BUT THE TURN COULD NOT BE READ BACK.\n\n"
            f"Session waited for : {workflow_id}\n"
            f"Base URL           : {env['LANGFUSE_BASE_URL']}\n"
            f"Observations seen  : {sorted(found)}\n\n"
            "Check, in order: that the base URL is the region the keys belong to (a key pair "
            "only works in its own region), ingestion latency (raise "
            f"SPIKE_LANGFUSE_READBACK_TIMEOUT_S), and LANGFUSE_DEBUG=true output. See docs/adr/{ADR}.",
            pytrace=False,
        )

    turn, tool_call = found["run-provider-matching"], found["search_directory"]
    assert str(turn.type).upper().endswith("AGENT")  # type: ignore[attr-defined]
    assert str(tool_call.type).upper().endswith("TOOL")  # type: ignore[attr-defined]
    assert tool_call.parent_observation_id == turn.id  # type: ignore[attr-defined]
    print(f"\n[LANGFUSE] turn read back: trace={turn.trace_id} session={workflow_id}")  # type: ignore[attr-defined]
