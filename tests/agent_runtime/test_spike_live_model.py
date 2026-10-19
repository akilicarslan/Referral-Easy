"""Live-model spike: the four behaviours the runtime's model path depends on.

Deselected by default (``-m 'not spike'`` in ``addopts``) because every test here
costs a network round-trip and a little money. Run it deliberately, before a release
or after touching ``model.py``/``retries.py``:

    uv run pytest tests/agent_runtime -m spike -v

Credentials come from the repository ``.env``, read **inside this module** so an
offline run never touches it, and applied to ``os.environ`` only for the duration of
a test: LiteLLM reads ``OPENROUTER_API_KEY`` itself, which is why :func:`build_model`
never hands it the key.

What each test is defending:

* **Tool calling** — an agent whose tools are never called is a chat wrapper. The
  tool is declared through a JSON schema, exactly as the runtime declares its own.
* **Structured output** — ``response_schema`` (not ``response_json_schema``) is the
  spelling that returns the object shape; a test pins the working one.
* **Streaming** — ADK's ``generate_content_async(stream=True)`` yields partials.
* **Cancellation** — the turn deadline's whole premise. If cancelling does not stop
  the stream, the 30 s budget is a suggestion rather than a deadline.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import dotenv_values

from referraleasy_agent_runtime.budget import BudgetCaps, BudgetExhausted, CapKind, TurnBudget
from referraleasy_agent_runtime.errors import ConfigurationError
from referraleasy_agent_runtime.model import OPENROUTER_API_KEY_ENV, build_model, resolve_model
from referraleasy_agent_runtime.retries import run_with_retries

pytestmark = pytest.mark.spike

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"

#: The model the spike was run against. Named here so a test failure says which
#: construction drifted, rather than reporting a generic shape mismatch.
SPIKE_MODEL_ID = "anthropic/claude-haiku-5.5"


@pytest.fixture(scope="module")
def live_env() -> Iterator[dict[str, str]]:
    """The repository ``.env``, applied to ``os.environ`` and restored afterwards."""
    if not ENV_FILE.exists():
        pytest.skip(f"no {ENV_FILE.name} in the repository root")

    values = {key: value for key, value in dotenv_values(ENV_FILE).items() if value}
    if not values.get(OPENROUTER_API_KEY_ENV):
        pytest.skip(f"{OPENROUTER_API_KEY_ENV} is not set in {ENV_FILE.name}")

    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield {**os.environ}
    finally:
        for key, previous in saved.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


@pytest.fixture(scope="module")
def live_model(live_env: dict[str, str]) -> tuple[object, object]:
    model, ref = build_model(live_env, role="scheduling")
    assert ref.litellm_model == f"openrouter/{SPIKE_MODEL_ID}"
    return model, ref


def _request(ref: object, text: str, config: object | None = None) -> object:
    from google.adk.models import LlmRequest
    from google.genai import types

    return LlmRequest(
        model=ref.litellm_model,  # type: ignore[attr-defined]
        contents=[types.Content(role="user", parts=[types.Part(text=text)])],
        config=config or types.GenerateContentConfig(),
    )


def _text_of(response: object) -> str:
    content = getattr(response, "content", None)
    parts = getattr(content, "parts", None) or []
    return "".join(part.text or "" for part in parts)


@pytest.mark.asyncio
async def test_the_live_model_answers(live_model: tuple[object, object]) -> None:
    model, ref = live_model

    response = await anext(
        model.generate_content_async(  # type: ignore[attr-defined]
            _request(ref, "Reply with exactly the word: pong"), stream=False
        )
    )

    assert "pong" in _text_of(response).lower()


@pytest.mark.asyncio
async def test_the_live_model_calls_a_tool(live_model: tuple[object, object]) -> None:
    """The construction the seven agents depend on: a declared tool is actually called."""
    from google.genai import types

    model, ref = live_model
    tool = types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="list_slots",
                description="List available appointment slots for a provider.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {"provider_id": {"type": "string"}},
                    "required": ["provider_id"],
                },
            )
        ]
    )

    response = await anext(
        model.generate_content_async(  # type: ignore[attr-defined]
            _request(
                ref,
                "Find slots for provider p-1. Call the tool; do not answer in prose.",
                types.GenerateContentConfig(tools=[tool]),
            ),
            stream=False,
        )
    )

    calls = [
        (part.function_call.name, dict(part.function_call.args or {}))
        for part in (response.content.parts or [])
        if part.function_call is not None
    ]
    assert calls, "the model answered in prose instead of calling the declared tool"
    name, arguments = calls[0]
    assert name == "list_slots"
    assert arguments.get("provider_id") == "p-1"


@pytest.mark.asyncio
async def test_the_live_model_returns_structured_output(live_model: tuple[object, object]) -> None:
    """`response_schema` is the spelling that returns the object; pinned on purpose."""
    from google.genai import types

    model, ref = live_model
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema={
            "type": "object",
            "properties": {"slot_id": {"type": "string"}},
            "required": ["slot_id"],
        },
    )

    response = await anext(
        model.generate_content_async(  # type: ignore[attr-defined]
            _request(ref, "Return slot_id 42. Emit JSON only.", config), stream=False
        )
    )

    parsed = json.loads(_text_of(response))
    assert parsed == {"slot_id": "42"}


@pytest.mark.asyncio
async def test_the_live_model_streams_partial_chunks(live_model: tuple[object, object]) -> None:
    model, ref = live_model

    chunks = [
        chunk
        async for chunk in model.generate_content_async(  # type: ignore[attr-defined]
            _request(ref, "Count from 1 to 12, one number per line."), stream=True
        )
    ]

    assert len(chunks) > 1, "a single chunk means streaming silently degraded to one call"
    assert "".join(_text_of(chunk) for chunk in chunks).strip()


@pytest.mark.asyncio
async def test_a_live_stream_can_be_cancelled_and_stops(live_model: tuple[object, object]) -> None:
    """If cancellation does not stop the stream, the turn deadline is not a deadline."""
    model, ref = live_model
    stream = model.generate_content_async(  # type: ignore[attr-defined]
        _request(ref, "Write 300 words about appointment scheduling."), stream=True
    )
    received = 0

    async def drain() -> None:
        nonlocal received
        async for _ in stream:
            received += 1

    reader = asyncio.create_task(drain())
    await asyncio.sleep(2.5)
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader

    stopped_at = received
    await asyncio.sleep(0.5)

    assert reader.done()
    assert received == stopped_at, "the stream kept producing after cancellation"


@pytest.mark.asyncio
async def test_a_live_call_runs_inside_the_turn_budget(live_env: dict[str, str]) -> None:
    """The real model, reached through the one retry owner, charging the real counters."""
    model, ref = build_model(live_env, role="scheduling")
    budget = TurnBudget(BudgetCaps.from_env(live_env), role="scheduling")

    async def call(lease: object) -> str:
        response = await anext(
            model.generate_content_async(  # type: ignore[attr-defined]
                _request(ref, "Reply with exactly the word: pong"), stream=False
            )
        )
        return _text_of(response)

    outcome = await run_with_retries(
        call, budget=budget, site="model:spike", timeout_ms=budget.caps.attempt_timeout_ms
    )

    assert "pong" in outcome.value.lower()
    assert outcome.attempts == 1
    assert outcome.outcome.value == "completed"
    assert budget.snapshot().model_calls == 1
    assert outcome.to_result().a2a_state.value == "completed"


@pytest.mark.asyncio
async def test_a_live_call_that_overruns_the_attempt_timeout_is_cancelled(
    live_env: dict[str, str],
) -> None:
    """A 1 ms attempt budget must stop a real HTTP call, not merely mark it late."""
    model, ref = build_model(live_env, role="scheduling")
    budget = TurnBudget(BudgetCaps.from_env(live_env), role="scheduling")
    started = time.monotonic()

    async def too_slow(lease: object) -> str:
        await model.generate_content_async(  # type: ignore[attr-defined]
            _request(ref, "Write 300 words about appointment scheduling."), stream=True
        ).__anext__()
        return "never reached"

    with pytest.raises(BudgetExhausted) as raised:
        await run_with_retries(too_slow, budget=budget, site="model:spike", timeout_ms=1)

    assert raised.value.cap is CapKind.ATTEMPTS
    assert time.monotonic() - started < 30, "the attempt timeout did not stop the call"


def test_the_spike_model_is_the_one_the_environment_names(live_env: dict[str, str]) -> None:
    ref = resolve_model(live_env, role="scheduling")

    assert ref.provider == "openrouter"
    assert ref.model_id == live_env.get("AGENT_MODEL_ID", SPIKE_MODEL_ID).strip()
    assert ref.litellm_model == f"openrouter/{ref.model_id}"


def test_a_missing_key_is_refused_before_the_network(live_env: dict[str, str]) -> None:
    without_key = {key: value for key, value in live_env.items() if key != OPENROUTER_API_KEY_ENV}

    with pytest.raises(ConfigurationError, match=OPENROUTER_API_KEY_ENV):
        build_model(without_key, role="scheduling")
