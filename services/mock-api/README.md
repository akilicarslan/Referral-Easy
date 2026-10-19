# `@referraleasy/mock-api`

The synthetic hospital network the ReferralEasy demo runs against: a directory of
locations, hospitals, providers and slots; coverage, referral requirements and
document checks; and a booking workflow that behaves like the real thing —
durable idempotency, real slot uniqueness, an outbox, and eleven reproducible
upstream faults.

Offline by construction. There is no model provider, no credential and no
outbound call at build time or at run time; `/healthz` reports `llm_calls: 0`,
and every response is flagged `synthetic: true`.

> All data is synthetic. No provider credentials exist and no model is ever called.
> The response `state` field is the only authority on whether a booking exists.

---

## Run it

```bash
# from the repository root
pnpm mock-api:dev              # tsx watch, PORT 4010, fixtures from ./fixtures

# narrower
pnpm --filter @referraleasy/mock-api test        # vitest, 12 files
pnpm --filter @referraleasy/mock-api typecheck   # tsc --noEmit
pnpm --filter @referraleasy/mock-api build       # tsc → dist/
```

Docker (the compose contract is fixed by `compose.yaml`: build context is the
**repository root**, because the image serves `fixtures/`):

```bash
docker build -f services/mock-api/Dockerfile -t referraleasy/mock-api:dev .
docker run --rm -p 4010:4010 referraleasy/mock-api:dev
# or, with the rest of the demo
docker compose --profile core up mock-api
```

The image runs as a non-root user, carries `curl` for the compose health probe
(`curl -fsS http://127.0.0.1:4010/healthz`), and keeps its SQLite file at
`/app/.data/mock-api.sqlite` — mount a volume there to keep bookings across a
restart.

## Configuration

Every variable has a working default; the demo needs none of them.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PORT` / `HOST` | `4010` / `0.0.0.0` | Listen address. |
| `MOCK_API_DB` | `services/mock-api/.data/mock-api.sqlite` | SQLite file (`:memory:` is supported). |
| `MOCK_API_SEED` | `auto` | `fixtures` (must load, or fail loudly), `builtin` (the embedded dataset), `auto` (fixtures when present, else builtin). |
| `MOCK_API_FIXTURES_DIR` | `<repo>/fixtures` | Root of the shared fixture set. Fault scenarios follow it. |
| `MOCK_FAULT_SCENARIOS_DIR` | `<fixtures>/fault-scenarios` | Scenario files (`*.json`). |
| `MOCK_FAULT_SEED` | `42` | Root seed of the fault engine. The demo pins `42`. |
| `MOCK_API_FAULTS_ENABLED` | `true` | `false` disables injection entirely (a clean network). |
| `MOCK_API_NOTIFICATION_MAX_ATTEMPTS` | `3` | Attempts before an outbox entry is terminal (`abandoned`). |
| `MOCK_API_NOTIFICATION_BACKOFF_MINUTES` | `5` | Wait between automatic delivery attempts. The tests set `0`; the demo keeps `5`. |
| `MOCK_API_URL` | `http://localhost:$PORT` | Advertised in the OpenAPI `servers` entry. |
| `MOCK_API_FAULT_SCENARIO` | unset | Arms a scenario at boot, for a container that should start already faulted. |
| `MOCK_API_FAULT_MODE` | `scenario` | Boot-time mode: `scenario`, `random` or `off`. |

## The API surface

**The contract is the code.** Every request and response is validated by a Zod
schema in `src/contracts/zod.ts`, and the served OpenAPI document is generated
from those same definitions — so `GET /openapi.json` cannot drift from what the
service accepts or returns. `src/contracts/types.ts` is the single seam to the
frozen contract: it re-exports the inferred types, and when the generated
`@referraleasy/contracts` package lands that one file changes.

`GET /` returns the endpoint index. The public surface, by module:

