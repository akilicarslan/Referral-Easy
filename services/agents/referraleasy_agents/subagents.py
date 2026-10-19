# ruff: noqa: RUF002  # Turkish quoted from §3 of ReferralEasy_Architecture_v3.md is kept verbatim
"""Provider Matching's two local ADK sub-agents, and what is deliberately *not* theirs.

§3 puts two sub-agents inside Provider Matching and draws the line between them and
code very precisely: *"Mesafe hesabı, filtreleme ve uniqueness kodla uygulanır…
Basit sıralama doğrudan kodla yapılır; alt ajan belirsiz/çelişkili metinsel koşulları
ve kullanıcıya sorulacak alternatifleri yorumlar."* — distance, filtering and
uniqueness are implemented in code; simple ranking is done directly in code; the
sub-agent interprets ambiguous or contradictory textual conditions **and the
alternatives to put to the user**.

So the two agents here are:

* :data:`CANDIDATE_RESEARCHER` — finds data, keeps provenance and the filter list,
  and produces the candidate pool. It does not rank: `matching_code.rank_candidates`
  runs inside `find_candidates`, which is the tool it calls.
* :data:`CONSTRAINT_ANALYST` — compares each candidate's evidence against each
  constraint and says *why* it fits or does not, and *which alternative* to ask the
  patient about. It has no numeric field to score with, and `matching_code.validate_analysis`
  refuses a non-null verdict with no evidence behind it.

They are invoked by the parent as **ADK agent tools** (:class:`~google.adk.tools.agent_tool.AgentTool`),
in the order §3 describes: the researcher builds the pool, then the analyst reviews it.
§4 permits the ordering because the reviewer's work genuinely depends on the
researcher's — two independent specialists run in parallel, these two cannot.

An ADK `SequentialAgent` was tried first and rejected, on evidence rather than taste
(recorded in ADR-0033): a chain of `sub_agents` is only entered when the parent
*transfers* control, and after a transfer the transferred agent's answer becomes the
invocation's answer — so the parent would never produce its own output, which for
this role is the `MatchingOutput` decision. Probed with a real `Runner` and the
offline model, the parent's own turn ended after the researcher and the analyst never
ran. `AgentTool` composition gives the parent its answer back (`resp:candidate_researcher`,
then `resp:constraint_analyst`, then the parent's own decision) and lets each sub-agent
write its `output_key` into the session the parent is using.

Because the tools arrive as arguments rather than as messages, the analyst reads the
pool the researcher wrote (`{candidate_pool}`) out of the shared session state, and a
resumed turn reads the same key. The parent's own instruction — extended by
:data:`SUB_AGENT_DIRECTIVE` — orders the two calls.

**The shared budget is shared by construction.** Every tool callable in the tree
comes from the *same* `ToolSet`, and a `ToolSet` holds one `TurnBudget`. There is no
second budget to allocate down and no arithmetic that could multiply the turn's
allowance: the researcher's tool call and the analyst's tool call charge the same
counter, so "the sub-agents share the parent's remaining time and call budget" is a
property of the object graph rather than a rule someone has to remember.
`test_matching_subagents.py` asserts it by spending the parent's budget through a
sub-agent's own tool and observing the parent's snapshot move.

The hand-off between the two is ADK session state, not a message: each agent's
``output_key`` writes under the parent's session, so the analyst reads
``{candidate_pool}`` and a resumed turn reads the same key. `assembly.py` owns where
that session lives — see ADR-0033.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from google.adk.agents import Agent
from google.adk.models.base_llm import BaseLlm
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.base_tool import BaseTool

from referraleasy_agents.tools.toolbox import ToolSet

__all__ = [
    "ANALYST_TOOLS",
    "CANDIDATE_POOL_KEY",
    "CANDIDATE_RESEARCHER",
    "CONSTRAINT_ANALYSIS_KEY",
    "CONSTRAINT_ANALYST",
    "RESEARCHER_TOOLS",
    "SUB_AGENT_DIRECTIVE",
    "candidate_researcher",
    "constraint_analyst",
    "protocol_name",
    "sub_agent_names",
    "sub_agent_tools",
]

#: The two sub-agent names **as ADK node names**, which is not how the card spells
#: them. ADK requires `Agent.name` to be a valid Python identifier, so the protocol's
#: hyphenated names (`cards.SUB_AGENT_NAMES`) cannot be used here — a hyphenated name
#: is refused by ADK's own validator before any of this runs. The two spellings are
#: tied together by :func:`protocol_name` rather than by a comment, and
#: `test_matching_subagents.py` asserts one is the other with `-` replaced by `_`.
CANDIDATE_RESEARCHER: Final = "candidate_researcher"
CONSTRAINT_ANALYST: Final = "constraint_analyst"

#: The session-state keys the two agents hand each other.
CANDIDATE_POOL_KEY: Final = "candidate_pool"
CONSTRAINT_ANALYSIS_KEY: Final = "constraint_analysis"

#: Which of the parent's tools each sub-agent may reach. Both are subsets of the
#: parent's `find_candidates`/`search_directory` allowlist — a sub-agent can hold
#: less authority than its parent, never more, and there is no widening path because
#: every callable comes from the parent's own `ToolSet`.
RESEARCHER_TOOLS: Final[tuple[str, ...]] = ("find_candidates", "search_directory")
ANALYST_TOOLS: Final[tuple[str, ...]] = ("search_directory",)

_RESEARCHER_INSTRUCTION: Final = """\
You are the Candidate Researcher inside Provider Matching (§3). Your job is data, not
judgement:

