# ADR-0021 — The mock hospital network API: contract, durable state and fault injection

- **Status:** Accepted
- **Date:** 2026-10-14
- **Phase:** 1
- **Depends on:** [ADR-0012](ADR-0012-contract-codegen.md) (the frozen schemas), [ADR-0024](ADR-0024-fixture-design.md) (the fixture set and its fault files), [ADR-0023](ADR-0023-compose-topology.md) (the container contract)
- **Owns:** `services/mock-api/**`

## Context

The demo needs a hospital network to reason about — a directory, coverage, slots,
and a booking workflow that a workflow layer can drive, fail against and be
judged on. It cannot use a real one: there are no credentials, there must be no
outbound call, and the network has to behave the same way on every laptop at
every run.

What makes the service *usable as a subject* is that it is honest under failure.
Five rules were treated as release-blocking before any endpoint was written:

1. No double booking, ever.
2. An unknown outcome is reconciled with the **same** idempotency key.
3. A notification or telemetry failure never rolls a booking back.
4. `PENDING_REVIEW` / `REQUESTED` are never presented as confirmed; there is no
   boolean `confirmed` field, and HTTP 200 does not mean confirmed.
5. `outcome_unknown` is not a rejection.

Constraints that shaped the decisions below:

- **Offline by construction.** No model provider, no credentials, no network
  client. Every response is flagged `synthetic: true`.
- **Deterministic.** A fault decision is a pure function of the seed, the
  scenario and the request sequence. `MOCK_FAULT_SEED=42` is the demo's pin.
- **Durable.** Idempotency keys, slot claims and the outbox survive a restart,
  because a demo that only works in one process cannot demonstrate reconciliation.
- **Reproducible from a seed.** Eleven faults, each reachable by name, each
  observable without reading a log from another process.
- The fixtures (ADR-0024) are the dataset *and* the fault scripts. The service
  serves them, and the fault files address the routes the demo's scenario text
  names.
- `compose.yaml` (ADR-0023) fixes the interface: build context is the repository
  root, image `referraleasy/mock-api:dev`, port 4010, health probe
  `curl -fsS http://127.0.0.1:4010/healthz`, profile `core`.

## Decisions

### D1 — `node:sqlite` as the store, with the invariants enforced in the database

The store is SQLite through Node's built-in `node:sqlite` (`WAL`,
`busy_timeout = 5000`, `foreign_keys = ON`), in one file, no ORM, no separate
database service. The demo has to start from a clean clone without a datastore
dependency, and the invariants have to be *structural* rather than remembered by
the application:

- `idempotency_keys` carries a `UNIQUE` index on the key, so a replay is a
  database fact, not a check that a race can slip through.
- A slot claim is a conditional `UPDATE … WHERE status = 'available' AND
  version = ?` inside `BEGIN IMMEDIATE`; the loser's `changes === 0` is the
  refusal. Three concurrent claims on one slot with three keys produce exactly
  one winner, which `test/slot-uniqueness.test.ts` asserts through real
  concurrent requests (including the `SQLITE_BUSY` window).
- One live appointment per slot is a `UNIQUE` partial index, so even a bug in the
  claim path cannot produce two appointments for one slot.

Node 22's built-in driver is why the image needs no native module and the
service has one dependency tree; the trade is a single-writer store, which is
correct for a demo and would not be for a deployment (see Consequences).

### D2 — Faults live at the upstream boundary, at two depths

A fault is injected where the *upstream* would fail, not where the caller's code
would find it convenient:

- **Edge faults** (`latency`, `rate_limit_429`, `service_unavailable_503`) are
  answered by an `onRequest` hook before any route logic, because a slow,
  throttled or unreachable network is not a well-formed body. They set
  `x-mock-fault`, `x-mock-fault-scenario` and `x-mock-fault-seed`.
- **Pipeline faults** (`malformed_schema`, `empty_valid_result`,
  `timeout_*`, `duplicate_event`, `stale_slot_409`, `stale_coverage_evidence`,
  `notification_failure`) are produced inside the route that owns the state they
  touch, so the fault travels with its own diagnostics: the attempt row, the
  outbox row, the coverage citation. A malformed directory body is still an HTTP
  200 and says so in its own body (`contract_violation`), because the point of
  that fault is that the *caller* must notice.

The control plane (`/_faults/*`, `/_diagnostics/*`), the health probes (`/health`,
`/healthz`), the index and `/openapi.json` are never faulted whatever is armed: a
fault that also breaks the endpoint you need in order to disarm it — or the probe
the container health check runs — leaves the demo unrecoverable.

