"""Langfuse tracing (ADR-0042): off unless configured, masked on the way out, readable when it lands.

Three properties are held here without a network:

* **Off by default, and offline means off.** No keys, no base URL, ``LANGFUSE_TRACING_ENABLED=false``
  or ``MODEL_BACKEND=offline`` each leave the runtime with no Langfuse client at all.
* **The Langfuse copy is masked before it is reshaped.** The repo's ``MaskingPolicy`` runs on the
  raw attributes, so an e-mail inside a prompt is scrubbed before it becomes a chat message, and a
  ``user.id`` that is really a workflow id never reaches Langfuse.
* **A generation reads as a conversation.** OpenInference's flattened message attributes become the
  OpenAI-format messages Langfuse renders, tool calls and tool results included.

The span *tree* of a real ADK turn is held in ``tests/agents/test_llm_tracing_span_tree.py``, where
the agents' fixtures are.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from referraleasy_agent_runtime import llm_tracing
from referraleasy_agent_runtime.llm_tracing import (
    LlmTracing,
    _langfuse_attributes,
    _messages,
    active_llm_tracing,
    configure_llm_tracing,
)
from referraleasy_contracts import AgentTaskRequest
from referraleasy_telemetry import DEFAULT_POLICY, REDACTED, TelemetryConfig, configure_telemetry

pytestmark = pytest.mark.contract

CONFIGURED = {
    "LANGFUSE_PUBLIC_KEY": "pk-lf-test",
    "LANGFUSE_SECRET_KEY": "sk-lf-test",
    "LANGFUSE_BASE_URL": "http://langfuse.invalid",
    "MODEL_BACKEND": "live",
    "APP_ENV": "demo",
}


def _telemetry() -> Any:
    return configure_telemetry(
        "agent-test", config=TelemetryConfig(service_name="agent-test"), set_global=False
    )


# ── off unless configured ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"MODEL_BACKEND": "offline"}, "MODEL_BACKEND=offline"),
        ({"LANGFUSE_TRACING_ENABLED": "false"}, "LANGFUSE_TRACING_ENABLED=false"),
        ({"LANGFUSE_SECRET_KEY": ""}, "LANGFUSE_SECRET_KEY not set"),
        ({"LANGFUSE_BASE_URL": " "}, "LANGFUSE_BASE_URL not set"),
    ],
)
def test_tracing_stays_off_unless_every_condition_holds(
    override: dict[str, str], reason: str
) -> None:
    tracing = configure_llm_tracing(_telemetry(), release="0.0.0", env={**CONFIGURED, **override})

    assert not tracing.active
    assert tracing.disabled_reason is not None and reason in tracing.disabled_reason


def test_offline_wins_even_with_keys_present() -> None:
    """The offline profile is zero network calls; keys in the environment do not change that."""
    env = {**CONFIGURED, "MODEL_BACKEND": "OFFLINE"}
    assert not configure_llm_tracing(_telemetry(), release="0.0.0", env=env).active


def test_a_disabled_turn_and_tool_are_no_ops() -> None:
    request = _request()
    with LlmTracing().turn("provider_matching", request, {}) as observation:
        observation.finish(output={"x": 1}, outcome="completed", error="ignored")
    with active_llm_tracing().tool("search_directory", {"q": "x"}) as tool:
        tool.finish(output=None, outcome="ok")


# ── masked, then reshaped ────────────────────────────────────────────────────────────────────


def _llm_span(**extra: Any) -> dict[str, Any]:
    return {
        "openinference.span.kind": "LLM",
        "llm.model_name": "openrouter/anthropic/claude-haiku-5.5",
        "input.value": '{"model": "raw ADK request"}',
        "output.value": '{"content": "raw ADK response"}',
        "gcp.vertex.agent.llm_request": '{"model": "raw ADK request"}',
        "gcp.vertex.agent.llm_response": '{"content": "raw ADK response"}',
        "llm.input_messages.0.message.role": "system",
        "llm.input_messages.0.message.content": "You are Provider Matching.",
        "llm.input_messages.1.message.role": "user",
        "llm.input_messages.1.message.contents.0.message_content.type": "text",
        "llm.input_messages.1.message.contents.0.message_content.text": "Find a cardiologist.",
        "llm.input_messages.2.message.role": "model",
        "llm.input_messages.2.message.tool_calls.0.tool_call.id": "call-1",
        "llm.input_messages.2.message.tool_calls.0.tool_call.function.name": "search_directory",
        "llm.input_messages.2.message.tool_calls.0.tool_call.function.arguments": '{"q": "cardio"}',
        "llm.input_messages.3.message.role": "tool",
        "llm.input_messages.3.message.name": "search_directory",
        "llm.input_messages.3.message.tool_call_id": "call-1",
        "llm.input_messages.3.message.content": '{"candidates": []}',
        "llm.output_messages.0.message.role": "model",
        "llm.output_messages.0.message.contents.0.message_content.type": "reasoning",
        "llm.output_messages.0.message.contents.0.message_content.text": "No candidates; widen.",
        "llm.output_messages.0.message.contents.1.message_content.type": "text",
        "llm.output_messages.0.message.contents.1.message_content.text": '{"candidates": []}',
        "session.id": "wf-1",
        "user.id": "wf-1",
        **extra,
    }


def test_flattened_messages_become_an_openai_conversation() -> None:
    messages = _messages(_llm_span(), "input")

    assert messages == [
        {"role": "system", "content": "You are Provider Matching."},
        {"role": "user", "content": "Find a cardiologist."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "search_directory", "arguments": '{"q": "cardio"}'},
                }
            ],
        },
        {
            "role": "tool",
            "content": '{"candidates": []}',
            "name": "search_directory",
            "tool_call_id": "call-1",
        },
    ]


def test_a_generation_carries_one_copy_of_its_messages() -> None:
    patched = _langfuse_attributes(_llm_span(), DEFAULT_POLICY)

    assert json.loads(patched["langfuse.observation.input"])[1]["content"] == "Find a cardiologist."
    assert json.loads(patched["langfuse.observation.output"]) == {
        "role": "assistant",
        "content": '{"candidates": []}',
        "reasoning": "No candidates; widen.",
    }
    for superseded in (
        "input.value",
        "output.value",
        "gcp.vertex.agent.llm_request",
        "gcp.vertex.agent.llm_response",
        "llm.input_messages.0.message.content",
        "llm.output_messages.0.message.role",
    ):
        assert superseded not in patched
    # What Langfuse needs for cost and filtering is untouched.
    assert patched["llm.model_name"] == "openrouter/anthropic/claude-haiku-5.5"
    assert patched["session.id"] == "wf-1"


def test_masking_runs_before_the_conversion() -> None:
    span = _llm_span(
        **{"llm.input_messages.1.message.contents.0.message_content.text": "Mail jo@example.com"}
    )

    patched = _langfuse_attributes(span, DEFAULT_POLICY)

    user = json.loads(patched["langfuse.observation.input"])[1]
    assert "jo@example.com" not in user["content"] and REDACTED in user["content"]


def test_the_workflow_is_not_reported_as_a_user() -> None:
    assert "user.id" not in _langfuse_attributes(_llm_span(), DEFAULT_POLICY)
    assert "user.id" not in _langfuse_attributes({"user.id": "wf-1"}, DEFAULT_POLICY)


def test_named_identifiers_are_pseudonymised_on_any_span() -> None:
    patched = _langfuse_attributes({"patient.identifier": "MRN-1"}, DEFAULT_POLICY)

    assert "patient.identifier" not in patched
    assert len(patched["patient.identifier_hash"]) == 64


def test_a_failed_generation_keeps_its_raw_output() -> None:
    """With no output messages (the call raised), ``output.value`` is the only record of why."""
    span = {key: value for key, value in _llm_span().items() if "output_messages" not in key}

    patched = _langfuse_attributes(span, DEFAULT_POLICY)

    assert "langfuse.observation.output" not in patched
    assert patched["output.value"] == '{"content": "raw ADK response"}'


# ── the turn and tool observations ───────────────────────────────────────────────────────────


@pytest.fixture
def exported() -> Iterator[tuple[LlmTracing, InMemorySpanExporter]]:
    """A real Langfuse client over an in-memory exporter: the spans it would send, not sent.

    The public key is unique per test because Langfuse keeps one client per key and ignores the
    arguments of a second ``Langfuse(...)``: a reused key would hand this test the previous test's
    exporter, already shut down, and ``flush`` would block on it.
    """
    from uuid import uuid4

    from langfuse import Langfuse

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    client = Langfuse(
        public_key=f"pk-lf-unit-{uuid4()}",
        secret_key="sk-lf-unit",
        base_url="http://langfuse.invalid",
        tracer_provider=provider,
        span_exporter=exporter,
        environment="test",
    )
    yield LlmTracing(client=client), exporter
    client.shutdown()


def _request() -> AgentTaskRequest:
    return AgentTaskRequest(
        workflow_id="wf-1",
        session_id="wf-1:cmd-1",
        workflow_state="PROVIDER_MATCHING",
        task="Find providers.",
        idempotency_key="idem-1",
        trace_id="",
        payload={"command_id": "cmd-1", "task_envelope": {"depth": 0}, "specialty": "cardiology"},
    )


def test_a_turn_is_one_agent_observation_in_the_workflow_session(
    exported: tuple[LlmTracing, InMemorySpanExporter],
) -> None:
    tracing, exporter = exported
    request = _request()

    with tracing.turn("provider_matching", request, request.payload) as observation:
        observation.finish(output={"candidates": []}, outcome="no_match")
    tracing.client.flush()  # type: ignore[union-attr]

    (span,) = exporter.get_finished_spans()
    attributes: dict[str, Any] = dict(span.attributes or {})
    assert span.name == "run-provider-matching"
    assert attributes["langfuse.observation.type"] == "agent"
    assert attributes["session.id"] == "wf-1"
    assert list(attributes["langfuse.trace.tags"]) == ["provider_matching"]
    # The orchestration keys are metadata, not what the role was asked.
    assert json.loads(attributes["langfuse.observation.input"]) == {"specialty": "cardiology"}
    assert attributes["langfuse.trace.metadata.command_id"] == "cmd-1"
    assert "langfuse.observation.level" not in attributes


def test_a_failed_turn_is_filterable_as_one(
    exported: tuple[LlmTracing, InMemorySpanExporter],
) -> None:
    tracing, exporter = exported
    request = _request()

    with tracing.turn("provider_matching", request, request.payload) as observation:
        observation.finish(output=None, outcome="terminal_error", error="output: bad JSON")
    tracing.client.flush()  # type: ignore[union-attr]

    (span,) = exporter.get_finished_spans()
    attributes: dict[str, Any] = dict(span.attributes or {})
    assert attributes["langfuse.observation.level"] == "ERROR"
    assert attributes["langfuse.observation.status_message"] == "output: bad JSON"


def test_a_workflow_tool_call_is_a_tool_observation_under_the_turn(
    exported: tuple[LlmTracing, InMemorySpanExporter],
) -> None:
    tracing, exporter = exported
    request = _request()

    with tracing.turn("coverage_network", request, request.payload) as observation:
        with tracing.tool("check_plan", {"plan_id": "p-1"}) as tool:
            tool.finish(output={"plan": "PPO"}, outcome="ok")
        observation.finish(output={}, outcome="completed")
    tracing.client.flush()  # type: ignore[union-attr]

    tool_span, turn_span = exporter.get_finished_spans()
    assert tool_span.name == "check_plan"
    assert dict(tool_span.attributes or {})["langfuse.observation.type"] == "tool"
    assert tool_span.parent is not None
    assert turn_span.context is not None
    assert tool_span.parent.span_id == turn_span.context.span_id


def test_a_delegated_turn_hangs_under_the_callers_span_not_the_server_span(
    exported: tuple[LlmTracing, InMemorySpanExporter],
) -> None:
    """The A2A server span is filtered out of Langfuse, so the turn is parented past it."""
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    tracing, exporter = exported
    caller = SpanContext(
        trace_id=0x0AF7651916CD43DD8448EB211C80319C,
        span_id=0x00F067AA0BA902B7,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    inbound = trace.set_span_in_context(NonRecordingSpan(caller))
    server = TracerProvider().get_tracer("server")

    with (
        server.start_as_current_span("POST /tasks", context=inbound),
        tracing.turn("provider_matching", _request(), {}) as observation,
    ):
        observation.finish(output={}, outcome="completed")
    tracing.client.flush()  # type: ignore[union-attr]

    (span,) = exporter.get_finished_spans()
    assert span.parent is not None and span.context is not None
    assert span.parent.span_id == caller.span_id
    assert span.context.trace_id == caller.trace_id


def test_active_llm_tracing_is_disabled_until_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_tracing, "_active", None)
    assert not active_llm_tracing().active