| Module | Endpoints |
| --- | --- |
| meta | `GET /health`, `GET /healthz`, `GET /openapi.json`, `GET /` |
| locations / hospitals / providers | `GET /locations[/:id]`, `GET /hospitals[/:id]`, `GET /providers[/:id]` |
| availability | `POST /availability`, `GET /availability` |
| directory | `GET /directory/search` |
| coverage | `POST /coverage/lookup`, `GET /coverage`, `GET /plans`, `GET /network-records` |
| referral requirements | `GET /referral-requirements` |
| documents | `GET /documents`, `GET /documents/:document_id`, `POST /documents/check` |
| evidence | `GET /evidence`, `GET /evidence/:evidence_id`, `GET /evidence-snapshots` |
| appointments | `POST /appointments`, `GET /appointments`, `GET /appointments/:booking_request_id`, `GET /appointments/lookup_request`, `POST /appointments/:booking_request_id/cancel`, `POST /appointments/:booking_request_id/reschedule` |
| notifications | `GET /notifications/outbox[/:outbox_id[/preview]]`, `POST /notifications`, `POST /notifications/drain`, `POST /notifications/outbox/:outbox_id/retry` |
| fault control plane | `GET /_faults/scenarios`, `GET /_faults/state`, `POST /_faults/arm`, `POST /_faults/reset` |
| diagnosis | `GET /_diagnostics/attempts`, `GET /_diagnostics/vendor-calls` |

### One endpoint, several spellings

Several calls are reachable under more than one address, because the demo's
scenario text and the frozen contract use different words for the same call.
They are the same code path **and** the same route to the fault engine
(`src/faults/routes.ts`), so a fault armed for one spelling fires on the other
and the occurrence counters are shared rather than separate:

| Spelling | Canonical address |
| --- | --- |
| `GET /availability` | `POST /availability` |
| `GET /coverage` | `POST /coverage/lookup` |
| `POST /notifications` | the outbox sender's hop, `POST /internal/notifications/send` |

