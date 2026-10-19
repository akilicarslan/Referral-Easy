# ADR-0038 — The three-layer input guardrail and the output guardrail: measured, closable, and wired

- **Status:** Accepted
- **Date:** 2026-10-15
- **Phase:** Phase 4.2 and 4.3 (§4.2, §4.3, §9, §12 wire contract; §18 service boundaries)
- **Depends on:** §9 (the three layers, the two profiles, the closed-dependency rule, the per-layer
  measurement), §4.2 (input guardrails), §4.3 (output guardrails), §12 (`ComponentInstance`),
  ADR-0002 (OpenRouter as the provider path), ADR-0005 (the local guardrail classifier and its
  threshold record), ADR-0036 (the wiring conventions this phase follows), ADR-0037 (the registry
  gate, whose numbering this file follows rather than reuses)

## Context

§9 names three input layers — **regex**, an **OpenRouter decision model on a masked message**, and a
**local small semantic model** — with a fixed order, one short-circuit rule, a per-layer measurement
requirement, and a rule that is a *product* rule rather than an engineering one: *"Booking writes
are prohibited while a guardrail dependency is unavailable"*, with a static guide card staying
available anyway. §4.3 adds the other
side: the output is schema-valid, its entity ids exist, its times are real, its contacts are sourced,
and it carries no coverage promise and no clinical language.

The repository before this phase had:

- a **gateway-side** `referraleasy_gateway.guardrails` with a real layer 1 (patterns, normalisation,
  length cap, the two state-aware exceptions), a pipeline with §9's order and short-circuit, and
  *unconfigured* layers 2 and 3 that raise rather than answer. Nothing behind them, because §8's model
  factory is Phase 3's and the classifier's artifact is Phase 4's;
- **no** measurement of anything: the pipeline recorded verdicts and latencies in memory and no corpus
  existed against which false positives, false negatives, added latency or added cost could be
  computed;
- **no** second profile, so §9's *"the conditional profile is compared against the three-stage
  profile"* had nothing
  to compare;
- **no** output guardrail at all;
- and **no** answer to the question the corpus exists to force: which layer actually catches what, and
  at what price to a patient with a genuine request.

Phase 4.2/4.3 is packages and tests only: `packages/guardrails/**` and `tests/guardrails/**`. The
gateway keeps its own wiring; this phase does not edit `services/gateway/**`.

## Decision

### One package, three layers, one contract

`packages/guardrails` holds the layers (`layer_regex`, `layer_decision_model`, `layer_classifier`),
the orchestrator (`pipeline`), the environment contract (`config`), the assembly function the wiring
calls (`build`), the measurement harness (`evaluation`), the output guardrail (`output`), and the
committed data the measurement is taken over (`data/`).

§9's layer 1 is **re-implemented here rather than imported from the gateway**, and the duplication is
temporary and measured: `tests/guardrails/test_guardrail_layer1_parity.py` compares the two
implementations verdict for verdict over every corpus message, and they agree everywhere except the
wording of an emergency reason. The gateway's copy is deleted with the wiring (this phase may not edit
`services/gateway/**`).

§9 reasons in prose where the code needs facts, so three of them are made explicit and tested:

- **The verdict never carries a `method` out of thin air.** Layer 1 declares `METHOD = "regex-v1"`
  because the *layer* has a method; layers 2 and 3 report the model or the artifact actually used
  (`openrouter:<model>`, `hashed-char-ngram-v1`, `minilm-onnx:<sha256 prefix>`). A number attributed to
  "layer 3" without its method would attribute a lexical matcher's operating point to the pinned
  encoder.
- **A similarity is not a probability.** Layer 3 stores the cosine similarity it computed and every
  surface reports `score_is_calibrated_probability: false`. §9 asks for a threshold; a threshold is not
  a calibration and the report says so where the number is.
- **Input screening is not the only defence against prompt injection.** It is the *first* one. The
  layer's own module docstring states the rest: the model's reply is parsed into a closed vocabulary
  and never executed, never forwarded and never concatenated into another prompt, so a successful
  injection against the model still cannot do anything — and an unparseable reply is *not a decision*
  rather than an allow.

### The seam is structural, and it is exercised in both directions

The gateway's pipeline reads `layer.name`, calls `layer.screen(request, masked_text)` and copies
`layer / decision / category / reason / latency_ms / response`. Everything in `types.py` is named so
that a verdict produced here can be handed to that pipeline unchanged, and
`tests/guardrails/test_guardrail_seam.py` proves it three ways: the gateway's pipeline drives these
three layers to the **same decisions** as this package's pipeline over the whole corpus; the gateway's
own wrapper pair accepts exactly the `screen | None` pair `LayerSet.screens()` returns; and the refusal
that leaves a closed layer is **the gateway's own exception class**, resolved at runtime by
`raise_unavailable`, because the class that closes the write path is the one the catching pipeline has
an `except` clause for. A different-but-equivalent class would surface as an unhandled `500` instead of
§9's `503`.

