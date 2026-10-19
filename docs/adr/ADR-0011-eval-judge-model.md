# ADR-0011 — Evaluation judge model

- **Status:** **UNRESOLVED — needs credentials**, and beyond credentials needs a
  judge-validation run. No model id is pinned.
- **Date:** 2026-10-14
- **Phase:** 0 (spike S-11)
- **Decides:** `EVAL_JUDGE_MODEL_ID` (and the provider it runs on).

## Question

Which model judges the evaluation suite, and why that one?

## What was checked

| # | Command / URL | Result |
|---|---|---|
| 1 | environment inspection | no provider credentials (no `OPENROUTER_API_KEY`, no Vertex); the judge cannot be called |
| 2 | `curl -s https://openrouter.ai/api/v1/models` (2026-10-14) | 462 models; **342** advertise both `tools` and `structured_outputs`; **272** of those also advertise `reasoning` |
| 3 | Python filter over that snapshot: reasoning + tools + structured output, non-router/non-free, newest first | produced the candidate table below |
| 4 | `.env.example` | `EVAL_JUDGE_MODEL_ID=CHANGEME_JUDGE_MODEL` |

## Decision

- **`EVAL_JUDGE_MODEL_ID=CHANGEME_JUDGE_MODEL`** — unresolved, deliberately.
- **Provider: OpenRouter** (`AGENT_MODEL_PROVIDER` path), because that is the one provider
  route with a verified generic client (ADR-0002). A judge that needs a second, unverified
  provider integration would multiply the Phase 0 unknowns.

### Candidate models, with the rationale for each

All prices are USD per token from the catalogue snapshot above; `in`/`out` are the
prompt/completion prices. None of these is a pin — they are the shortlist the spike will
test, and the id must resolve at run time.

| Candidate | in / out | Why it is on the list |
|---|---|---|
| `anthropic/claude-sonnet-5.5` | 2e-6 / 10e-6 | Mid-tier frontier model — the usual price/quality point for automated judging; different family from the Gemini agent model, which limits self-preference bias. |
| `openai/gpt-6.1-sol` | 2e-6 / 10e-6 | Same tier and reasoning capability; distinct third family. Useful as the *second* judge for an agreement check. |
| `google/gemini-4-argon` | 2e-6 / 10e-6 | Strongest on the shortlist, and **same family as the agent model** — recorded specifically as a *negative* control: judging a model with its own family is exactly the bias this ADR wants visible. |
| `mistralai/mistral-large-4-0` | 6.8e-7 / 2.09e-6 | ~3× cheaper; the candidate if the judge is used at high volume rather than at the 1% sample rate. |
| `qwen/qwen3.8-max-prime` | 4e-6 / 12e-6 | Different lab and training lineage again; a defensible cross-family alternative. |

**Cost note.** At `EVAL_SAMPLE_RATE=0.01` with `EVAL_CRON=0 0,12 * * *` (twice daily), even the
most expensive candidate costs well under a dollar a day for this demo's traffic. Cost is
therefore **not** the deciding constraint — judge *reliability* is.

### Selection rule (fixed now, applied when credentials exist)

1. The judge must **not** be the agent's own model family, where a comparable alternative
   exists — self-preference bias is a measured, documented effect, and picking a judge is
   the one place the demo can control for it cheaply.
2. The judge must support **structured output** (a judge that returns prose cannot be
   parsed reliably) — verified per candidate from `supported_parameters`, not assumed.
3. The winner is chosen by **agreement with human labels** on a held-out slice of the eval
   dataset, not by which model "seems smarter". Target: record the agreement rate and the
   judge's disagreement cases; if two candidates agree with humans within noise, take the
   cheaper one.
4. The judge's **prompt and rubric are versioned artifacts**; changing either invalidates
   the agreement measurement above and requires re-measuring.

## What is needed to resolve it

1. `OPENROUTER_API_KEY`.
2. `EVAL_JUDGE_MODEL_ID` set to a candidate whose id resolves at run time.
3. A labelled slice of the eval dataset (human or hand-adjudicated) to measure agreement
   against — **this is the real dependency**, not the API key. A judge validated only
   against itself proves nothing.
4. Spike behaviour: with the key absent it fails naming `OPENROUTER_API_KEY`; with the key
   present and `EVAL_JUDGE_MODEL_ID` still `CHANGEME_JUDGE_MODEL` it fails naming
   `EVAL_JUDGE_MODEL_ID` and printing this shortlist.

## What remains unverified

1. **Whether these catalogue ids resolve for this account** — the catalogue is global;
   account entitlements and credit balance are per-account (`402` is a documented response).
2. **Quality.** No candidate has been measured against human labels; the table above is
   sourced pricing and capability metadata, **not** a quality ranking.
3. **Structured-output fidelity** per candidate — advertised capability is not a guarantee
   that a given schema validates.
4. **Determinism**: judge stability across repeated identical calls (temperature handling,
   provider-side nondeterminism) is unmeasured, yet the eval suite's comparability depends
   on it.
5. **Retirement risk** — judge model ids are retired like any other; `EVAL_JUDGE_MODEL_ID` is
   a pin with an expiry, and the ADR must be re-dated when it moves.

## Consequences

- The demo can state honestly that it has **no** eval judge today, rather than shipping a
  judge id nobody validated.
- The selection rule is written down *before* the candidates are tested, which is the only
  way it constrains anything.