1. Build the candidate pool with `find_candidates`, using the specialty the referral
   states and the origin the caller resolved. Distance, filtering, uniqueness and
   simple ranking are computed **in code** by that tool: do not re-derive a distance,
   re-filter a list, or re-rank what it returned.
2. Use `search_directory` when a question is textual — a patient's wording about a
   practice, a service the structured API cannot filter on. Its answers carry
   provenance and a retrieval degradation level.
3. Preserve provenance and the filter list exactly as they arrive. A candidate
   without evidence is a candidate nobody can defend: keep `provenance` and
   `evidence_ids` as they were returned, and keep `strategy` and `filters_applied`
   so a reviewer can see what was actually searched.
4. Report what you could not find. An empty pool from an honest search is an answer
   (§3: `no_match` is a success), and it must be reported as what it is rather than
   as an error.

You do not decide which candidate is best and you never choose for the patient. You
do not relax a constraint to make the pool non-empty.
"""

_ANALYST_INSTRUCTION: Final = """\
You are the Constraint Analyst inside Provider Matching (§3). The Candidate Researcher
has produced this pool:

{candidate_pool}

Compare each candidate in the pool against each stated constraint, and for every
(candidate, constraint) pair say:

* `satisfied: true` or `false` — and then the `evidence_ids` for that constraint, or
* `satisfied: null` — when the evidence does not say. A null verdict is a real
  answer, not a gap to fill: **absent data is never covered by a score**
  (Türkçe: *"bulunmayan veriyi puanla örtemez"*), and there is no numeric field in
  this output to hide one in.

Then, when nothing satisfies everything, say **which alternative to put to the
patient** — the one constraint whose relaxation would open the most options, phrased
as a question the patient can answer. The patient decides; you propose the question,
never the answer. A hard constraint is never presented as relaxed, and never
presented as satisfied when it is not.

Ranking is not yours: the pool arrives already ranked by distance in code, and a
judgement about "better" is a judgement you are not allowed to make.

