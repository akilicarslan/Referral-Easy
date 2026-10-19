"""The offline model profile: a deterministic ``BaseLlm``, and no socket at all.

``MODEL_BACKEND=offline`` is the default profile in this checkout, and the whole
point of it is that a test can drive a **real ADK turn** — a real ``Runner``, real
tool execution, real session state — without a provider, a credential or a network.
:class:`ScriptedLlm` is what makes that possible: it is a ``BaseLlm`` subclass, so
ADK treats it exactly as it treats ``LiteLlm``, and its answers come from a script
instead of a completion.

The script is a list of steps and a step is either a tool call or a final text:
``ToolCallStep(tool="check_plan", args={...})`` then ``TextStep(text="{...json...}")``.
The step index is **derived, not stored** — it is the number of
``function_response`` parts already in the request — which is what keeps this model
stateless and makes the same instance usable by two concurrent turns without one
consuming the other's steps.

Two things are deliberately *not* here:

* **No fallback to a real provider.** If the script runs out, the model says so in
  its final text. A silent fallback to a live model would turn a hermetic test into
  a network call the moment the script was one step short.
* **No task-sensing.** The model does not read the request to decide what to do. A
  script is authored by the caller (a test, or ``OFFLINE_SCRIPT`` in a container),
  which is the difference between a deterministic harness and a small improviser.

A gap, stated rather than hidden: ``AgentSettings.from_env`` still requires
``AGENT_MODEL_ID`` and ``OPENROUTER_API_KEY``, because the runtime's factory
(`referraleasy_agent_runtime.model`) implements one provider (ADR-0002) and is out
of this package's scope. The offline path never calls ``build_model``, never reads
the key and never constructs ``LiteLlm``; the value only has to be non-empty for the
process to resolve its settings. Wiring the offline profile into the runtime's
factory is Phase 3.5's, not this module's.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import Any, Final

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_OFFLINE_MODEL",
    "MODEL_BACKEND_ENV",
    "NO_SCRIPT_MESSAGE",
    "OFFLINE_BACKEND",
    "OFFLINE_SCRIPT_ENV",
    "ScriptStep",
    "ScriptedLlm",
    "TextStep",
    "ToolCallStep",
    "offline_enabled",
    "script_from_env",
    "scripted_model",
]

#: Which backend the process runs on. Anything other than ``offline`` means the
#: model factory's own path, which needs a provider credential.
MODEL_BACKEND_ENV: Final = "MODEL_BACKEND"
OFFLINE_BACKEND: Final = "offline"

#: A JSON list of steps, for driving an offline container without a code change.
OFFLINE_SCRIPT_ENV: Final = "OFFLINE_SCRIPT"

#: The ADK model string. Not a provider prefix — this model never leaves the process.
DEFAULT_OFFLINE_MODEL: Final = "offline/scripted"

#: The final text when the script has no step left. It is a sentence, not JSON, so
#: the caller's output-schema validation fails loudly instead of quietly succeeding
#: on a turn that never happened.
NO_SCRIPT_MESSAGE: Final = (
    "The offline model profile had no scripted step left for this turn. This answer is "
    "not a result: supply more steps, or run with a live model backend."
)


class ToolCallStep(BaseModel):
    """One step that calls one tool, exactly as the model would have asked for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    args: Mapping[str, Any] = Field(default_factory=dict)


class TextStep(BaseModel):
    """One step that ends the turn with text — the agent's own answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str


#: A step is a tool call or a final answer. Two shapes, because ADK's own protocol
#: has two: a content that carries a `function_call`, and one that carries text.
ScriptStep = ToolCallStep | TextStep


def _step_from_payload(payload: Mapping[str, Any]) -> ScriptStep:
    step = dict(payload)
    if "tool" in step:
        return ToolCallStep.model_validate(step)
    return TextStep.model_validate(step)


def script_from_env(env: Mapping[str, str] | None = None) -> tuple[ScriptStep, ...]:
    """Read ``OFFLINE_SCRIPT`` as a JSON list of steps. Absent or empty means none."""
    import os

    resolved = (os.environ if env is None else env).get(OFFLINE_SCRIPT_ENV, "").strip()
    if not resolved:
        return ()
    parsed: Any = json.loads(resolved)
    if not isinstance(parsed, list):
        raise ValueError(f"{OFFLINE_SCRIPT_ENV} must be a JSON list of steps")
    return tuple(_step_from_payload(step) for step in parsed)


def offline_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether ``MODEL_BACKEND`` selects the offline profile."""
    import os

    resolved = os.environ if env is None else env
    return resolved.get(MODEL_BACKEND_ENV, "").strip().lower() == OFFLINE_BACKEND


class ScriptedLlm(BaseLlm):
    """A deterministic model: the ``index``-th step answers the ``index``-th call.

    ``index`` is the count of ``function_response`` parts in the request, so a step
    is consumed by the *tool result* it belongs to rather than by a counter this
    object has to keep. Nothing else about the request changes the answer, which is
    what makes two runs comparable.
    """

    model: str = DEFAULT_OFFLINE_MODEL
    script: tuple[ScriptStep, ...] = ()

    def scripted_tools(self) -> tuple[str, ...]:
        """The tools this script names, in order. Used by tests, not by the turn."""
        return tuple(step.tool for step in self.script if isinstance(step, ToolCallStep))

    def _index(self, llm_request: LlmRequest) -> int:
        return sum(
            1
            for content in llm_request.contents or []
            for part in content.parts or []
            if part.function_response is not None
        )

    def _declared(self, llm_request: LlmRequest, tool: str) -> None:
        """Refuse a scripted tool the agent was not given.

        A script naming a tool outside the turn's surface is a bug in the script, and
        it must fail here — where the name is still the script's — rather than as a
        tool-not-found inside the agent loop.
        """
        declared = set(llm_request.tools_dict or {})
        if declared and tool not in declared:
            raise ValueError(
                f"the offline script calls tool {tool!r}, which this agent was not given "
                f"(declared: {sorted(declared)})"
            )

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        """Emit the script's next step. ``stream`` is accepted and ignored, on purpose.

        The profile answers a whole turn at a time: there is nothing to stream from,
        and pretending otherwise would put a `partial` flag on a response that was
        never incremental.
        """
        del stream
        index = self._index(llm_request)
        if index >= len(self.script):
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text=NO_SCRIPT_MESSAGE)])
            )
            return
        step = self.script[index]
        if isinstance(step, ToolCallStep):
            self._declared(llm_request, step.tool)
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(name=step.tool, args=dict(step.args))
                        )
                    ],
                )
            )
            return
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=step.text)]))


def scripted_model(script: Sequence[ScriptStep] = ()) -> ScriptedLlm:
    """A :class:`ScriptedLlm` over ``script``.

    A named constructor rather than a bare ``ScriptedLlm(script=...)`` call so the
    model string is decided in one place: the ADK ``Agent`` records it, and a test
    asserting "no provider was constructed" can look for it.
    """
    return ScriptedLlm(model=DEFAULT_OFFLINE_MODEL, script=tuple(script))
