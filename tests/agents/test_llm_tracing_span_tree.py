"""The span tree a real ADK turn hands Langfuse: one tree, every model call once (ADR-0042).

This is the regression the ADK/OpenInference pin needs. ``openinference-instrumentation-google-adk
==1.0.4`` suppresses ADK's duplicate spans only when ADK opens them with ``start_as_current_span``;
``google-adk==2.12.0`` opens ``invocation`` and ``invoke_agent`` with ``start_span``. Unpatched, the
measured tree was:

    invocation [agent-provider_matching]      openinference
      invocation                              gcp.vertex.agent   <- dropped by Langfuse's filter
        agent_run [provider_matching]         openinference      <- so this was orphaned
          invoke_agent provider_matching      gcp.vertex.agent   <- a second agent node
            call_llm ...                      openinference      <- every generation under it

``_suppress_duplicate_adk_spans`` closes that gap. The test drives Provider Matching with both
sub-agents through a real ``Runner`` and holds the tree: no ADK-native span is recorded, every span's
parent is in the tree, each sub-agent is its own ``agent`` under the tool that dispatched it, and
every scripted model call is one ``LLM`` span. An upgrade of either pin that reopens the gap fails
here rather than in a Langfuse UI.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from agents_fixtures import EVIDENCE_ID, PROVIDER_ID, AllowAllPolicy, StubApi, handler_for, run_turn
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from referraleasy_agent_runtime import AgentRole
from referraleasy_agent_runtime.llm_tracing import _suppress_duplicate_adk_spans
from referraleasy_agents import subagents
from referraleasy_agents.offline_model import TextStep, ToolCallStep, scripted_model

pytestmark = pytest.mark.contract

_DIRECTORY_ANSWER = {
    "candidates": [{"provider_id": PROVIDER_ID, "display_name": "Elena Marquez"}],
    "result_kind": "candidates_found",
    "evidence_id": EVIDENCE_ID,
}


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """ADK instrumented as ``configure_llm_tracing`` does it, and uninstrumented afterwards.

    The global provider records too, so a native ADK span that escaped suppression — which ADK
    emits through the global tracer — is caught rather than silently going nowhere.
    """
    from openinference.instrumentation.google_adk import GoogleADKInstrumentor

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    previous = trace._TRACER_PROVIDER  # restored below: the global is process state
    trace._TRACER_PROVIDER = provider
    instrumentor = GoogleADKInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    _suppress_duplicate_adk_spans()
    try:
        yield exporter
    finally:
        instrumentor.uninstrument()
        trace._TRACER_PROVIDER = previous


def _id(span: ReadableSpan) -> int:
    assert span.context is not None
    return span.context.span_id


def _parent_id(span: ReadableSpan) -> int | None:
    return span.parent.span_id if span.parent is not None else None


@pytest.mark.asyncio
async def test_a_matching_turn_is_one_tree_with_each_model_call_once(
    spans: InMemorySpanExporter,
) -> None:
    parent_answer: dict[str, Any] = {
        "candidates": [],
        "alternatives": [],
        "constraint_change": None,
        "mandatory_conditions": {
            "specialty_identified": True,
            "approval_present": False,
            "slot_valid": False,
        },
        "decision": {
            "action": "provider_matching:find_candidates",
            "reason_code": "no_candidate_matched",
            "evidence_ids": [EVIDENCE_ID],
            "missing_fields": [],
            "next_allowed_actions": ["provider_matching:find_candidates"],
        },
    }
    pool = {"candidates": [], "strategy": "directory_search", "filters_applied": []}
    api = StubApi({"/directory/search": dict(_DIRECTORY_ANSWER)})
    handler = handler_for(
        AgentRole.PROVIDER_MATCHING,
        model=scripted_model(
            [
                ToolCallStep(tool="candidate_researcher", args={"request": "build the pool"}),
                ToolCallStep(tool="constraint_analyst", args={"request": "review the pool"}),
                TextStep(text=json.dumps(parent_answer)),
            ]
        ),
        api=api,
        policy=AllowAllPolicy(),
        sub_agent_models={
            subagents.CANDIDATE_RESEARCHER: scripted_model(
                [
                    ToolCallStep(tool="search_directory", args={"query": "cardiologists"}),
                    TextStep(text=json.dumps(pool)),
                ]
            ),
            subagents.CONSTRAINT_ANALYST: scripted_model(
                [TextStep(text=json.dumps({"candidates": [], "alternatives": []}))]
            ),
        },
    )

    await run_turn(
        AgentRole.PROVIDER_MATCHING,
        script=[],
        workflow_state="PROVIDER_MATCHING",
        handler=handler,
        api=api,
    )

    finished = spans.get_finished_spans()
    native = [
        s.name
        for s in finished
        if s.instrumentation_scope is not None
        and s.instrumentation_scope.name == "gcp.vertex.agent"
    ]
    assert native == [], f"ADK-native spans escaped suppression: {native}"

    ids = {_id(s) for s in finished}
    roots = [s for s in finished if _parent_id(s) not in ids]
    assert [s.name for s in roots] == ["invocation [agent-provider_matching]"]

    by_id = {_id(s): s for s in finished}
    kind = {_id(s): (s.attributes or {}).get("openinference.span.kind") for s in finished}

    def parent(span: ReadableSpan) -> ReadableSpan:
        return by_id[_parent_id(span)]  # type: ignore[index]

    agents = {s.name: s for s in finished if kind[_id(s)] == "AGENT"}
    assert set(agents) == {
        "agent_run [provider_matching]",
        "agent_run [candidate_researcher]",
        "agent_run [constraint_analyst]",
    }
    for name in ("candidate_researcher", "constraint_analyst"):
        # sub-agent -> its invocation -> the tool call that dispatched it, under the parent agent
        dispatch = parent(parent(agents[f"agent_run [{name}]"]))
        assert dispatch.name == f"execute_tool {name}"
        assert parent(dispatch) is agents["agent_run [provider_matching]"]

    # Three scripted steps for the parent, two for the researcher, one for the analyst.
    llm = [s for s in finished if kind[_id(s)] == "LLM"]
    assert sorted(parent(s).name for s in llm) == sorted(
        ["agent_run [provider_matching]"] * 3
        + ["agent_run [candidate_researcher]"] * 2
        + ["agent_run [constraint_analyst]"]
    )