`POST /internal/notifications/send` and `POST /internal/upstream/booking` are
**route identities for the fault engine, not HTTP addresses**: they name the two
hops the fixtures target inside a request (the confirmation delivery and the
hospital's booking call). Anything a fixture names is reachable, which is why
the fold exists at all — a fault nobody can trigger is not a fault.

Every response that carries a fault also carries `x-mock-fault`,
`x-mock-fault-scenario` and `x-mock-fault-seed` headers. A response with no
injection has no such header. The control plane (`/_faults/*`,
`/_diagnostics/*`), the health probes (`/health`, `/healthz`), the index and
`/openapi.json` are never faulted, whatever is armed: a fault that also breaks
the endpoint you need in order to disarm it, or the probe the container health
check runs, would make the demo unrecoverable.

## Fault injection

`POST /_faults/arm {"scenario_id": "notification_failure"}`, then call the route
the scenario is about; `POST /_faults/reset` clears the counters and the armed
state; `GET /_faults/state` shows the armed scenario, one counter per canonical
route, and the decisions that were served with the seed that produced each.

Fixtures are addressed four ways, all equivalent:

| Spelling | Example |
| --- | --- |
| the fault name | `stale_slot_409` |
| the journey | `standard_booking` (the journey's preferred file) |
| journey + variant | `standard_booking/stale_slot_409` |
| the injection alias | `stale_coverage_evidence` (the file the journey declares) |

A single request can also carry its own controls: `x-fault: rate_limit_429`
fires that fault once, without arming anything; `x-fault-scenario: <id>` (or
`?fault_scenario=<id>`) picks the scenario for that request; `x-fault-mode:
scenario | random | off` overrides the mode for it. An unknown fault or scenario
is refused rather than silently ignored.

### How a decision is made

1. **Fixture steps win.** A file's `steps` (each with `match`, `occurrence` and
   `fault`) drive the decision. `default_fault` is the fallback for a scenario
   that declares no steps — it does not shadow the steps of one that does.
2. **One occurrence counter per canonical route.** `occurrence: 2` means "the
   second time this endpoint is called", whatever spelling the caller used.
3. **Peek, then consume.** A fault that names one channel (`"channel": "sms"`)
   neither fails nor spends its occurrence on the other leg. A decision that is
   never used leaves no trace: the counter is untouched and no event is recorded.
4. **A fault that names its subject is about that subject.** `stale_slot_409`
   names one slot; a booking for a different slot is served normally, and the
   occurrence it was holding is given back to the counter — otherwise arming a
   scenario would refuse an unrelated booking and spend the demonstration in the
   same breath.
5. **The seed is the fixture's, not the process's.** `/_faults/state` and every
   diagnostic carry the seed that produced the decision (`MOCK_FAULT_SEED=42`
   plus each file's own `fault_seed`), so a run can be replayed.

Faults also live at two depths, on purpose. `latency`, `rate_limit_429` and
`service_unavailable_503` are answered at the HTTP edge, before any route logic,
because a slow or throttled network is not a well-formed body. Everything else is
produced inside the route that owns it, so the fault travels with its own
diagnostics (`/_diagnostics/attempts`, the outbox row, the coverage citation).

### The eleven faults

| Fault (seed) | What it stands for | What you observe |
| --- | --- | --- |
| `latency` (41) | A slow upstream that eventually answers. | The call stalls (12 s in the fixture), then `200 slots_found`. |
| `rate_limit_429` (42) | The upstream throttling this caller. | `429 upstream_rate_limited`, `retry-after`, `retry_after_seconds` in the envelope. |
| `service_unavailable_503` (43) | The upstream is down. | `503 upstream_unavailable` with a `retry-after`; never an empty slot list. |
| `empty_valid_result` (45) | An empty page that is *valid*. | `200 no_slots_for_constraints`, `slots: []`, **no** error envelope. |
| `malformed_schema` (44) | A syntactically valid body that violates the contract. | `200` with `result_kind: "maybe"`, no `candidates` array, and a `contract_violation` block saying what is missing. Retrying cannot fix it. |
| `timeout_before_commit` (47) | The hospital never answers and never wrote anything. | The caller times out; the same idempotency key later answers `timeout_before_commit`, `state: "REQUESTED"`, `appointment_committed: false`. Nothing was committed. |
| `timeout_after_commit` (42) | The hospital wrote it; the response was lost. | The caller times out; the same key later answers `created`, `found: true`, `state: "CONFIRMED"`, reconciliation `resolved_booked`. |
| `duplicate_event` (48) | The same confirmation arrives twice. | The second delivery is recorded as `duplicate_replayed`; exactly one appointment exists for the slot. |
| `stale_slot_409` (46) | The slot moved on between the board and the booking. | `409 slot_conflict` naming `presented_version`, `current_version` and the stored `slot_version`; **nothing is written**. |
| `notification_failure` (50) | The appointment commits, the confirmation does not send. | The booking is `CONFIRMED`; the named leg is `failed` on the outbox with its error; the delivery route answers `502 notification_undeliverable` with `booking_still_committed: true`. |
| `stale_coverage_evidence` (49) | A stale network file still claiming `in_network`. | The coverage answer reports `conflict: true`, `freshness.state: "stale"` and names `winning_evidence_id`. |

`pnpm --filter @referraleasy/mock-api test` reproduces all eleven from the seed
in `test/faults.test.ts`, and each one again through the real fixture files in
`test/fixtures-integration.test.ts`.

## Booking semantics

The five rules the workflow layer is allowed to build on:

1. **No double booking.** One live appointment per slot, enforced by a
   `UNIQUE` index in SQLite and by a conditional `UPDATE` inside
   `BEGIN IMMEDIATE`, not by a read-then-write in application code. Two
   concurrent requests with different keys on one slot produce exactly one
   winner and one `409 slot_conflict`; the loser writes nothing at all.
2. **An unknown outcome is reconciled with the SAME key.** The key is durable
   across restarts. `GET /appointments/lookup_request?idempotency_key=…` answers
   what actually happened, including after a timeout — and
   `outcome_unknown` is *not* a rejection: it is `202`, with the booking left to
   be reconciled. `POST /appointments` with the same key and a different payload
   is a `409 idempotency_key_reuse` (hashes on both sides), never a silent
   re-book.
3. **A notification failure never rolls a booking back.** Deliveries run after
   the commit transaction, and a failure is recorded on the outbox. A booking
   that was written stays written.
4. **`REQUESTED` and `PENDING_REVIEW` are never presented as confirmed.** There
   is no boolean `confirmed` field anywhere in the contract; `state` is the only
   authority. `HTTP 200` does not mean confirmed — a `review_required` hospital
   answers `200` with `state: "PENDING_REVIEW"` and no verified appointment.
5. **`expected_slot_version` is a real compare-and-set.** Presenting a version
   that has moved on is refused with `409`, and the envelope reports the version
   the caller presented, the version the hospital reports as current, and the
   version this service holds — three separate facts, never one blended number.

Cancelling a booking returns its slot to the board. Rescheduling secures and
verifies the replacement **before** cancelling the original: if the replacement
is not secured, the original stays exactly as it was.

## Notifications

An outbox row per channel (`email`, `sms`) per booking, written inside the
booking's own commit transaction — so the intent to notify is never lost with
the booking, and the booking never depends on the delivery.

* States: `pending → sent`, or `failed` (retryable) → `abandoned` (terminal
  after `MOCK_API_NOTIFICATION_MAX_ATTEMPTS`).
* **Automatic passes respect the backoff.** `POST /notifications/drain` (the
  optional `booking_request_id` narrows it to one booking) and the delivery
  right after a commit attempt only what is due; a provider that just failed is
  not hammered by the loop.
* **An explicit request means now.** `POST /notifications` and
  `POST /notifications/outbox/:outbox_id/retry` attempt past the backoff —
  "retry this one" answered with "nothing was due" would be a no-op that looks
  like a success. Attempts still count, so a provider that keeps failing reaches
  the ceiling instead of retrying forever.
* Reopening an `abandoned` entry is the retry route's explicit job, and the only
  case where the attempt count restarts.
* `GET /notifications/outbox/:outbox_id/preview` renders the patient-facing
  message in the **hospital's** timezone, not UTC.

## Coverage

Two facts are kept separate all the way out: whether the provider bills this
payer, and whether they are in network **for this plan**. `null` means unknown
and never becomes `false` or `true` — a caller must never read unknown as
in-network.

The `conflict` flag is not a heuristic about those two booleans. It is a reading
of the *recorded standing* of the sources: a source that was explicitly
invalidated or is recorded stale does not stand on its own, and when such a
source sits next to an authoritative one the answer is disputed, both sides are
cited in `conflicting_evidence_ids`, and `winning_evidence_id` names what the
answer rests on. A clean out-of-network answer is not a conflict; a fresh but
merely non-authoritative blurb next to a record is not one either.

A lookup may name a provider, a hospital, or both. With no provider there is no
plan-level record to read, so the answer is the hospital's own evidence:
`status: "unknown"`, `accepts_insurance: null`, and the hospital's directory
blurb — the thing a naive answer would rest on — decides the reported freshness.
A lookup that names neither subject is `400 missing_lookup_subject`, not an
empty answer.

**Fixture-side divergences** (reported, not copied): `coverage_results.json`
row `…0012` reports `accepts_insurance` and `prior_auth_required` as `null` while
the provider's own listing names the plan's payer and the plan sets the flag
explicitly — and while row `…0005` of the *same plan* reports it as `false`. The
same row cites `…0020` as its second source, but that row belongs to another
provider; no evidence row in the set names the provider it is about. Row `…0009`
nulls the same plan-level field. `test/coverage-fixtures.test.ts` pins both
directions: an unexpected divergence fails, and a documented divergence that
quietly disappears fails too.

## Availability

`POST /availability` (or `GET /availability`) returns available slots in
chronological order, with UTC instants and `result_kind` distinguishing a
genuine empty result (`no_slots_for_constraints`) from a failed upstream
(`upstream_error`) — an empty list is never returned for a failure.

`preferred_days` and `preferred_time_of_day` are **echoed in `query`, not
applied as filters**. No single rule reproduces the authored
`availability_results.json`: `preferred_days: ['friday']` keeps a Wednesday slot
in one case, and every case whose data can tell contradicts
`preferred_time_of_day`. Ranking belongs to the layer that can explain itself, so
the service answers with the facts it holds and hands the preferences back
intact. Four authored expectations therefore differ from the data the same
fixture set ships (slots `…0005`, `…0012`, `…0021`, `…0027`); they are listed and
pinned in `test/fixtures-integration.test.ts` rather than silently matched.

No ranking data is served: `provider_candidates.json`'s `constraint_fit`, `rank`
and `distance_miles` are deliberately absent from every response.

## Diagnosis

| Endpoint | Answers |
| --- | --- |
| `GET /_diagnostics/attempts?idempotency_key=…` | Every attempt and the booking for a key, **plus how many appointments that slot actually holds** (the double-booking answer). |
| `GET /_diagnostics/vendor-calls` | What the adapter sent and what the hospital's own dialect answered **before** normalisation — evidence that the two dialects are normalised, not faked. |
| `GET /_faults/state` | The armed scenario, the occurrence counters, and the decisions served, each with its seed. |

## Tests

`pnpm --filter @referraleasy/mock-api test` — 12 files, all offline, no sleeps
that are not fault delays.

| File | What it pins |
| --- | --- |
| `smoke.test.ts` | Every frozen contract schema is exposed; the SQLite `UNIQUE` constraint is real; payload hashing is key-order independent. |
| `api-surface.test.ts` | Boot, seed counts, the generated OpenAPI, and the shapes of the main reads. |
| `idempotency.test.ts` | Replay, key reuse, durability across a restart, a missing key. |
| `slot-uniqueness.test.ts` | Two racing keys on one slot (one winner), the same key twice, a stale version, and the database constraint behind both. |
| `transactions.test.ts` | Appointment + outbox + attempt in one transaction; a total notification failure leaves the booking intact; an undeliverable notification is abandoned instead of retried forever. |
| `lifecycle.test.ts` | `PENDING_REVIEW` is never confirmed; timeout before vs after commit; refusal is not `outcome_unknown`; cancel returns the slot; reschedule secures before cancelling. |
| `adapters.test.ts` | Both hospital dialects normalise to the same field names, and both keep their raw payload as evidence. |
| `availability-failure.test.ts` | "We could not ask" never becomes "there is nothing"; preferences are echoed, not filtered; the hospital's wall clock. |
| `faults.test.ts` | All eleven faults from one seed, each with its own observable; determinism and replay after a reset; the random mode is a function of the seed; the control plane is out of a blanket fault's reach; an unknown fault is refused. |
| `fixtures-integration.test.ts` | The shared fixtures drive the served network: every referenced id resolves, every authored availability query is answered (with the four divergences named), every fault file loads and is reachable, the ladders run in order, each fault is reproduced through its own route. |
| `coverage-fixtures.test.ts` | Every authored coverage row, field by field, in both directions, plus the `network_conflict` journey's five assertions. |
| `routes.test.ts` | The spellings of one endpoint fold onto one canonical route, and a fixture step matches either spelling. |

## Decisions

The architecture decisions behind this service — `node:sqlite`, faults at the
upstream boundary, occurrence semantics, journey/variant/alias addressing, route
folding, "no ranking data", the local contract transcription, fixtures-first
seeding, the availability preference semantics, and the coverage/reconciliation
rules — are recorded in
[`docs/adr/ADR-0021-mock-api-contract.md`](../../docs/adr/ADR-0021-mock-api-contract.md).
