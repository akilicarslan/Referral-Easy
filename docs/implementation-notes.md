# Implementation notes — previous README

> Historical implementation notes retained from the README when the public showcase was prepared.
> Some phase descriptions and counts predate later fixes. For current scope, start with the
> [public README](../README.md), [continuation lifecycle record](architecture/durable-continuation-lifecycle.md)
> and [coordination evidence](architecture/autonomous-coordination.md).

A portfolio demo platform for **patient referral coordination** built on Google ADK, A2A agents,
streaming A2UI, a versioned prompt/agent registry, three-layer guardrails, hybrid RAG, evaluation,
OTel/OpenSearch observability and human-in-the-loop handoff.

> **This is a demonstration, not a product.** It does not accept real patients, makes no clinical
> decision, and has no real insurance or hospital integration. Every hospital, doctor, patient,
> insurance, document, email and SMS datum is synthetic (§1 of the architecture).

## Start here

| Document | What it is |
| --- | --- |
| [`ReferralEasy_Architecture_v3_EN.md`](../ReferralEasy_Architecture_v3_EN.md) | **The source of truth.** v3.0, English. Section references like `§7` throughout the code point into it. |
| [`IMPLEMENTATION_TODO.md`](../IMPLEMENTATION_TODO.md) | The derived, checkable work plan. Nine phases, plus the invariants that must hold. |
| [`docs/adr/`](../docs/adr/) | Every non-obvious decision, dated, with its consequences. |
| [`docs/runbooks/`](../docs/runbooks/) | Symptom-first: what you might see, and what it means. |
| [`docs/architecture/README.md`](../docs/architecture/README.md) | Spec section → code module map. |

The current architecture review, repaired integration defects, reproducible scenarios and remaining
gaps are recorded in [`docs/architecture/review-2026-10-18.md`](../docs/architecture/review-2026-10-18.md).

## Status

This table is updated as phases close — it is not a wish list. A phase is "done" only when its exit
criterion in [`IMPLEMENTATION_TODO.md`](../IMPLEMENTATION_TODO.md) is met by a test that fails without it.