That test found a real defect on this side: the pipeline copied `method`, `similarity` and `decisive`
off the verdict it received, and a verdict built by the gateway's wrappers has none of them — the
mixed configuration raised `AttributeError` in the middle of screening. The three reads are now
defensive (`getattr` with the safe default), for the same reason layer 2 reads `open_question`
defensively: the gateway builds its own `ScreeningRequest`, which carries four facts and no question.

Dependency direction is enforced rather than documented: only `types.py` mentions
`referraleasy_gateway`, the import is inside `raise_unavailable`, and a subprocess asserts that
importing the package imports no service.

### The closed state: the write is refused, the card stays, the emergency still routes

§9 puts two things in one sentence. Both are concrete:

- `LayerSet.closed()` is the single fact `/healthz`, the refusal message and the measurement report
  read;
- `GuardrailPipeline.require_complete(outcome)` raises `InputPathClosed`, naming the missing layers,
  whenever a layer could not answer — and an unavailable layer **never leaves an `allow`**: this
  package's pipeline turns an incomplete screening into `clarify`. (The gateway's pipeline does not; see
  "What still does not work".)
- `LayerSet.closed_card()` returns a real §12 `HumanHandoffCard` with no action, no entity id, no time,
  no booking state and `workflow_state_version: 0`, whose `instance_id` is a pure function of *which*
  layers are missing, so re-rendering replaces the same slot. It passes the real output guardrail.
  When nothing is closed it returns `None`: a help card shown to every patient would be a UI change
  dressed as a safety measure.
- An emergency phrase is answered with the approved fixed response even when every layer behind layer 1
  has nothing behind it. The refusal that message gets is the *decision's*, not the closure's, and the
  two are asserted separately.

### Two profiles, and the comparison §9 asks for instead of an assumption

`three_stage` runs all three layers. `conditional` skips the layers after a layer-1 verdict that is
**decisive by construction** (an anchored pattern that matched the whole message, an explicit attack,
an emergency, an empty or oversized message) — a property of the message, not a bet about the layer
that would have run. A closed layer on the skip path is still recorded `unavailable`: skipping a call
must never turn a closed layer into a complete screening.

Measured over the labelled corpus, both profiles agree: **`decisions_changed == 0`**, `{block: 14,
clarify: 20}`, `complete: 11`, `writes_refused: 23`, `false_positives: 0`, `attacks_allowed: 0`, with 6
skipped layer-3 calls. §9 asks for the conditional profile to be compared rather than assumed better,
and on this corpus the comparison is what says so.

### The measurement, and what it is allowed to claim

`evaluation.measure` runs a deployment over the committed corpus (`labelled-corpus-v1`: 35 cases, 34
user messages, calibrated on 16 and held out on 18) and reports per layer: false positives, false
negatives, silent allows, over-asks, attacks not blocked, added latency, provider calls and cost. The
tests **recompute every per-layer count independently** from the corpus and require equality; they also
require that the corpus carries more than one label, that at least one attack is missed by some layer,
and that a rate nobody measured is reported as `None` rather than `0`.

Numbers, offline profile, layer 3 on the named offline rung:

| Layer | FP | FN | silent allows | over-asks | attacks not blocked | mean latency |
| --- | --- | --- | --- | --- | --- | --- |
| 1 `regex-v1` | 0 | 4 | 1 | 0 | 4/12 | 0.037 ms |
| 2 `openrouter:<model>` | *not screened* — `MODEL_BACKEND=offline` means zero network calls | | | | | |
| 3 `hashed-char-ngram-v1` | 0 | 1 | 4 | 10 | 4/12 | ~1.0 ms (0.96–1.00 across runs) |

Layer 2's line is the honest one: an offline profile has no provider call to measure, so its rate
fields are `None` and its `provider_calls` is 0. Both profiles refuse every corpus attack and refuse
no benign case, and the report says which layers were measurable rather than inventing numbers for the
ones that were not. The whole corpus screens in about a millisecond on this hardware; a first-touch
warm-up (63–94 ms for the first layer-2 call) is discarded before the timed pass so it is not reported
as per-case latency.

The profile comparison, as printed by the report: the conditional profile made **17** layer-3 calls
where the three-stage profile made **23** — a delta of **−6** — with `decisions_changed: 0` and a mean
of **0.4987 ms** per case against **0.6534 ms**. Layer 1 was called 34 times in both.