If the pool above is empty, say that there was nothing to compare rather than
reviewing a pool you were not given: an empty pool is an answer (`no_match` is a
success), and an invented one is not.
"""


def _tools_named(tools: ToolSet, names: Sequence[str], *, owner: str) -> list[Callable[..., Any]]:
    """The parent's own tool callables for ``names``, and nothing else.

    A name that the parent's `ToolSet` does not expose is a configuration error, not
    an empty list: a sub-agent silently given no tools would answer from memory and
    look like it had searched.
    """
    by_name: Mapping[str, Callable[..., Any]] = {
        callable_.__name__: callable_ for callable_ in tools.exposed()
    }
    missing = [name for name in names if name not in by_name]
    if missing:
        raise ValueError(
            f"{owner} needs tools {missing}, which this role's surface does not expose "
            f"(available: {sorted(by_name)})"
        )
    return [by_name[name] for name in names]


def candidate_researcher(*, tools: ToolSet, model: BaseLlm) -> Agent:
    """The sub-agent that finds data and keeps its provenance.

    ``output_key`` writes the pool into the parent's session state under
    :data:`CANDIDATE_POOL_KEY`, which is both how the analyst receives it and how a
    later turn can read what was actually retrieved.
    """
    return Agent(
        name=CANDIDATE_RESEARCHER,
        description=(
            "Builds the provider candidate pool for a specialty and origin, preserving provenance "
            "and the filters applied. Distance, filtering, uniqueness and ranking are done in code."
        ),
        model=model,
        instruction=_RESEARCHER_INSTRUCTION,
        tools=_tools_named(tools, RESEARCHER_TOOLS, owner=CANDIDATE_RESEARCHER),
        output_key=CANDIDATE_POOL_KEY,
    )


def constraint_analyst(*, tools: ToolSet, model: BaseLlm) -> Agent:
    """The sub-agent that says why each candidate fits, and which alternative to ask about."""
    return Agent(
        name=CONSTRAINT_ANALYST,
        description=(
            "Compares candidates against constraints using evidence, emits null for what the "
            "evidence does not say, and proposes which alternative to put to the patient. "
            "No score, and never a decision on the patient's behalf."
        ),
        model=model,
        instruction=_ANALYST_INSTRUCTION,
        tools=_tools_named(tools, ANALYST_TOOLS, owner=CONSTRAINT_ANALYST),
        output_key=CONSTRAINT_ANALYSIS_KEY,
    )


def sub_agent_tools(*, tools: ToolSet, model_for: Callable[[str], BaseLlm]) -> list[BaseTool]:
    """The two sub-agents, exposed to the parent as agent tools.

    ``model_for`` is asked for the model of each sub-agent by name, and a caller that
    answers with the parent's own model is expressing the shared-budget rule: one
    model object, one `TurnBudget`, one set of counters. The offline harness answers
    with a per-agent script instead, which is why the decision is a function rather
    than a single model argument.

    Neither tool adds authority. Each sub-agent is built from the parent's own
    `ToolSet`, so its tools are a *subset* of the parent's allowlist and every call it
    makes is charged to the parent's turn.
    """
    return [
        AgentTool(agent=candidate_researcher(tools=tools, model=model_for(CANDIDATE_RESEARCHER))),
        AgentTool(agent=constraint_analyst(tools=tools, model=model_for(CONSTRAINT_ANALYST))),
    ]


#: Appended to the parent's instruction for this role. The order of §3's two calls is
#: stated here rather than left to the model's taste: the analyst reads a pool, and a
#: pool is what the researcher produces. It is prompt-level, and it is stated as
#: prompt-level — the analyst's own instruction says what to do when the pool it reads
#: is empty, so a wrong order degrades to an honest "nothing to compare" rather than to
#: an invented pool.
SUB_AGENT_DIRECTIVE: Final = """\

Your two local sub-agents are tools, and you call them in this order:

1. `candidate_researcher` — build the candidate pool for the specialty and the origin.
2. `constraint_analyst` — compare each candidate against each constraint and tell you
   why it fits or does not, and which alternative to put to the patient.

The pool is already filtered, de-duplicated and ranked by distance in code before it
reaches you; do not re-derive any of it, and do not choose for the patient. Your own
answer is the decision, and it cites the evidence ids the tools returned.
"""


def sub_agent_names() -> tuple[str, str]:
    """The two ADK node names, in the order the pipeline runs them."""
    return (CANDIDATE_RESEARCHER, CONSTRAINT_ANALYST)


def protocol_name(adk_name: str) -> str:
    """The name the Agent Card declares for an ADK node name.

    The card is the protocol surface and pins sub-agent names to the same
    ``^[a-z][a-z0-9-]*$`` shape the card name uses (§13); ADK is a Python object
    graph and requires an identifier. This is the one conversion between them, so a
    reader never has to wonder whether `candidate_researcher` and
    `candidate-researcher` are two different agents. It goes only this way: a name is
    authored as a protocol name and derived into a node name, never the reverse.
    """
    return adk_name.replace("_", "-")
