# ADR-0002 — Agent runtime and OpenRouter model access (google-adk + litellm)

- **Status:** Accepted (library versions pinned). The *model id* pin remains `UNRESOLVED`.
- **Date:** 2026-10-14
- **Phase:** 0 (spike S-02)
- **Decides:** `AGENT_MODEL_PROVIDER`, `AGENT_MODEL_ID`, and the runtime library versions.

## Question

Which versions of `google-adk` and `litellm` does ReferralEasy run, is the
OpenRouter (LiteLLM) integration path real in those versions, and do those versions
support the four capabilities the agent loop depends on — **tool calling**,
**structured output**, **streaming** and **cancellation**?

## What was checked

| # | Command / URL | Result |
|---|---|---|
| 1 | `curl -s https://pypi.org/pypi/google-adk/json` | latest `2.12.0`, `requires_python >=3.11` |
| 2 | `gh api repos/google/adk-python/releases/latest` | `v2.12.0`, published `2026-10-13T16:35:19Z` |
| 3 | `curl -s https://pypi.org/pypi/litellm/json` | latest `1.105.1`, `requires_python <3.15,>=3.10` |
| 4 | `gh api repos/google/adk-python/contents/src/google/adk/models?ref=v2.12.0` | `lite_llm.py` present (4,338 lines) |
| 5 | `gh api repos/BerriAI/litellm/contents/litellm/llms/openrouter?ref=v1.105.1` | `chat/transformation.py`, `embedding`, `responses` — OpenRouter is a first-class LiteLLM provider |
| 6 | `curl -s https://pypi.org/pypi/google-adk/2.12.0/json` (`requires_dist`) | extras pull `a2a-sdk[http-server]<2,>=0.3.4` and `litellm>=1.84` |
| 7 | source inspection of `lite_llm.py` @ `v2.12.0` | see capability table below |

## Decision

- **`google-adk==2.12.0`** — CONFIRMED (PyPI + upstream release tag agree).
- **`litellm==1.105.1`** — CONFIRMED (PyPI latest).
- **OpenRouter integration path** — `google.adk.models.lite_llm.LiteLlm(model="openrouter/<vendor>/<model>")`.
  The string `openrouter` appears **nowhere** in ADK's `lite_llm.py`: ADK does not
  special-case the provider at all. It delegates to LiteLLM, and LiteLLM owns the
  `openrouter/` provider prefix (provable: `litellm/llms/openrouter/chat/transformation.py`
  exists at tag `v1.105.1`). Credentials go in the standard env var
  `OPENROUTER_API_KEY`, which LiteLLM reads itself — ADK passes no key.
- **`AGENT_MODEL_PROVIDER=openrouter`** — unchanged from `.env.example`.
- **`AGENT_MODEL_ID=CHANGEME_TOOL_CAPABLE_MODEL`** — **UNRESOLVED.** See below.

### Capability support in `google-adk==2.12.0` + `litellm==1.105.1`

| Capability | Verdict | Evidence |
|---|---|---|
| Tool calling | **SUPPORTED** | `lite_llm.py` handles both structured `tool_calls` and the inline-JSON fallback that some providers emit (`"LiteLLM responses either provide structured tool_calls or inline JSON, not both"`, ~line 2459), and maps `finish_reason` values. |
| Structured output | **SUPPORTED** | `_LITELLM_STRUCTURED_TYPES = {"json_object", "json_schema"}` plus a converter that turns an ADK `response_schema` (`types.Schema` or Pydantic model) into a LiteLLM structured-output format; it raises `Unsupported response_schema type %s for LiteLLM structured outputs.` otherwise. |
| Streaming | **SUPPORTED** | Dedicated streaming paths (`_aggregate_streaming_thought_parts`, `_model_response_to_generate_content_response` over stream chunks, chunk-level `delta` vs full-object handling). |
| Cancellation | **NOT VERIFIED — absent from the source** | `grep -n -i "cancell\|aclose\|GeneratorExit"` over `src/google/adk/models/lite_llm.py` **and** `src/google/adk/models/base_llm.py` at `v2.12.0` returns **zero matches**. There is no cancellation hook in the model layer. Whether cancelling the async iteration actually aborts the in-flight HTTP request is an empirical question. |

## Why `AGENT_MODEL_ID` stays `CHANGEME`

`https://openrouter.ai/api/v1/models` currently lists **462** models; **342** of them
advertise both `tools` and `structured_outputs` in `supported_parameters`. That is a
**catalogue claim, not a verified call**, and catalogue entries are added and retired
continuously. Pinning one of those ids here would be exactly the failure mode
`.env.example` warns about: presenting an unverified string as real configuration.

Resolving it requires an `OPENROUTER_API_KEY` and running the spike below, which
verifies tool calling, structured output, streaming and cancellation **against the
candidate id** rather than against a marketing page.

## What remains unverified

1. **ADK 2.12.0 + litellm 1.105.1 is an untested pairing.** ADK declares `litellm>=1.84`
   **with no upper bound**, so `pip`/`uv` will happily install `1.105.1` — but ADK 2.12.0
   was published on 2026-10-13 and LiteLLM releases continuously. "Both versions exist" is
   *not* "these versions work together". The spike is what closes that gap.
2. **Cancellation must be measured.** No source-level support was found; the spike asserts
   it behaviourally (elapsed-time bound plus a `CancelledError` assertion).
3. **OpenRouter tool-call behaviour through LiteLLM's normaliser** — LiteLLM re-shapes
   provider responses, and ADK re-shapes LiteLLM's. Two normalisation layers means the
   `arguments` JSON must be validated end-to-end, not assumed.
4. **`custom_llm_provider` / prefix routing** — ADK accepts an explicit
   `custom_llm_provider`; whether an explicit value is needed for OpenRouter (rather than
   relying on the `openrouter/` prefix) is unverified.

## Consequences

- The runtime layer is pinned and importable today; nothing in Phase 0 depends on the
  model id being resolved, because the default profile is `MODEL_BACKEND=offline`.
- The spike `tests/contracts/spikes/test_adk_openrouter_spike.py` fails loudly (naming
  `OPENROUTER_API_KEY` and `AGENT_MODEL_ID`) until both are real.
- When the pin lands, this ADR is amended with the dated result and the four capability
  assertions, not merely the version numbers.