### The threshold record, and the offline rung

`data/threshold_record.json` carries threshold 0.32, margin 0.08, the method, the corpus version and
content hash, the reference-set hash, both partitions' counts, and the latency corpus. Layer 3 is
**configured** only when an encoder is present, the record matches the loaded data, and the environment
threshold equals the recorded one — otherwise it is closed with the reason. `artifact_hash` is empty
for the offline rung *because there is no artifact*: the rung is a deterministic hashed character
ngram, named in the environment as `GUARDRAIL_LOCAL_MODEL=offline/hashed-char-ngram-v1`, and nothing is
substituted for the pinned encoder silently.

§9's sentence about a token that is not a resolved model id is enforced at startup:
`GUARDRAIL_DECISION_MODEL=CEV` (and `cev`, `CHANGEME*`, `tbd`, a vendor-less `gpt-5-nano`) is refused,
`MODEL_BACKEND=live` with an empty model or an empty key is refused, every problem in one configuration
is reported together, and **a credential's value is never printed** — a `repr` that echoed the key or
the resolved environment was a key in a log line, so both fields are excluded from it.

### The two maskers, and why the floor moved

The masker's digit floor was nine and is now seven: the member ids the directory hands out and the
corpus carries are seven digits, so the nine-digit floor sent exactly the identifier the masking exists
to remove. This was found by a test, not by reading, and the same floor is used by layer 2's
"is the masking holding?" check — the two had agreed with each other about the wrong number. The
gateway's masker still uses nine, and layer 2 **refuses to send** a message that still carries an
identifier rather than masking it again, so the difference is visible as a refusal rather than as a
leak. The wiring must adopt one masker for both sides.

### The output guardrail

`output.OutputGuardrail` checks seven things and reports which ones it ran: schema, entity ids, times,
sourced contacts, booking state, coverage language, clinical language. It takes a three-question oracle
port (`entity_exists`, `slot_time`, `booking_state`) rather than a database handle, so a check cannot
grow a query per field. A phone number or an address on a card must be **sourced** — present in the
payload's sources — not merely well-formed; a booking state must be a state the workflow actually has;
coverage and clinical language are named refusals rather than a sentiment score.

### The live path, and one defect it found

`tests/guardrails/test_guardrail_live_spike.py` is the only place in this phase that spends money. It is
marked `spike` (deselected by default, `addopts = -m 'not spike and not e2e'`) and additionally gated
on `GUARDRAIL_LIVE_SPIKE=1`; once that flag is set, a missing key or an unresolved pin **fails** rather
than skipping, because at that point the operator believes a live run happened.

Its first run found that the pinned model is a **reasoning** model and that the client's completion
budget was sized for the answer rather than for the model: `max_tokens: 200` was consumed entirely by
192 reasoning tokens, the reply came back `finish_reason: length` with `content: null`, and the layer
correctly closed the input path — which, in a live deployment, would have refused **every** message.
The budget is now the named constant `MAX_COMPLETION_TOKENS = 2048`, and a truncated reply raises a
reason that names the budget and the reasoning breakdown instead of "not a chat completion", which
would have sent an operator looking at the transport. The payload is reproduced in the offline
transport tests.

Measured live on 2026-10-15 against `openai/gpt-5-nano` through OpenRouter, with the masking applied:
a benign appointment request answered `allow / benign` in **5.39–5.44 s**, 476 prompt tokens and
350–384 completion tokens (320+ of them reasoning), **$0.000164–$0.000179** per screening.

## Consequences

### What was verified, and with which commands

```
uv run --no-sync ruff check packages/guardrails tests/guardrails
    → All checks passed!
uv run --no-sync mypy packages/guardrails/src
    → Success: no issues found in 12 source files
uv run --no-sync pytest tests/guardrails -q
    → 318 passed, 2 deselected in 1.52s        (the 2 are the live spike)
GUARDRAIL_LIVE_SPIKE=1 uv run --no-sync pytest tests/guardrails/test_guardrail_live_spike.py -m spike -q -s
    → 2 passed in 17.13s                       (two live calls, ~$0.0003)
uv run --no-sync pytest -q                     (whole repository, after the package changes)
    → 2684 passed, 187 skipped, 46 deselected in 306.43s
```

The three warnings are pre-existing. The 187 skips are the worker suites, which need a real
PostgreSQL (`TEST_DATABASE_URL`), and the 46 deselections are the spikes and e2e tests.

Everything except that last command is offline: no provider, no artifact, no database. Layer 2 is
exercised through an injected client and over `httpx.MockTransport`; layer 3 through a deterministic
encoder and, separately, through the committed rung.

### What still does not work, and whose files are in the way

