# ADR-0033 — The specialists run under ADK's `Runner`, and the runtime composes around it

- **Status:** Accepted
- **Date:** 2026-10-15
- **Phase:** the seven specialist services (§3, §4, §5, §6, §7, §8)
- **Depends on:** §3 (agent roles, tools and forbidden authority), §4 (flow and parallelism), §5
  (envelopes, versioned schemas, decision outputs), §6 (context minimisation), §7 (workflow states),
  §8 (failure classes and the one-way door), ADR-0002 (Google ADK), ADR-0030 (the agent runtime)

## Context

`packages/agent-runtime` (ADR-0030) owns the turn: the budget, the allowlist gate, the policy
authority, the retry owner, the result projection and the suspend/resume contract. It does **not**
own the agent loop — `create_app` takes a `TaskHandler` and asks it to return a `TurnReport`. Phase
3.2 is the seven handlers, and the first question is who runs the model loop:

1. **ADK's `Runner`.** A real `google.adk.agents.Agent` per role, real tool callables, ADK's own
   event stream and session service. ADK is the framework the project advertises (§14), so
   "built on Google ADK" means the agent loop, not a dependency in a lockfile.
2. **A runtime-owned turn loop.** Call `BaseLlm.generate_content_async` directly, execute the
   function calls we recognise, and stop. Total control over the loop, at the cost of writing — and
   maintaining — a small agent framework: tool declaration, function-call plumbing, multi-agent
   handoff, session state, event streaming. Every one of those is a place where our version and
   ADK's version could disagree, and the disagreement would be invisible until a model behaved
   unusually.

Two constraints made this a real decision rather than a preference:

* **The budget must keep meaning something.** §4's caps are per turn. If ADK owns the loop and the
  budget is only consulted by the tools, `max_model_calls` is decorative — the loop would call the
  provider as often as it liked and only the tool counter would move.
* **§3's forbidden list must be refused by the runtime, not by a prompt.** "cannot write a booking
  directly" has to survive a model that tries anyway.

## Decision

**The specialists run under ADK's `Runner`.** `assembly.build_agent` builds one `Agent` per turn
from this turn's `ToolSet`; `assembly.build_runner` wraps it in a `Runner` over a shared
`SessionService`; `handler.RoleHandler` is the `TaskHandler` that drives one turn and projects its
result. The runtime's pieces compose *around* the loop rather than being reimplemented inside it.

### The budget is enforced by wrapping the model, not by asking ADK

ADK calls the model itself, so the model is wrapped: `assembly.BudgetedLlm` charges
`TurnBudget.model_call(site="agent_model")` **before** each call and bounds the call by the lease's
`timeout_ms` — `min(attempt_timeout_ms, remaining_ms)` — raising the runtime's `UpstreamTransient`
when the lease expires. A call that times out is still counted; a loop cannot outlive the deadline;
a sub-agent's model calls charge the parent's counter, because it is the same wrapper over the same
budget. Verified with a real `Runner` and the offline model: one parent turn with two sub-agents
charged `model_calls=7`, `tool_calls=2` to a single budget.

### Tool calls are the same call path the tests probe

Every callable the model can reach comes from `ToolSet.exposed()`, which wraps `ToolSet.invoke`:
allowlist check, policy check, lease, handler, provenance. ADK's function-call plumbing is a
transport onto that path, so a forbidden tool is refused by `ToolGate` with `ToolNotAllowed` — and
the refusal propagates out of the ADK run, because ADK re-raises a tool's exception. This is what
makes §3's forbidden column testable without a live model: `forbidden.py` calls the same functions
with the same arguments.

### Where the ADK session lives, and how it relates to `workflow_id` / `command_id`

```
app_name   = service_name(role)              # agent-scheduling (the deployment, not the card)
user_id    = workflow_id                     # one workflow is one user of one agent
session_id = f"{workflow_id}:{command_id}"   # one ADK session per command
```

The session is therefore **per command, not per workflow**: a redelivered command finds the session
it already opened and resumes it, and a genuinely new command starts a new one. That is the same
granularity §7 gives a command, and it is why the session is not keyed by `session_id` from the
request — the request's `session_id` is the *transport's* session, which is a different thing from
"the state this command has accumulated".

`auto_create_session` is off (`build_runner`), deliberately: a turn whose session is missing is a
bug in the caller, and a Runner that silently created one would hide a workflow whose state was
never restored. The handler opens the session with the command's key before the run, reusing an
existing session when the command is a redelivery.

The session service is in-memory in-process (`InMemorySessionService`) for now. A session that must
survive a restart is a deployment decision (a database-backed `SessionService`), and the in-memory
choice is stated as a limitation rather than dressed up: a turn that is retried after a restart
re-reads its state from the workflow store and the tool layer, not from ADK's session.

### Provider Matching's sub-agents are `AgentTool`, not a `SequentialAgent`

§3 puts two sub-agents inside Provider Matching. The first composition tried was an ADK
`SequentialAgent` over `subagents=[researcher, analyst]`, and a probe with a real `Runner` showed it
cannot work as intended: a chain of `sub_agents` is only entered when the parent **transfers**
control, and after a transfer the transferred agent's answer *is* the invocation's answer — so the
parent never produces its own output, which for this role is the `MatchingOutput` decision. In the
probe the parent's turn ended after the researcher; the analyst never ran and the pool was never
written to session state.

