# The eight demo scenarios

These are §20's eight portfolio scenarios. Each one exists to demonstrate a named technology on a
path a real user would take, and each is designed to be **reproducible** — same fixtures, same
faults, same observable behaviour, every run.

**The source of truth for this document is [`fixtures/scenarios.json`](../../fixtures/scenarios.json).**
It carries, per scenario, the exact fixture IDs (`patients`, `hospitals`, `slots`, `plans`,
`coverage_results`, …), the `fault_scenarios` that inject the failure, the `expected_result_kinds`,
and the `assertions` that decide whether the scenario actually happened. This page is the narrative;
that file is the data. Where the two could disagree, the file wins — regenerate this page's tables
from it rather than editing them by hand.

Everything here runs on **synthetic data** (§1). No scenario touches a real patient, hospital or
insurer.

## Status at a glance

The scenarios are only as real as the phases behind them, so this column is deliberately blunt.

| # | Scenario | Technology it demonstrates (§20) | What exists today |
|---|---|---|---|
| 1 | Standard booking | ADK, tool schemas, A2UI/SSE | fixtures, faults, scripted e2e; **agent + gateway + UI path not yet wired end to end** |
| 2 | Multi-constraint search | Multi-agent collaboration, sub-agents, skills | fixtures, faults, skills; **sub-agents in progress** |
| 3 | Network conflict | Evidence, coverage agent, guardrails | fixtures, faults; **coverage agent + guardrails in progress** |
| 4 | Missing document | Readiness, suspend/resume, context | fixtures; **readiness agent in progress** |
| 5 | Timeout after commit | Idempotency, reconciliation, distributed state | fixtures, faults, **e2e proven** (`tests/e2e::test_a_timeout_before_commit_...`) |
| 6 | Three transient errors | Retry, circuit breaker, RAG/rerank | fixtures, faults, **RAG ladder proven**; rerank is 404-blocked |
| 7 | No specialist found | HITL, ownership, audit | fixtures, faults; **HITL workers in progress** |
| 8 | Version regression | Registry, eval platform, OTel/OpenSearch | **fixtures only — registry and telemetry in progress** |

Nothing in the "not yet wired" column is a claim that it works. When a scenario is provable, this
table gains the trace or test that proves it, and the README's technology table cites it.

---

## 1 — Standard booking

> Patient picks hospital, then doctor, then a slot, then approves. One receipt. The straight-through
> path with no conflicts and no faults except the ones injected deliberately.

**Demonstrates:** ADK agents, tool schemas, A2UI components over SSE.

The ordering rule from §4 is the substance here, not the happy path: the patient sees **hospitals
first, then doctors at the chosen hospital, then slots**, and the system **never auto-selects a slot
and never pre-books**. A component that lists doctors before a hospital is chosen is a defect, not a
shortcut.

**Faults:** `notification_failure.json`, `stale_slot_409.json` — the booking must survive both.
**Expected result kinds:** `slots_found`
**Assertions:** 4

## 2 — Multi-constraint search with no result

> Language and weekday and a 15 mile radius together yield no bookable slot. Alternatives are
> offered with evidence, and the hard distance constraint is never widened automatically.

**Demonstrates:** multi-agent collaboration, the two sub-agents inside Provider Matching, skill
packages.

This is the constraint-negotiation scenario. Language and weekday are preferences that a patient may
trade; **distance is a hard constraint and is never relaxed silently** (§2.1). The interesting
output is not "no results" but the *evidence-backed alternatives* — a wider radius, another day,
another hospital — each of which the patient must choose.

**Faults:** `empty_valid_result.json` — an empty result is a **valid** result, not an error, and is
never retried (§8). Getting that distinction wrong turns a good answer into a retry storm.
**Expected result kinds:** `no_slots_for_constraints`, `no_matching_provider`, `slots_found`
**Assertions:** 5

## 3 — Network conflict shown as unknown

> The provider accepts the insurance in general while the plan-level network record says unknown, and
> a hospital's public description contradicts its own structured record. Both are shown as unknown
> with freshness, never resolved into a confident answer.

**Demonstrates:** evidence handling, the coverage agent, guardrails.

The whole scenario is one rule: **`unknown` is not `false`.** A plan-level record that says "not
verified" must not be rendered as out-of-network, and a structured record must not be overridden by
prose from a hospital's public description (§10). Both surfaces carry their freshness so a stale
record can be told from a fresh one.

**Faults:** `stale_coverage_evidence.json`
**Expected result kinds:** `slots_found`
**Assertions:** 6

## 4 — Missing document, then one re-check

> A required prior report is absent, the workflow stops, the synthetic document arrives, and only the
> one failed check re-runs while the checks that already passed keep their original result and run id.

**Demonstrates:** referral readiness, suspend/resume, context separation.

The precision matters: **only the failed check re-runs.** Re-running the checks that already passed
would re-issue their run ids and quietly invalidate the evidence the earlier decision was based on.
The suspension is a real one — the workflow stops without holding an open model call (§5).

