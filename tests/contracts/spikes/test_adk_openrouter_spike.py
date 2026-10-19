"""Spike S-02: ADK + LiteLlm against OpenRouter.

Establishes, against a live model, the four capabilities ADR-0002 claims for the
agent loop:

1. tool calling
2. structured output
3. streaming (incremental chunks, not one coalesced response)
4. cancellation (a cancelled stream actually stops, promptly)

Capability 4 in particular was **not** found in the ADK source at v2.12.0: there
is no cancellation hook in ``lite_llm.py`` or ``base_llm.py``. That is precisely
why it is measured here instead of asserted.

Requires OPENROUTER_API_KEY and AGENT_MODEL_ID. Run with ``make spike``.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from _spike_env import openrouter_model_id, require_env, require_module

pytestmark = pytest.mark.spike

ADR = "ADR-0002-agent-model-runtime-google-adk-and-litellm.md"

_TOOL_PROMPT = "How many open referrals does patient P-1001 have? Use the tool."
_STRUCTURED_PROMPT = "Summarise this referral in the requested schema: a 61 year old patient needs a cardiology review within 2 weeks."
_STREAM_PROMPT = "Count from 1 to 20, one number per line, nothing else."


def _requirements() -> tuple[str, str]:
    """Return (api_key, llm_model). Fails loudly when either is unresolved."""
    api_key = require_env(
        "OPENROUTER_API_KEY",
        purpose="authenticates the ADK -> LiteLlm -> OpenRouter path",
        where="OpenRouter dashboard -> Keys -> Create key (https://openrouter.ai/keys)",
        adr=ADR,
    )
    raw_model = require_env(
        "AGENT_MODEL_ID",
        purpose="the tool-capable agent model this spike verifies",
        where="resolve it by running this spike against a candidate; the id must support "
        "tools + structured output (catalogue snapshot 2026-10-14: 342 of 462 models claim both)",
        adr=ADR,
        extra="""
  Candidates seen in the OpenRouter catalogue on 2026-10-14 (NOT pins - verify the
  capability claim by running this spike, then record the result in the ADR):

      google/gemini-3.8-flash          openai/gpt-6.1-sol
      google/gemini-3.5-flash          anthropic/claude-sonnet-5.5
      google/gemini-3.1-flash-lite     mistralai/mistral-large-4-0