### D3 — Occurrence semantics: steps win, counters are per canonical route, and a decision is peeked before it is consumed

The fault files (ADR-0024 D6) address the API by `{match, occurrence, fault}`.
The service reads them like this:

1. **Fixture steps win.** `default_fault` is the fallback for a scenario that
   declares no steps; it never shadows another file's steps. (A journey such as
   `three_transient_errors` is three files with `occurrence: 1/2/3`; a journey
   with a step-less default and a stepped variant composes without either
   erasing the other.)
2. **One occurrence counter per canonical route**, shared by every spelling of
   that route. `occurrence: 2` means "the second time this endpoint is called",
   not "the second time this client called it".
3. **Peek, then consume.** A fault is decided only for an attempt it is actually
   about. A step that names `channel: "sms"` neither fails nor spends its
   occurrence on the email leg; a step whose subject does not match is handed
   back (`store.releaseOccurrence`), so the counter is untouched and the decision
   is not recorded as a served fault. Without this, arming a scenario would be a
   single-use resource whose one use could go to an unrelated request.
4. **A fault that names its subject is about that subject.** `stale_slot_409`
   names one slot. A booking for a different slot is served normally — and the
   occurrence survives for the request the fixture authored.
5. **The seed travels with the decision.** `/_faults/state` records each decision
   with the seed that produced it (the file's `fault_seed`, not the process
   default), so a run can be replayed.

Rejected: consuming an occurrence on every request that merely matched the route
(makes an armed scenario a one-shot lottery), and keeping the counters in memory
(a restart would silently re-arm a spent fault).

### D4 — Addressing: journey, variant, alias, and per-request controls

The manifest (ADR-0024 D3) indexes scenarios as journeys with per-fault files.
A demo script should not have to know which file a journey currently prefers, and
a test should be able to name one fault exactly. So a scenario is addressable
four equivalent ways: the fault name (`stale_slot_409`), the journey
(`standard_booking`), the variant (`standard_booking/stale_slot_409`) and the
manifest's `injection` alias (`stale_coverage_evidence`). Fixtures win over
built-ins, and a shadowed built-in id is reported in `/_faults/scenarios` rather
than silently disappearing.

Per-request controls exist for one-off probes: `x-fault: <type>` fires one fault
for that request without arming anything, `x-fault-scenario` (or
`?fault_scenario=`) picks the scenario, `x-fault-mode: scenario|random|off`
overrides the mode. `mode: "random"` draws from the seed, not from a stateful
generator, so it is replayable too. An unknown fault or scenario is refused.

### D5 — Route folding: an unfoldable fault is not a fault

Several calls are reachable under two spellings because the demo's scenario text
and the frozen contract use different words for the same call:
`GET /availability` ≡ `POST /availability`, `GET /coverage` ≡
`POST /coverage/lookup`, `POST /notifications` ≡ the outbox sender's hop
(`POST /internal/notifications/send`). They are the same code path **and** the
same route to the fault engine, with shared counters.

The alternative — treating each spelling as its own route — is a fault fixture
that silently stops firing because the caller used the other address, which is a
defect nothing would notice. `POST /internal/upstream/booking` is likewise a
route identity for the engine, not an HTTP address: it names the hop the timeout
fixtures target inside a booking request.

### D6 — The contract seam: transcribe today, one file to change tomorrow

`src/contracts/types.ts` is the only module that knows where the types come
from. Today they are inferred from `src/contracts/zod.ts`, a transcription of
`packages/contracts/schemas/*.json` (ADR-0012), and the served OpenAPI document
is generated from those same Zod definitions — so `GET /openapi.json` cannot
drift from what the service validates. When the generated
`@referraleasy/contracts` package lands, that one file changes.

Two deliberate choices inside the transcription:

- **Request shapes are local.** The published package exposes result schemas; a
  frozen `CoverageLookupRequest` does not exist in it. Inventing one in the
  published namespace would be worse than owning a local request type, so the
  service transcribes the request it validates and says so here.
- **`provider_id` is nullable in a coverage lookup.** The published
  `CoverageResult` allows `provider_id: null` and ADR-0024 D7 #4 records that a
  hospital-level conflict *cannot* be a network record — it is expressed through
  `conflicting_evidence_ids` and `winning_evidence_id`. A request shape that
  demanded a provider would make the authored hospital-level row unreachable, so
  the lookup accepts plan + hospital (and refuses a request that names neither
  subject with `400 missing_lookup_subject`, rather than answering about nothing).