The two sub-agents are therefore exposed to the parent as `AgentTool`s. The parent calls
`candidate_researcher`, then `constraint_analyst`, then answers. Verified end-to-end: both
sub-agents ran, both wrote their `output_key` into the parent's session (`candidate_pool`,
`constraint_analysis` — which is how the analyst reads what the researcher found, and how a resumed
turn reads the same pool), both sub-agents' tool calls went through the same gate, and all of it
charged one budget.

`SequentialAgent` itself carries a `DeprecationWarning` in ADK 2.12.0 ("deprecated in favor of
`Workflow`"). We do not use it, so the deprecation does not block us; `google.adk.workflow.Workflow`
is a graph/node API rather than a drop-in for `sub_agents`, and adopting it is a later decision with
its own ADR if the multi-agent tree grows.

**ADK node names are not card names.** ADK requires `Agent.name` to be a valid Python identifier, so
`Agent(name="referral-intake")` is refused by ADK's own validator. The node name is therefore the
card name with `-` replaced by `_` (`assembly.adk_node_name`, `subagents.protocol_name`), and the
card — the protocol identity — is unchanged. A test asserts the two spellings differ only by that
substitution, for the seven roles and the two sub-agents.

### The offline profile is chosen here, not in the runtime's factory

`MODEL_BACKEND=offline` selects `offline_model.ScriptedLlm`, a real `BaseLlm` whose answers come
from a script. `assembly.resolve_llm` checks that **first** and never calls the runtime's
`build_model`, so an offline process constructs no provider client and reads no credential.

Stated as a gap: `AgentSettings.from_env` still requires `AGENT_MODEL_ID` and a non-empty
`OPENROUTER_API_KEY`, because the runtime's factory implements one provider (ADR-0002) and the
offline profile lives in this package rather than in it. The values have to be *present*; they are
never used. Wiring the offline profile into the runtime's factory is Phase 3.5's, and it is the
first thing to fix if an offline container is ever expected to start without dummy variables.

## Consequences

* **"Built on Google ADK" is true of the loop.** A real `Runner`, real `Agent`s, real function tools
  and real session state; the models in the tests are scripted, but the loop around them is not.
* **The runtime keeps every guarantee it claims.** The allowlist, the policy authority, the turn
  budget, the retry owner, the result projection and the suspend contract are all still the
  runtime's, because the loop was inserted *between* them, not around them.
* **The agent is built per turn.** ADK fixes `tools` at construction and the tool surface is
  per-turn (budget, envelope, ledger), so `build_agent` runs per turn. The expensive object — the
  model — is shared; the agent is cheap.
* **Two things are now charged by a wrapper.** If a future ADK version calls the model through a
  path that bypasses `BudgetedLlm` (a built-in tool that summarises, say), that call is uncharged.
  The mitigation is the test that asserts the agent's model is a `BudgetedLlm` over the resolved one,
  plus the cap test that fails when the counter does not move.
* **The tool gate is synchronous and the policy backend is psycopg.** `service.build_policy` opens
  one synchronous connection for `SELECT policy_assert(...)`, which is one blocking round trip inside
  the event loop per tool call. Stated rather than hidden; moving it behind a thread or an async
  adapter changes the runtime's interface and belongs to a later phase.
* **No `DATABASE_URL` means no tool authority.** The gate refuses every call with
  `PolicyUnavailable`, which is the runtime's default-deny rule applied to a deployment mistake.
* **A sub-agent is not authority.** `AgentTool` adds a *delegation*, never a tool: sub-agents are
  built from the parent's own `ToolSet`, so their tool list is a subset of the parent's allowlist and
  there is no code path that widens it.
* **The session is per command, in memory.** A restart loses the session, not the workflow: state
  that must survive is in the workflow store and the tool layer, and the next command re-reads it.
* **A malformed answer is terminal.** No second model call is made to repair or reword it — not to
  fix JSON, and not to soften a failure message (§8). A turn that cannot answer says so.

## Alternatives considered

* **A runtime-owned turn loop.** Rejected: it reimplements tool declaration, function-call plumbing,
  multi-agent delegation and session state, and each is a place our behaviour and ADK's could drift
  without either appearing wrong. It would also make "built on Google ADK" a claim about a
  dependency rather than about the agent loop.
* **`SequentialAgent` for the two sub-agents.** Rejected on evidence (above): the parent never gets
  to answer, and the analyst never runs.
* **One `Agent` per role, built once per process.** Rejected: ADK fixes tools at construction, so a
  process-wide agent could only hold a process-wide tool surface — which is precisely the turn's
  budget, envelope and provenance ledger being shared across turns.
* **Charging the model call after it returns.** Rejected: a call that times out is still work the
  provider did, and a counter that only moves on success is a counter a runaway loop can avoid.
* **Keying the ADK session by `workflow_id` alone.** Rejected: two commands of one workflow would
  share a session, so a redelivery and a genuinely new command would be indistinguishable, and the
  §7 command ledger would be the only thing that knew the difference.
