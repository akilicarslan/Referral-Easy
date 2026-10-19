"""Where the ADK objects are put together, and where the ADK session lives.

The specialists run under ADK's own `Runner` (ADR-0033): a real ``google.adk``
``Agent`` per role, tools that are real Python callables, and a session service that
holds the state the sub-agents hand each other. The runtime's pieces compose
*around* that — the allowlist gate, the turn budget, the retry policy, the result
envelope and the suspend/resume contract all still apply, because every tool call in
the tree goes through one `ToolSet`.

Two things are per-turn rather than per-service, and both on purpose:

* **The agent.** ADK fixes ``tools`` at construction, and the tool surface is
  per-turn (`ToolSet` binds the budget, the envelope and the provenance ledger).
  Building the agent per turn is what makes "the model can only call what this turn
  may call" true; it is cheap, because the expensive object (the model) is shared.
* **The session identity.** §7 keys everything a turn does by workflow and command,
  so the ADK session is keyed the same way:

  ================  =========================================================
  ``app_name``      ``service_name(role)`` — ``agent-scheduling``
  ``user_id``       ``workflow_id`` — one workflow is one user of one agent
  ``session_id``    ``f"{workflow_id}:{command_id}"``
  ================  =========================================================

  The session is therefore per **command**, not per workflow: a redelivered command
  resumes the same session, and a genuinely new command starts a new one. That is
  what makes ADK's memory of a turn match the workflow's — the relation ADR-0033
  records, and the reason the session service is not asked to be clever.

Nothing here reads the environment except :func:`resolve_llm`, and that function
exists so the offline profile is chosen in exactly one place.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from google.adk.agents import Agent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService
from google.adk.tools.base_tool import BaseTool

from referraleasy_agent_runtime import (
    AgentRole,
    ConfigurationError,
    TurnBudget,
    UpstreamTransient,
    service_name,
)
from referraleasy_agents.cards import CARD_NAME_BY_ROLE
from referraleasy_agents.offline_model import (
    ScriptStep,
    offline_enabled,
    script_from_env,
    scripted_model,
)
from referraleasy_agents.subagents import SUB_AGENT_DIRECTIVE, sub_agent_tools
from referraleasy_agents.tools.toolbox import ToolSet

__all__ = [
    "BudgetedLlm",
    "TurnSession",
    "adk_node_name",
    "budgeted_model",
    "build_agent",
    "build_runner",
    "resolve_llm",
    "session_key",
    "turn_session",
]


class BudgetedLlm(BaseLlm):
    """A model whose every call charges the turn's model-call quota.

    ADK owns the agent loop, so without this wrapper the turn's
    ``max_model_calls`` cap would be decorative: ADK would ask the provider as many
    times as the loop needed and nothing would count. Wrapping the model is the one
    seam where a call that leaves the process can be charged, so it is where the
    runtime's quota is enforced — the cap in `BudgetCaps`, the deadline, and the
    lease's own timeout all still come from `TurnBudget`, which stays the single
    owner of "how much is left".

    Two readings of the cap, both deliberate:

    * a call is charged **before** it is made, so a call that times out or errors is
      still counted — an attempt that reached the provider is not free;
    * the model call is bounded by the lease's ``timeout_ms``, which is
      ``min(attempt_timeout_ms, remaining_ms)``. A model that hangs past the turn's
      deadline cannot keep the turn alive, and the failure arrives as the runtime's
      :class:`UpstreamTransient` rather than a bare ``TimeoutError``, because the
      model *is* an upstream and its timeout is retryable.
    """

    model: str = ""
    #: The wrapped model. `Any` rather than `BaseLlm` so pydantic does not re-validate
    #: and copy it: the identity of the shared model object is what makes the per-turn
    #: wrapper cheap.
    inner: Any = None
    budget: Any = None

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if self.inner is None or self.budget is None:
            raise ConfigurationError(
                "BudgetedLlm was constructed without an inner model or a budget; a model that "
                "cannot charge the turn would let the loop run past its cap."
            )
        budget: TurnBudget = self.budget
        with budget.model_call(site="agent_model") as lease:
            try:
                async with asyncio.timeout(lease.timeout_ms / 1000):
                    async for response in self.inner.generate_content_async(
                        llm_request, stream=stream
                    ):
                        yield response
            except TimeoutError as error:
                raise UpstreamTransient(
                    f"the model did not answer within {lease.timeout_ms} ms, the turn's own "
                    "remaining time for one attempt"
                ) from error


def budgeted_model(model: BaseLlm, *, budget: TurnBudget) -> BaseLlm:
    """``model``, wrapped so this turn's model calls are charged and bounded."""
    return BudgetedLlm(model=model.model, inner=model, budget=budget)


def adk_node_name(role: AgentRole) -> str:
    """The ADK node name for a role: the card name, hyphenated shape and all.

    ADK requires ``Agent.name`` to be a valid Python identifier, and the protocol's
    card name is ``referral-intake`` — a hyphen is refused by ADK's own validator, so
    the node name is the card name with ``-`` replaced by ``_``. The card is unchanged
    and remains the protocol identity; this is the internal spelling, derived here so
    that exactly one place knows how the two relate.
    """
    return CARD_NAME_BY_ROLE[role].replace("-", "_")