- **`packages/guardrails` is not a workspace member.** It is importable only through the `sys.path`
  bridge in `tests/guardrails/guardrail_fixtures.py`. Two lines in the root `pyproject.toml`
  (`[tool.uv.workspace] members` + `[tool.uv.sources]`), one in the `Makefile`'s `PYTHON_SOURCE_ROOTS`,
  and the ruff `src` list remove the bridge. That wiring is the parent's, not this phase's.
- **The gateway still ships its own layer 1.** It is byte-for-byte compatible on the corpus and the
  parity test is the evidence; deleting it and its parity test is one edit in a file this phase may not
  touch, together with the `mask_pii` floor.
- **The gateway's pipeline leaves an `allow` when a layer is unavailable.** Its `decision` stays at the
  `allow` it started with while `complete` is `False`, so a caller reading `decision` alone reads
  permission; the write path is still safe because `require_complete` refuses. This package's pipeline
  converts the same outcome to `clarify`. The wiring must either adopt that rule or gate every read on
  completeness — this is the single most important integration item in this ADR.
- **The gateway's outcome can explain a `clarify` with an allowing layer's reason.** Its `reason` is the
  last verdict of any kind, while its `category` comes from the last non-allow verdict. Both are pinned
  by tests in `test_guardrail_seam.py`; the fix is in the gateway's file.
- **This checkout's own configuration closes layer 3.** `.env` sets `GUARDRAIL_LOCAL_THRESHOLD=0.50`
  while the record measured 0.32, and the pinned encoder's ~23 MB artifact is not present. That is the
  closed state working as designed, not a defect — but a live demo run has to resolve which one it
  wants: the offline rung at 0.32, or the encoder artifact with `.env` fixed.
- **The layer-2 timeout is not a total budget.** `GUARDRAIL_TOTAL_TIMEOUT_MS` (default 3000) is passed
  to httpx as a per-phase timeout, and a live screening measured at 5.4 s still returned. If the
  product needs a hard wall-clock bound on a screening, the caller has to enforce it.
- **OpenRouter can refuse a prompt-injection call before the model runs.** The corpus injection returned
  the prose `"I'm sorry, but I cannot assist with that request."` with `finish_reason` unreported and
  usage reported as **zeros** (0 prompt tokens, $0.0). The layer correctly treats prose as "not a
  decision" and closes the input path — never an allow — but the operator-facing outcome for that
  message is a closure rather than `block / prompt_injection`, and a per-layer cost computed from that
  call would record a reported zero as if it were the cost. Both facts are worth a decision: another
  model, or an explicit mapping for provider-level refusals.
- **`services/gateway/**` still has no client for layer 2 and no artifact loader for layer 3.** This
  phase ships the layers, not the composition into the running service.

### Divergences recorded (files this phase may not edit)

- `services/gateway/src/referraleasy_gateway/guardrails.py` — the duplicate layer 1, the nine-digit
  masker floor, the allow-with-unavailable rule, the reason-from-the-last-verdict rule.
- The root `pyproject.toml` and `Makefile` — workspace membership and the mypy source roots.
- `.env` — the 0.50-vs-0.32 threshold and the missing artifact.

## Alternatives considered

**Importing the gateway's layer 1 instead of re-implementing it.** Rejected: a package that imports a
service inverts the dependency direction this repository keeps (`services → packages`), and the
gateway's layer 1 is deletable only by its owner. The duplication is paid for by a parity test over the
whole corpus, and the ADR names the deletion as the wiring's job.

**A model response that is not parseable treated as an allow, or as a heuristic `block`.** Rejected:
the first is the bypass §9 is written against, and the second invents a mapping from free text to a
category that no layer was measured on. An unparseable reply closes the input path, which is safe and
honest.

**Running the conditional profile as the default because it is faster.** Rejected — and this is what
§9 asks for: the profile is *compared*, and the comparison on this corpus found `decisions_changed == 0`
with 6 skipped calls. That is a measurement, not a licence; the three-stage profile stays the default
and the showcase.

**Reporting layer 2's numbers as zero under the offline profile.** Rejected: zero false positives for a
layer that never ran is a fabricated measurement. The line is *not screened*, the rate fields are
`None`, and the tests assert the difference between "not measured" and "measured as zero".

**Sizing the completion budget to the JSON object, as the first version did.** Rejected by measurement:
the pinned model spends more tokens reasoning than answering, and the live call proved the budget
truncated before the answer existed.

**Making the live spike mandatory (fail instead of skip without the flag).** Rejected: unlike the
Phase 0 pins, the offline profile is a complete configuration, and a spike that spent money on every
`make spike` would be a worse demo. The flag makes the run deliberate; once set, missing prerequisites
fail.