**Faults:** none
**Assertions:** 5

## 5 — Timeout after commit

> The hospital commits the booking and the response is lost. The outcome is unknown, so no second
> booking may be created and reconciliation must be explicit.

**Demonstrates:** idempotency, reconciliation, distributed state.

The sharpest rule in the project, stated in §7 in one line: **a second independent booking key is
never generated.** The result is `outcome_unknown`, which the A2A adapter maps to protocol state
`failed` (terminal) *while* reconciliation is in flight — and that protocol state must never be read
without the application outcome beside it. Reconciliation calls `lookup_request` with **the same
key** until it resolves or escalates.

**Proven today:** `tests/e2e/test_scripted_booking.py`, against the real mock API, in both
directions — `test_a_timeout_after_commit_is_resolved_by_reconciling_the_same_key` and
`test_a_timeout_before_commit_resolves_to_not_booked`. The second asserts that no **confirmed**
booking exists, not that no booking row exists: the client's own record of having tried is expected,
and "not committed" is a claim about the hospital, not about whether the attempt was written down.

**Faults:** `timeout_after_commit.json`, `timeout_before_commit.json`, `duplicate_event.json`
**Assertions:** 5

## 6 — Three transient errors

> A timeout, a 429 and a 503 in a row. Retries are bounded, the directory falls back to its static
> seed copy, and an upstream failure is reported as a failure rather than as an empty result.

**Demonstrates:** retry policy, circuit breaker, hybrid RAG and its degradation ladder.

Two distinctions this scenario exists to enforce: **an upstream failure is not an empty result**
(an empty result is a legitimate answer; a failure is not), and **retries are bounded** — the third
transient failure is a one-way door to recovery, not another attempt (§3).

**Proven today:** `packages/rag`'s degradation ladder descends
`HYBRID_RERANK → HYBRID_RRF → BM25_ONLY → SEED_FALLBACK → HUMAN_HANDOFF`, and `ALLOWED_TRANSITIONS`
makes it monotonic — it cannot climb, and a seed descent after a model call is a `LadderViolation`
that a test asserts. Every rung is reached by the real failure that causes it, not by a flag:
reranker unavailable, embeddings down, OpenSearch unreachable (both via a raising client and via a
**real refused connection**), then the versioned static seed, then HITL. The seed rung is asserted
to make **zero model calls** and to be deterministic with sockets blocked at process level.

**Rerank is not proved.** `cohere/rerank-v3.5` returns HTTP 404 from OpenRouter, so the live
ordering effect of reranking is unmeasured; what is proved is the fallback path.

**Faults:** `latency.json`, `rate_limit_429.json`, `service_unavailable_503.json`,
`malformed_schema.json`
**Expected result kinds:** `upstream_error`, `slots_found`
**Assertions:** 4

## 7 — No specialist found

> The requested specialty does not exist anywhere in the directory. The workflow offers a human queue
> with evidence, an operator claims the ticket, and the patient returns to the same state with final
> approval still required.

**Demonstrates:** human-in-the-loop, ownership, audit.

This is §11 end to end. The load-bearing detail is the last clause: **resolving a ticket is not
booking consent.** An operator can add alternatives and notes and return the workflow to a safe
state, but the patient's final approval is still required — and while a human owns the workflow,
**automation writes are suspended** (RE003). If no operator is available the UI shows a pending
ticket and directory contact; it **never pretends a live agent was reached.**

**Faults:** none
**Expected result kinds:** `no_matching_provider`
**Assertions:** 5

## 8 — Version regression

> A change to an agent description or skill must be caught by a version check in CI and shown on an
> old-versus-new evaluation chart. This scenario owns no clinical fixtures; its fixture contribution
> is a pinned, deterministic input set so that two runs are comparable.

**Demonstrates:** the versioned registry (§13), the evaluation platform, OTel/OpenSearch.

The only scenario with **no clinical fixtures**, by design. Its content is a *pinned input set*
(`pinned_inputs`, `knowledge_version_pins`) so two runs are comparable, plus the CI rule that any
change to an agent description, instruction, tool or skill changes that agent's artifact hash — and
**CI fails if an affected agent's version did not increase.** Even a PATCH is a behaviour change and
triggers evaluation.

**Non-fixture dependencies:** the evaluation dataset, the two agent versions being compared, and the
chart that plots one against the other.

**Faults:** none
**Assertions:** 4

---

## What "reproducible" is going to mean

§21's definition of done is stricter than "the demo runs": **all eight scenarios reproducible, each
showing a real task, trace and result.** When this work is complete, every row above gains:

- a **task id** from the real A2A call,
- a **trace** in OpenSearch from the real run,
- and an **evaluation result** naming the release it evaluated.

Until then the table's third column is the honest answer, and no scenario is described as working on
the strength of its fixtures alone.