| Phase | Scope | State |
| --- | --- | --- |
| 0.1 | Repo skeleton, toolchain, compose, CI | done |
| 0.2 | Risk spikes (S1–S10) | **partial** — S1, S6, S8 verified live; S7 blocked; S2/S9/S10 pending. See below. |
| 1.1 | Contracts (JSON Schema → Pydantic + Zod) | done — 9 schemas, 102 Pydantic models / 131 Zod definitions, drift-checked |
| 1.2 | Synthetic fixtures | done — 296 records across all 8 scenarios |
| 1.3 | PostgreSQL schema | done — 16 migrations; coordination grants and handoff consent added in 015–016 |
| 1.4 | State machine, policy, approval binding | done — `packages/domain`, 176 tests, ADR-0026 |
| 1.5 | Mock hospital API | done — `services/mock-api`, 94 tests |
| 1.6 | Scripted end-to-end booking | done — `make e2e`, 16 tests, all 12 fault types |
| 2.1 | Gateway (REST, SSE, auth, guardrails) | done — 156 tests without a database, **266 with one (110 hitting the real server)**, ADR-0031. Postgres control profile implemented; the same suite runs against both backends |
| 2.2 | Web client + eight A2UI components | done — `apps/web`, ADR-0027. Plus the **admin app** (below) as a third entry point in the same bundle |
| 3.1 | `packages/agent-runtime` | done — 225 tests (9 live-provider), ADR-0030 |
| 3.2 | Specialist agents | done — `services/agents`, 525 tests, ADR-0033. **All seven run in compose** on the same image, one per role |
| 3.4 | Skill packages | done — 8 packages, 205 tests, ADR-0028 |
| 3.3 | Coordination | done for the standard referral journey — durable patient-answer continuations, independent plan coverage ∥ matching, doctor-specific network checks, hospital-specific readiness, reviewed booking and consented handoff. See [completion evidence](../docs/architecture/autonomous-coordination.md) and ADR-0041 |
| 3.5 | Registry and versioning | **partial** — the manifest, its the-hash-moves-for-the-right-reasons tests and the CI version gate exist, and a real manifest has been produced: `registry/releases/rel-20261016-8e00275.json`, built with the digest of an actually-built `referraleasy/agents:dev` image, with `latest.json` and `index.json` beside it. `make eval` reads it. What is still missing is the other half of §13: **every service image is built, and no CI job builds or publishes them**, so the gate guards a registry that nothing yet ships |
| 4.2 | Input guardrails — three layers | done and **adopted**: `packages/guardrails`, ADR-0038. All three layers are wired into the gateway's write path, and `/healthz` reports them individually |
| 4.3 | Output guardrails | package done, **not adopted** — `output.OutputGuardrail` checks seven things and no production path calls it. The obvious place (the gateway's `SurfaceRegistry.issue()`, the one choke point every card passes) is the wrong one: its `SourceOracle` is asked about `provider_id`/`hospital_id`/`slot_id`, which live in the mock API and the directory, not in the gateway — so a gateway oracle would answer "yes" to every id the check exists for. It belongs with the agents, which hold the retrieval results. The reasoning is in `IMPLEMENTATION_TODO.md` §4.3 |
| 5 | Workers: outbox, leases, reconciliation, HITL | done — 264 tests (69 integration), ADR-0035 |
| 6 | Hybrid RAG | done — 171 tests (13 against a live OpenSearch), ADR-0029 |
| 7.1 | Telemetry (§15) | **adopted in the gateway and the seven agents, as spans and now as events.** One server span per request, the caller's `traceparent` continued across the A2A hop, verified read back out of `otel-traces-*`. The gateway measures its **turns** (`latency.turn.ms`, plus the outcome class) and records every guardrail screening per layer; the agents measure their **invocations** (`latency.invocation.ms`) and their logical **triggers** — all three of §15's latency distributions, read back out of `otel-events-*` from a live request rather than asserted. The workers emit §15's two HITL measurements from `handoff resolve` and their spans; **the two TypeScript surfaces are not wired** |
| 4.1 | Demo scenarios | definitions done, verification **stack-gated** — all eight are specified in `fixtures/scenarios.json` with their fault steps, correct outcome classes and assertions, and the admin app renders them. The behaviour-level checks are in the database-gated worker suite and `make e2e` |
| 7.2 | Admin app | **all nine pages read real data** — `apps/web/src/admin/`, its own entry point, hash router and API client. Overview, Agents & Versions, Trace Explorer (with a per-trace waterfall), Evaluations, Guardrails, Feedback and Fault Scenarios read the telemetry indices (ADR-0039); Alerts reads §16's rules and the kill switch, and Human Queue reads §11's handoff table (read-only — the actions stay in the workers' CLI, so there is one arbiter for the claim races) |
| 7.3 | Alerts, mock email, kill switch | **partial** — `packages/alerts` implements §16's four rules, the three-state decision (`firing`/`ok`/`unmeasured`, where the third is never a pass), dedup/cooldown with resolutions as their own event, and a mock outbox that cannot loop and never sends. The **kill switch is wired** to the booking path: it refuses the write with `booking_halted` and leaves the help card, the stream, the transitions and the dashboard working. Not done: the scheduler loop that queries the indices on an interval, and the durable tables |
| 7.4, 8.2 | Scenario Runner, version regression | not started — §12's runner needs the stack to reset a seed and produce a real trace link |
| 8.1 | Evaluation (§14) | package done, **has scored nothing** — `packages/eval`: a 44-case dataset across all eight scenarios, fourteen deterministic rules, a judge seam that reports `not_applicable` while ADR-0011 is unresolved, and a CLI that refuses to invent its inputs. No replay harness exists, so every case reports as unmeasured |
| 8.3 | Delivery | `make demo` orchestrates the lifecycle; the Cloud Run design is ADR-0040, which names the four things that do not hold |

**Coordination verification (2026-10-19):** 2,934 Python unit tests passed, with two expected fixture
skips. All 24 E2E cases passed twice, including eight autonomous journeys with PostgreSQL. The web
suite passed 168 tests. See [the full record](../docs/architecture/autonomous-coordination.md).
Counts move, so the commands are the real answer:

```bash
make test                 # unit + contract, no services needed
make test-integration     # database-backed tests (needs the compose postgres)
make test-integration-rag # the 13 OpenSearch-backed retrieval tests
make e2e REPEAT=2         # isolated mock API + temporary SQLite per iteration
```

Three suites are excluded from the default `pytest` run, each by marker, and each is run explicitly
because its precondition is not what a bare `pytest` provides:

- **`integration` → `make test-integration`** (344 tests). Without `TEST_DATABASE_URL` these *skip*,
  the run still exits 0, and that reads as a pass. CI has a job with a Postgres service, and it
  **fails if any of them skip** — and until recently it would not have noticed 104 of them, because
  `migrations/tests` sat outside `testpaths` and could not be collected at all: its helpers were in a
  `conftest` imported by bare name. Both are fixed, and `tests/test_test_layout.py` now enforces that
  rule over `migrations/tests` too.
- **`e2e` → `make e2e`** (16 tests). The suite consumes slots and the mock API has no reset
  endpoint, so a second run against surviving state fails in fixture discovery — an error that looks
  like a product bug and is not. CI restarts the mock API on a clean database first, and asserts the
  tests ran rather than trusting a zero exit code.
- **`spike` → `make spike`** (19 tests). They require provider credentials and a network, and they
  **fail rather than skip** — a skipped spike would hide that Phase 0 is still open.

The 13 OpenSearch-backed tests are the one exception: **they have no CI job.** A service container
for OpenSearch is fiddly enough that an unverifiable job would be worse than a documented gap, so
they run only when someone runs `make test-integration-rag`.

### Three things to know before you read any number in this repo

1. **The provider contracts are verified, not assumed.** S1 (tool calling, structured output,
   streaming, cancellation) and S8 (OpenSearch kNN, hybrid search, OTel ingest) were run against the
   live providers and their findings are recorded in `docs/adr/`. S8's most useful result was
   negative: **OpenSearch does not ingest native OTel metrics**, so the telemetry path is designed
   around that rather than against it. S7 (rerank) is still blocked — see
   [`docs/adr/ADR-0008`](../docs/adr/ADR-0008-opensearch-single-node-hybrid-search-and-otel-metrics.md).
2. **The demo runs on the plan's offline-fixture profile** (`MODEL_BACKEND=offline`) by default:
   canned, deterministic model/embedding/rerank responses, zero network calls, zero cost. Every
   artefact produced that way is stamped `synthetic: true`.
3. **Anything that would be a measurement — latency, cost, guardrail false-positive rates, eval
   scores — reports "no data" under the offline profile.** It does not report canned numbers dressed
   up as results. `scripts/validate_env.py` refuses to start a `live` backend while any `CHANGEME_*`
   pin remains, so an un-pinned model ID can never reach a demo run.

## Technology → scenario → code → evidence

§8.3 of the plan requires the README to do more than list technologies: each one must be tied to a
working scenario, the module that implements it, and the trace or measurement that proves it ran.

The **Evidence** column names something you can run or read, not a claim. Where a technology is
implemented but not reachable from a running service, the row says so — that is the honest version
of the table, and it is more useful than one that leaves the gap to be discovered.

| Technology | Scenario | Code | Evidence |
| --- | --- | --- | --- |
| Google ADK | Standard booking | `packages/agent-runtime/src/.../model.py`, `services/agents/` | `tests/agent_runtime/test_model_factory.py` (9 live-provider tests) and ADR-0002's recorded spike |
| A2A agents | Standard booking | `services/agents/*/` (7 roles), `packages/contracts/python/src/referraleasy_contracts/a2a.py` | `make up` → `curl localhost:8106/healthz` returns `{"role":"scheduling",...}`; `tests/contracts/test_a2a_adapter.py` |
| Streaming A2UI | Standard booking | `apps/web/src/a2ui/`, `services/gateway/src/referraleasy_gateway/api.py` (`stream_events`) | `GET /v1/workflows/{id}/events` streams real frames — `id: …/data: {"type":"agent_progress",…}` (verified against the running stack) |
| Three-layer guardrails | Any write | `packages/guardrails/`, `services/gateway/src/.../guardrails.py` | `/healthz` reports `guardrails: {configured: true, regex: true, decision_model: true, local_classifier: true}`; ADR-0038's per-layer corpus measurement |
| Versioned prompt/agent registry | — | `packages/release-registry/`, `registry/` | `registry/releases/latest.json` is built from a real image digest (`uv run python scripts/check_release_versions.py build --git-sha … --image-digest …`) and read by `make eval`. **Not reachable from a running service** — a service reports its versions, but none of them reads the registry to get them |
| Hybrid RAG | Network conflict | `packages/rag/` | `make test-integration-rag` — 13 tests against a live OpenSearch, including the structured-record-beats-stale-text rule |
| Evaluation | — | `packages/eval/`, `evaluations/datasets/offline-v1.json`, `docs/runbooks/evaluation.md` | **Built, and has measured nothing.** 44 cases across all eight scenarios, 14 rules, a refused-or-unmeasured judge seam, and a CLI that reports `coverage 0/44` and why. No replay harness exists, so nothing has been scored — which the runbook and the CLI's own output both say |
| OTel + OpenSearch observability | Standard booking | `packages/telemetry/`, gateway and agent middleware | Send `traceparent: 00-<trace-id>-<span-id>-01` to the gateway or an agent, then read the span back from `otel-traces-*`: same `traceId`, same `parentSpanId`, `serviceName` naming the role. Reproduced on this checkout, not asserted |
| Human-in-the-loop handoff | No specialist found | `services/workers/`, `packages/agent-runtime/src/.../suspend.py` | `tests/workers/test_hitl.py`, `tests/agent_runtime/test_suspend.py` — including that a suspended turn persists state and holds no call |
| Admin dashboard (§12) | Any | `apps/web/src/admin/` (own entry point), `services/gateway/src/.../admin.py` | Open `http://localhost:3000/admin`, or `GET /v1/admin/overview` with an admin session. Nine pages, eight reading live indices; a page whose source is missing answers `{available: false, reason}` rather than zeroes, and `tests/gateway/test_admin_reads.py` asserts exactly that |
| Alerting and the kill switch (§16) | Any write | `packages/alerts/`, `services/gateway/src/.../killswitch.py` | `tests/alerts/test_alerts.py` (36) and `tests/gateway/test_kill_switch.py` (20). Engage the switch and the booking write answers `booking_halted` while the help card, the stream, the transitions and the dashboard keep working — each of the four is its own test |

### Known gaps, named

These are load-bearing enough to state outside the table:

1. **Per-conversation release pinning is incomplete.** The gateway resolves the dispatch release at
   startup. Conversations are not pinned to a release and the canary router has no caller.
   (The *admin* kill switch §16 asks for is wired; the
   registry's rollout kill switch, which stops a canary rather than all writes, is a different control
   and is not.)
2. **The CI version gate has nothing to gate.** A baseline manifest now exists
   (`registry/releases/latest.json`, built from a real `HEAD` and a locally built image digest) and
   `make eval` reads it, so the gate can run — but nothing in CI *builds or publishes an image*, so
   `RELEASE_IMAGE_DIGEST` is still set nowhere and the job reports a broken gate rather than gating.
   The remaining work is a build-and-push step, not a manifest.
3. **Evaluation has run nothing.** The package, the dataset, the rules and the CLI exist and are
   tested; no replay harness does, so `make eval` reports all 44 cases as unmeasured. That is the
   honest state rather than a stub, and it is the largest remaining piece of work.
4. **Historic SSE replay remains process-local.** Standard coordination now projects specialist
   results into durable workflow cards and resumes accepted answers. Historic events and general
   model-task dispatch still need shared persistence before multiple gateway replicas can serve
   the same stream. See [ADR-0041](../docs/adr/ADR-0041-autonomous-coordination.md).
5. **Most turns state no outcome class.** §15's class is recorded from the `terminal` event, which
   the gateway emits on the endings that produce a consequential result (a booking, an emergency).
   A plain recorded message ends without one, and no class is invented for it — the turn is still
   counted and still contributes its latency, and `tests/gateway/test_turn_telemetry.py` pins that
   distinction rather than leaving it to be discovered from a chart.
6. **The operator's actions have no dashboard.** The Human Queue page reads the queue; claiming,
   releasing and resolving are `python -m referraleasy_workers handoff ...` on a terminal. That is
   deliberate — one implementation of §11's claim rules, where the database's compare-and-set is the
   arbiter — but it is not a UI an operator would choose.

## Running it

```bash
make bootstrap         # uv sync + pnpm install
make demo              # validate env → compose up → migrate → seed → index → health → print URLs
make test              # unit + contract tests (Phase 0 spikes excluded)
make test-integration   # database-backed tests — needs the compose postgres, see below
make e2e               # booking faults and autonomous journeys against a fresh mock API
make eval              # §14's evaluation — reports what it could not measure, and why
make down
```

Two surfaces come out of the same web bundle: the patient client at `/` and the admin dashboard at
`/admin` (nine pages of telemetry, evaluation, guardrails, feedback and §20's scenarios).

`make help` lists every target. The compose stack has three profiles — `core`, `showcase` and
`dashboards` (`make up PROFILE=showcase`).

Two of those targets exist because the tests they run would otherwise pass by not running:

- **`make test-integration`** runs the tests that need a real PostgreSQL. Without `TEST_DATABASE_URL`
  they *skip*, the run still exits 0, and that reads as a pass — so the target refuses to start
  without the variable instead of reporting a green run over nothing. CI has its own job with a
  Postgres service that fails if any of them skip. (The 13 integration tests under `tests/rag` need
  OpenSearch rather than PostgreSQL and are excluded here; they have no CI job yet.)
- **`make e2e`** starts an isolated mock API with a temporary database. Each `REPEAT` iteration
  starts fresh; it does not stop the running demo or delete its data.

## Layout

```text
apps/web/                     A2UI patient client + admin
services/gateway/             FastAPI: REST commands, SSE stream, auth, guardrails
services/agents/{coordinator,intake,coverage,matching,readiness,scheduling,recovery}/
services/mock-api/            Fastify: synthetic hospital + coverage + directory API
services/workers/             Outbox, reconciliation, evaluation, indexing
packages/contracts/           JSON Schema source → Pydantic + Zod bindings
packages/skills/              Versioned skill packages (SKILL.md + manifest)
packages/agent-runtime/       Model factory, budgets, tool allowlist, skill loader
registry/{agents,prompts,releases}/
evaluations/{datasets,rubrics,evaluators,reports}/
fixtures/                     Synthetic directory, patients, coverage, documents, faults
migrations/                   PostgreSQL schema and runner
infra/{otel,data-prepper,opensearch,postgres}/
docs/{architecture,adr,runbooks,demo-scenarios}/
tests/{contracts,integration,e2e,alerts,eval,scripts}/
```

## Invariants

These are release blockers, not guidelines. The full list with its tests lives in
[`IMPLEMENTATION_TODO.md`](../IMPLEMENTATION_TODO.md); the load-bearing ones:

- No booking without a valid approval bound to patient + workflow + state version + payload hash + expiry.
- No double booking — slot uniqueness, and the same idempotency key on every retry.
- A stale worker (expired lease or old generation) cannot write.
- Hard constraints are never relaxed silently.
- `PENDING_REVIEW` and `REQUESTED` are never shown as confirmed. HTTP 200 ≠ confirmed.
- Documents and retrieved text are data, never instructions.
- Telemetry, evaluation and notification failures never roll back a booking.
- No fabricated demo charts — every admin number comes from a real trace or event.