### D7 — Fixtures first, built-in fallback, and a loud failure mode

`MOCK_API_SEED` is `auto` by default: serve the shared fixtures when they are
present, the embedded dataset otherwise, so the service is coherent with the
rest of the demo without becoming unusable when `fixtures/` is absent.
`fixtures` means "the fixtures must load": a fixture set that cannot be parsed
fails the health report (`fixture_load_errors`) instead of quietly serving a
different network. The built-in dataset exists so the service's own tests pin a
dataset that a fixture edit cannot move; every fixture assertion lives in
`test/fixtures-integration.test.ts` and `test/coverage-fixtures.test.ts`.

### D8 — Availability answers with facts, with preferences echoed and no ranking

`preferred_days` and `preferred_time_of_day` are returned inside `query`, not
applied as filters. No single rule reproduces the authored
`availability_results.json` (`preferred_days: ['friday']` keeps a Wednesday slot
in one case; `preferred_time_of_day` is contradicted in every case whose data can
tell), and a filter that only sometimes means what it says would teach the
workflow layer a wrong lesson. Ranking belongs to the layer that can explain
itself, so the service serves the slots that satisfy the hard constraints, in
chronological order, and hands the preferences back intact.

`result_kind` distinguishes a genuine empty page (`no_slots_for_constraints`)
from a failed upstream (`upstream_error`): "we could not ask" never becomes
"there is nothing". `provider_candidates.json`'s `constraint_fit`, `rank` and
`distance_miles` are deliberately not served — a rank the mock invented would be
indistinguishable from a rank the demo computed.

Four authored expectations disagree with the data the same fixture set ships;
they are named and pinned in `test/fixtures-integration.test.ts` rather than
silently matched (see Open questions).

### D9 — Coverage: two facts, recorded standing, and an arbitration

`accepts_insurance` (does the provider bill this payer) and `in_network_for_plan`
(is the provider in network **for this plan**) are separate all the way out, and
`null` means unknown — never smoothed into `false` or `true`, because a caller
that reads unknown as in-network is exactly the failure the demo exists to show.

`conflict` is not a heuristic about those two booleans. It is a reading of the
recorded standing of the sources: a source that was explicitly invalidated
(`invalidated_by`) or recorded `stale` does not stand on its own, and when such a
source sits beside an authoritative one the answer is disputed —
`conflicting_evidence_ids` names both sides and `winning_evidence_id` names what
the answer rests on. This reproduces every authored row, including the two the
naive "accepts insurance but out of network" reading gets wrong: a clean
out-of-network answer is not a conflict, and a stale public description against a
live structured record is one. A fresh-but-non-authoritative blurb beside a
record is not a conflict either.

`freshness` is the recorded freshness of the source the answer rests on, and
`winning_evidence_id` is set whenever a conflict is reported — including under
the armed `stale_coverage_evidence` fault, which claims a conflict and therefore
has to name its record.

### D10 — The booking write path, in order

`POST /appointments` is: replay check → directory/slot validation → claim
transaction → the hospital call → commit transaction → notifications.

1. **The replay check comes first**, so the same key and payload returns the same
   booking with no new row; the same key with a different payload is `409
   idempotency_key_reuse` with both hashes. The key is durable, so reconciliation
   works across a restart.
2. **The claim and the booking row are written in one `BEGIN IMMEDIATE`
   transaction**, with the `UNIQUE` key index as the structural guard. The loser
   of a race writes nothing at all — not a booking, not an attempt.
3. **The hospital call uses the hospital's own dialect** (`instant_confirm`,
   `review_required`), and the raw vendor call is kept as evidence
   (`/_diagnostics/vendor-calls`) so the normalisation can be shown rather than
   asserted.
4. **Appointment, outbox rows and the attempt land in one commit transaction**:
   the intent to notify can never be lost with the booking.
5. **Notifications run after the commit**, in the request's fault context. A
   failure is recorded on the outbox row and the booking stands; the explicit
   delivery route answers `502 notification_undeliverable` with
   `booking_still_committed: true` rather than pretending the failure was
   swallowed.
6. **`timeout_after_commit` writes the appointment and withholds the
   notification** (the real system lost the response, so the confirmation never
   went out): reconciling with the same key answers `created`, `state:
   CONFIRMED`, `reconciliation: resolved_booked`. `timeout_before_commit` writes
   a `REQUESTED` booking with `appointment_committed: false` and no appointment
   row at all, and `200`/`202` are never involved: the caller gets a `504` whose
   envelope carries the reconcile path.
