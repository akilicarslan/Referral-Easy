# ADR-0042 — Langfuse tracing for the ADK agents

- **Status:** Accepted
- **Date:** 2026-10-23
- **Supersedes:** ADR-0010 (LangSmith tracing for ADK), which never left UNRESOLVED: no
  `LANGSMITH_API_KEY` was ever provisioned and nothing in `services/` or `packages/` imported it.
- **Depends on:** §15 (telemetry, masking before export), ADR-0002 (ADK + LiteLLM), ADR-0032
  (the OTel → OpenSearch pipeline this sits beside), ADR-0033 (one `Runner` per turn)

## Decision

LLM tracing goes to **Langfuse**, from the agent services only, through the OpenTelemetry provider
`configure_telemetry` already builds: one instrumentation, two exporters. OpenSearch keeps its copy
for §15's charts. Langfuse gets a masked, LLM-only copy for debugging and evaluation.

| Piece | Choice | Where |
|---|---|---|
| SDK | `langfuse==4.15.6` | `packages/agent-runtime/pyproject.toml` |
| ADK spans | `openinference-instrumentation-google-adk==1.0.4` | same |
| Seam | `configure_llm_tracing` / `LlmTracing.turn` / `LlmTracing.tool` | `referraleasy_agent_runtime/llm_tracing.py` |
| Wiring | `main()` after `configure_telemetry`; `_run_task` wraps each turn | `referraleasy_agent_runtime/app.py` |

**Why 4.15.6 and not the latest (5.0.4):** `google-adk==2.12.0`, the newest ADK release, caps
`opentelemetry-{api,sdk}` at `<=1.42.1`, and every `langfuse>=4.16.0` requires `>=1.45.0`. The two
cannot co-install. When ADK lifts its cap, move to 5.x. The v4→v5 guide's breaking changes do
not touch what this repo calls (`Langfuse(...)`, `start_as_current_observation`,
`propagate_attributes`, `mask_otel_spans`, `api.observations.get_many`).

## What a trace is

- **One trace per committed command.** The coordinator's turn is the root. Each specialist it
  delegates to is an `agent` observation **under the `delegate_task` call that dispatched it**.
- **One session per workflow** (`session_id = workflow_id`). A referral spans several commands,
  with a patient or a human answering in between.
- **Names are stable and verb-first:** `run-<role>` for a turn, the tool name for a tool. Evaluators
  and dashboards target observations by name.
- **Turn input** is the role's validated payload without orchestration keys. **Turn output** is the
  validated decision, or the projected result for a refusal. An `error`-class outcome is level
  `ERROR`, a protocol event is `WARNING`, and the refusal stage and reason go in the status message.
- **Tags** are roles. **Environment** is `LANGFUSE_TRACING_ENVIRONMENT`, else `APP_ENV`.
  **Release** is the runtime's installed version.
- **ADK path:** OpenInference emits `invocation` (chain) → `agent_run` (agent) → `call_llm`
  (generation, with model and token usage, reasoning tokens included) and `execute_tool` (tool).
  Sub-agents are nested `agent_run`s under their `AgentTool` call.
- **Workflow path** (no model): each governed tool attempt is a `tool` observation.
- **No user id.** OpenInference copies ADK's `user_id`, and this repo's ADK user is the workflow. It
  is dropped from the Langfuse copy rather than reported as a person.

## Measured problems, and the fixes

1. **ADK 2.12 duplicate spans.** The instrumentor suppresses ADK's native spans only on
   `start_as_current_span`. ADK 2.12 opens `invocation` and `invoke_agent` with `start_span`.
   Measured tree before the fix: the native `invocation` had no `gen_ai.*` attribute, so Langfuse's
   default filter dropped it and orphaned `agent_run`; the native `invoke_agent` was exported as a
   second agent node holding every generation. `_suppress_duplicate_adk_spans` returns a
   non-recording span with the parent's context for those names.
   `tests/agents/test_llm_tracing_span_tree.py` holds the tree and fails without the patch
   (verified by running it with the patch removed).
2. **A2A hops did not propagate trace context.** `JsonServiceClient.post_json` sent no `traceparent`,
   so every delegated specialist began its own trace: five traces for one referral where there
   should be two. It now injects the active context.
3. **The server span between caller and turn is filtered out** as HTTP noise, which left delegated
   turns as second roots. `LlmTracing.turn` parents a turn arriving over A2A to the remote caller's
   span (`trace_context`). In the OpenSearch copy the turn therefore sits beside the server span
   instead of beneath it. It is still in the same trace.
4. **Generations rendered as raw JSON.** OpenInference's `input.value` is ADK's `LlmRequest`.
   The Langfuse copy instead gets `langfuse.observation.input`/`output` built from OpenInference's
   role-labelled message attributes (OpenAI format: `tool_calls` with JSON-string arguments, `tool`
   messages with `tool_call_id`, thinking under `reasoning`). The two other copies of the same text
   (`input.value`, ADK's `gcp.vertex.agent.llm_request`/`llm_response`) are removed from that copy.

## Masking

The repo's `MaskingExporter` and the collector's `mask_pii` only see the OpenSearch copy. The
Langfuse copy is masked by `mask_otel_spans` with the same `MaskingPolicy`, **before** the chat
conversion: named identifiers are pseudonymised or redacted, and e-mails, phones and tokens are
scrubbed from prompt text. Message content itself is exported, because capturing it is the reason
for Langfuse. `OPENINFERENCE_HIDE_INPUTS` / `OPENINFERENCE_HIDE_OUTPUTS` turn that off.
**This is acceptable for the synthetic demo data only.** Real PHI needs a deployment covered by a
BAA (Langfuse's HIPAA region or self-hosting) and a stricter policy.

## Off unless configured

Tracing is active only when both keys and `LANGFUSE_BASE_URL` are set, `LANGFUSE_TRACING_ENABLED`
is not `false`, and `MODEL_BACKEND` is not `offline` (the offline profile makes zero network calls).
`scripts/validate_env.py` warns when keys are present under the offline profile. Disabled tracing
is a no-op, never an error.

## Verified (2026-10-23, Langfuse Cloud US)

- Compose stack, autonomous referral: one trace per command, specialists nested under their
  `delegate_task`, workflow tools as `tool` observations, session = workflow, environment `demo`.
- In-process provider-matching turn on `openrouter/anthropic/claude-haiku-5.5`: 8 generations
  with token usage, 2 sub-agents nested under their tool calls, no duplicate or orphaned
  observations. The failed turn's root showed `ERROR` with `output: ValidationError: … MatchingOutput`.
- `make spike` (`tests/contracts/spikes/test_langfuse_spike.py`): export plus readback through the
  public API.

## Open

- **Cost is not computed.** Langfuse's managed `claude-haiku-5-5` pattern does not match the
  OpenRouter name `openrouter/anthropic/claude-haiku-5.5`. It needs a custom model definition in
  the Langfuse project (a project setting, not code).
- **Thinking text is not returned** on this provider path: reasoning *token counts* arrive, the text
  does not. The conversion keeps it under `reasoning` when a provider sends it.
- The live stack runs in autonomous mode, so its turns are workflow-path (no model). The ADK path
  was verified in-process with `AGENT_ATTEMPT_TIMEOUT_MS=30000`. At the spec default of 3000 ms,
  the first live model call timed out.