@dataclass(frozen=True, slots=True)
class TurnSession:
    """The three keys one turn's ADK session is filed under (§7)."""

    app_name: str
    user_id: str
    session_id: str

    def as_trace_entry(self) -> dict[str, str]:
        return {"app_name": self.app_name, "user_id": self.user_id, "session_id": self.session_id}


def session_key(role: AgentRole, *, workflow_id: str, command_id: str) -> TurnSession:
    """The session identity for one command of one workflow.

    ``app_name`` is the service name rather than the card name: it names the
    *deployment*, which is what a session service with several apps keys on. The card
    name is the protocol identity and lives in ``cards.py``; the two are kept
    separate so renaming a card cannot orphan every stored session.
    """
    return TurnSession(
        app_name=service_name(role),
        user_id=workflow_id,
        session_id=f"{workflow_id}:{command_id}",
    )


#: Kept as a function so a test can assert the app name is the service's, not the
#: card's, without restating the string.
def turn_session(role: AgentRole, *, workflow_id: str, command_id: str) -> TurnSession:
    """Alias of :func:`session_key`, named for what a caller is building."""
    return session_key(role, workflow_id=workflow_id, command_id=command_id)


def resolve_llm(
    role: AgentRole,
    *,
    env: Mapping[str, str] | None = None,
    script: Sequence[ScriptStep] = (),
) -> BaseLlm:
    """The model this process runs ``role`` on: the offline script, or the factory's.

    The offline profile is checked **first**, so an offline process never calls
    ``build_model`` and never touches a provider credential (ADR-0002 is the factory's
    path, and ADR-0033 records the split). The script comes from the caller when one
    is supplied, and otherwise from ``OFFLINE_SCRIPT``.
    """
    if offline_enabled(env):
        return scripted_model(script or script_from_env(env))

    from referraleasy_agent_runtime import build_model

    model, _ = build_model(env if env is not None else _environ(), role=role.value)
    return model


def _environ() -> Mapping[str, str]:
    import os

    return os.environ


def build_agent(
    role: AgentRole,
    *,
    tools: ToolSet,
    instruction: str,
    model: BaseLlm,
    sub_agent_models: Mapping[str, BaseLlm] | None = None,
) -> Agent:
    """One role's ADK agent, holding this turn's tools and nothing more.

    The tool callables come from the turn's own `ToolSet`, so the agent cannot be
    handed a surface wider than the turn's authority — there is no path here that
    adds a tool or widens an allowlist. The one extra entry point Provider Matching
    gets is a **sub-agent**, and a sub-agent is not authority: it is built from the
    same `ToolSet`, so it can only call what this turn may already call, and its calls
    charge this turn's budget.

    ``sub_agent_models`` exists for the offline harness, which gives each agent its
    own script. A caller that omits it gets the parent's model in both sub-agents,
    which is also what a live process does.
    """
    # Two kinds of entry point reach ADK's `tools`: the callables `ToolSet.exposed()`
    # produces, and the `BaseTool` objects ADK's `AgentTool` wraps a sub-agent in. Both
    # are declared here rather than left to inference, because the union is exactly the
    # surface this function may build.
    tools_: list[BaseTool | Callable[..., Any]] = [*tools.exposed()]
    if role is AgentRole.PROVIDER_MATCHING:

        def model_for(name: str) -> BaseLlm:
            if sub_agent_models is not None and name in sub_agent_models:
                return sub_agent_models[name]
            return model

        tools_ = [*tools_, *sub_agent_tools(tools=tools, model_for=model_for)]

    return Agent(
        name=adk_node_name(role),
        description=f"{service_name(role)}: a ReferralEasy specialist (see its Agent Card).",
        model=model,
        instruction=(
            f"{instruction}\n{SUB_AGENT_DIRECTIVE}"
            if role is AgentRole.PROVIDER_MATCHING
            else instruction
        ),
        tools=tools_,
        # The parent's own final answer is the turn's output. Sub-agents write their own
        # keys; the parent does not, so a sub-agent's intermediate state cannot be
        # mistaken for the answer.
        output_key=None,
    )


def build_runner(
    role: AgentRole,
    *,
    agent: Agent,
    sessions: BaseSessionService,
) -> Runner:
    """A `Runner` for one turn, over the service's session service.

    One Runner per turn rather than one per process: the Runner is bound to the
    agent, and the agent is bound to the turn's tools. Sessions live in the shared
    service, which is the part that must survive — and the part whose survival lets a
    resumed turn find the pool the researcher wrote.

    ``auto_create_session`` is left off deliberately: a turn whose session is missing
    is a bug in the caller (it must open the session with the command's key first),
    and a Runner that silently created one would hide a workflow whose state was
    never restored.
    """
    return Runner(
        app_name=service_name(role),
        agent=agent,
        session_service=sessions,
    )


#: The instruction-state keys ADK substitutes into an instruction, recorded here so a
#: caller does not have to read `subagents.py` to learn them.
INSTRUCTION_STATE_KEYS: Final[tuple[str, ...]] = ("candidate_pool",)