7. **`expected_slot_version` is a real compare-and-set.** The refusal reports the
   version the caller presented, the version the hospital reports as current, and
   the stored `slot_version` as three separate facts. Under the armed
   `stale_slot_409` fault the refusal is issued *without* running the claim: the
   fault stands for the hospital's answer, so the row is untouched — and the
   envelope quotes the caller's own number, or `null`, never a version nobody
   sent.
8. **Cancelling** returns the slot to the board when no live appointment still
   claims it. **Rescheduling** secures and verifies the replacement before
   cancelling the original; if the replacement is refused, the original keeps its
   state and the refusal never looks like a cancellation.
9. **The outbox respects the retry backoff on automatic passes** (the delivery
   after a commit, `POST /notifications/drain`) and ignores it for an explicit
   request (`POST /notifications`, the single-entry retry), because "retry this
   one" answered with "nothing was due" is a silent no-op. Attempts still count,
   so a provider that keeps failing reaches `abandoned` instead of retrying
   forever.

### D11 — Container interface

Build context is the repository root — the service *serves* `fixtures/`, and the
workspace install is driven by the root lockfile — with the Dockerfile at
`services/mock-api/Dockerfile`, matching `compose.yaml`. The image carries
`fixtures/`, runs as a non-root user, keeps its SQLite file in `/app/.data`
(mount a volume there to survive a `restart`), pins the same pnpm version the
repository pins, and installs `curl` because the health probe needs a client.
`GET /healthz` exists as the probe spelling of `GET /health`.

## Consequences

- `pnpm --filter @referraleasy/mock-api test` is green: 12 files, 62 tests, no
  network, no sleeps that are not fault delays. The five invariants are asserted
  directly (double booking, reconciliation with the same key, notification
  failure leaving the booking intact, review-queue honesty, `outcome_unknown` as
  its own outcome), and every authored fixture is re-checked field by field where
  the contract freezes it.
- The service is a **single-process, single-writer** store. That is the right
  trade for a demo (nothing to install, one file to delete) and it is not a
  deployment design: SQLite concurrency is handled with `BEGIN IMMEDIATE` and a
  busy timeout, not with replicas.
- Delivery is simulated: a failed send is a row and an HTTP answer, not a
  provider's error. The outbox state machine (`pending → sent`, `failed →
  abandoned`) is real, its backoff is real, and the ceiling is configurable.
- Requests are validated by the transcribed schemas, so a contract change
  published in `packages/contracts` does **not** automatically reach this service
  — it reaches it when `src/contracts/types.ts` is re-pointed and the
  transcription is deleted. Until then, the served OpenAPI is the honest
  statement of what the service accepts.
- The fault engine's counters live in SQLite with the rest of the state, so a
  spent occurrence survives a restart instead of silently re-arming.

## Open questions / follow-ups

1. **The published package has no request schemas.** Coverage, availability and
   booking requests are transcribed locally (D6). If request shapes are added
   there, they replace the local ones; the seam file already exists.
2. **Fixture-side divergences** for the fixtures owner (ADR-0024), each pinned in
   a test so it cannot drift quietly:
   - `availability_results.json` expects 3 slots where the declared window holds
     4 (slot `…0005` is available and inside it), `no_slots_for_constraints`
     where four available slots sit in the window (the Friday preference),
     1 slot where slot `…0012` is also available, and `no_matching_provider` for
     a provider who does have an available first visit at that hospital
     (slot `…0027`).
   - `coverage_results.json` row `…0012` nulls `accepts_insurance` and
     `prior_auth_required` that its own provider listing and plan assert — and
     that row `…0005` of the same plan reports as `false` — and cites
     evidence `…0020`, which belongs to a different provider; no evidence row in
     the set names the provider that row is about. Row `…0009` nulls the same
     plan-level field.
   - `fault-scenarios/stale_slot_409.json` declares `current_version: 3` while
     `directory/slots.json` serves slot `…0013` at version 2. The refusal reports
     the fixture's number as the hospital's claim and the stored version as its
     own, so the disagreement is visible rather than averaged away.
3. **No root `.dockerignore`.** The image build copies the repository root
   context; a `.dockerignore` would cut the transfer time, but that file belongs
   to the repository root rather than to this service.
4. **`compose.yaml` declares no volume for `mock-api`.** Bookings made in a
   container are lost on `docker compose down`; the Dockerfile documents the
   mount point (`/app/.data`) for whoever wires that.