""",
    )
    return api_key, openrouter_model_id(raw_model)


def _adk() -> tuple[Any, Any, Any]:
    """Import the ADK surface this spike needs, failing loudly when absent.

    Imports live inside functions on purpose: the default pytest run imports this
    module while deselecting its tests, so nothing here may require an optional
    SDK at import time.
    """
    require_module("litellm", distribution="litellm==1.105.1", adr=ADR)
    lite_llm = require_module(
        "google.adk.models.lite_llm", distribution="google-adk==2.12.0", adr=ADR
    )
    llm_request = require_module(
        "google.adk.models.llm_request", distribution="google-adk==2.12.0", adr=ADR
    )
    genai_types = require_module("google.genai.types", distribution="google-genai", adr=ADR)
    return lite_llm, llm_request, genai_types


def _make_llm(model: str) -> Any:
    lite_llm, _, _ = _adk()
    return lite_llm.LiteLlm(model=model)


def _make_request(prompt: str, *, config: Any = None) -> Any:
    _, llm_request, genai_types = _adk()
    # `config` must be omitted rather than passed as None: LlmRequest types it as a model, and
    # pydantic rejects an explicit None with `model_attributes_type`. Only pass it when set.
    kwargs: dict[str, Any] = {} if config is None else {"config": config}
    return llm_request.LlmRequest(
        contents=[
            genai_types.Content(role="user", parts=[genai_types.Part.from_text(text=prompt)])
        ],
        **kwargs,
    )


def test_tool_calling() -> None:
    """A declared tool must come back as a function_call with parseable arguments."""
    _, model = _requirements()
    llm = _make_llm(model)
    _, _, genai_types = _adk()

    tool = genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="get_open_referral_count",
                description="Return how many open referrals a patient has.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient identifier, e.g. P-1001",
                        }
                    },
                    "required": ["patient_id"],
                },
            )
        ]
    )
    config = genai_types.GenerateContentConfig(tools=[tool])
    request = _make_request(_TOOL_PROMPT, config=config)

    async def run() -> list[Any]:
        return [r async for r in llm.generate_content_async(request)]

    responses = asyncio.run(run())
    assert responses, "the model returned no response at all"

    calls: list[Any] = []
    for response in responses:
        for part in (response.content.parts or []) if response.content else []:
            if getattr(part, "function_call", None):
                calls.append(part.function_call)

    if not calls:
        rendered = [str(getattr(r, "content", None)) for r in responses]
        pytest.fail(
            "TOOL CALLING NOT SUPPORTED by the configured model "
            f"({model}).\n\n"
            "Expected a part carrying `function_call`, got:\n  "
            + "\n  ".join(rendered)
            + "\n\nThis model is advertised as tool-capable but did not emit a tool call "
            "through LiteLlm. Pick a different AGENT_MODEL_ID and re-run, then record the "
            f"result in docs/adr/{ADR}.",
            pytrace=False,
        )

    call = calls[0]
    assert call.name == "get_open_referral_count", f"wrong tool called: {call.name!r}"
    args = dict(call.args)
    assert "patient_id" in args, f"tool arguments did not round-trip: {args!r}"


def test_structured_output() -> None:
    """A declared response_schema must yield JSON that validates against it."""
    _, model = _requirements()
    llm = _make_llm(model)
    _, _, genai_types = _adk()

    try:
        from pydantic import BaseModel, Field

        class Triage(BaseModel):
            urgency: str = Field(description="one of: routine, urgent, emergency")
            specialty: str = Field(description="target specialty, e.g. cardiology")
            window_days: int = Field(description="days within which review is needed")

    except ImportError:  # pragma: no cover - pydantic ships with ADK
        pytest.fail("pydantic is unavailable; it is a dependency of google-adk.", pytrace=False)

    config = genai_types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=Triage,
    )
    request = _make_request(_STRUCTURED_PROMPT, config=config)

    async def run() -> list[Any]:
        return [r async for r in llm.generate_content_async(request)]

    responses = asyncio.run(run())
    texts = [
        part.text
        for response in responses
        if response.content
        for part in (response.content.parts or [])
        if getattr(part, "text", None)
    ]
    assert texts, f"structured output produced no text part for {model}"

    raw = texts[-1]
    try:
        parsed = Triage.model_validate(json.loads(raw))
    except Exception as exc:
        pytest.fail(
            "STRUCTURED OUTPUT NOT HONOURED "
            f"({model}).\n\nRaw model output:\n{raw}\n\n"
            f"Expected JSON validating against this schema:\n"
            f"{json.dumps(Triage.model_json_schema(), indent=2)}\n\n"
            f"Validation error: {exc}\n\n"
            "ADK converts response_schema into a LiteLLM structured-output format "
            "(google-adk v2.12.0, models/lite_llm.py); if the provider ignores it, the "
            f"schema is not usable and docs/adr/{ADR} must record that.",
            pytrace=False,
        )
    assert parsed.window_days > 0


def test_streaming_yields_incremental_chunks() -> None:
    """stream=True must deliver more than one chunk, not a single coalesced response."""
    _, model = _requirements()
    llm = _make_llm(model)
    request = _make_request(_STREAM_PROMPT)

    async def run() -> list[Any]:
        return [r async for r in llm.generate_content_async(request, stream=True)]

    chunks = asyncio.run(run())
    if len(chunks) < 2:
        pytest.fail(
            f"STREAMING DID NOT DELIVER INCREMENTAL CHUNKS ({model}).\n\n"
            f"Expected several partial responses for {_STREAM_PROMPT!r}, got "
            f"{len(chunks)}.\n"
            "The transport works but buffered the whole completion, so the demo's "
            "token-by-token UI path and its TURN_DEADLINE_MS budget cannot be evaluated "
            f"from this run. Record the finding in docs/adr/{ADR}.",
            pytrace=False,
        )
    assert any(
        part.text
        for chunk in chunks
        if chunk.content
        for part in (chunk.content.parts or [])
        if getattr(part, "text", None)
    ), "stream produced chunks but none carried text"


def test_cancellation_stops_the_stream() -> None:
    """Cancelling the consuming task must raise promptly instead of running to completion.

    No cancellation handling exists in ADK's lite_llm.py / base_llm.py at v2.12.0, so
    this measures behaviour rather than confirming it.
    """
    _, model = _requirements()
    llm = _make_llm(model)
    request = _make_request(_STREAM_PROMPT)
    budget_seconds = 5.0

    async def consume() -> int:
        seen = 0
        async for _chunk in llm.generate_content_async(request, stream=True):
            seen += 1
        return seen

    async def scenario() -> tuple[bool, float]:
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.5)  # let the HTTP request get under way
        if task.done():
            return (False, 0.0)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return (True, time.monotonic() - started)

    cancelled, elapsed = asyncio.run(scenario())

    if not cancelled:
        pytest.fail(
            f"CANCELLATION NOT OBSERVABLE ({model}).\n\n"
            "The stream completed within 0.5s, so cancellation could not be exercised. "
            "Re-run with a longer completion (raise the count in "
            f"_STREAM_PROMPT) to produce a measurable result for docs/adr/{ADR}.",
            pytrace=False,
        )

    assert elapsed < budget_seconds, (
        f"cancelling took {elapsed:.2f}s, over the {budget_seconds}s budget. "
        "With AGENT_ATTEMPT_TIMEOUT_MS=3000 and MAX_MODEL_CALLS_PER_TURN=12, a slow "
        "cancel leaks a live HTTP request per cancelled turn."
    )
