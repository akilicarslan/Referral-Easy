# referraleasy-gateway

The patient-facing HTTP surface (§9, §12): two session kinds, REST commands that refuse a stale
`workflow_state_version`, a replayable SSE stream, a policy check that consults the policy **table**,
and the guardrail seam that keeps the booking write closed while the model layers are unconfigured.

Nothing here re-implements a rule that the database enforces. State transitions, ownership, approvals,
command dedup and the policy rows are `migrations/`; this service reaches them through
`referraleasy_domain`, and the one place a rule *is* expressed in Python — the offline memory profile
— says so in its own docstring and is re-derived from migration 014 by a test.

```
services/gateway/
├── Dockerfile
├── pyproject.toml
├── README.md
└── src/referraleasy_gateway/
    ├── app.py          wiring: settings in, an ASGI app out
    ├── api.py          routes — transport only
    ├── commands.py     the four operations, assembled from the ports
    ├── control.py      the control-state port + the memory and postgres profiles
    ├── psql.py         the one place a connection is opened, and the one place it is translated
    ├── policy.py       the action→state table as data
    ├── policy_seed.py  the shipped rows, transcribed from migration 014
    ├── guardrails.py   the three-layer screen (§9)
    ├── approval.py     approval binding: patient + workflow + version + payload hash + expiry
    ├── events.py       content-addressed event ids, monotonic `seq`, replay
    ├── surfaces.py     card catalog, severability order, partial-output buffer
    ├── auth.py         patient/admin JWT sessions
    ├── errors.py       one failure envelope
    ├── timing.py       clocks, and progress latency kept apart from final latency
    ├── config.py       the environment contract and its startup refusals
    └── __main__.py     `python -m referraleasy_gateway`
```

## Routes

| Method | Path | Session | What it does |
| --- | --- | --- | --- |
| GET | `/healthz` | none | Liveness + which guardrail layers have an implementation |
| POST | `/v1/auth/patient-session` | none (demo only) | Mint a local fixture patient token |
| POST | `/v1/auth/admin-session` | none (demo only) | Mint a local fixture coordinator token |
| POST | `/v1/demo/workflows` | admin (demo only) | Seed a workflow |
| GET | `/v1/workflows/{id}` | patient | State, version, outstanding cards, stream cursor |
| GET | `/v1/workflows/{id}/help` | patient | The static help card. Served when the guardrails are down |
| POST | `/v1/workflows/{id}/actions` | patient | A card action the server issued |
| POST | `/v1/workflows/{id}/messages` | patient | Free text, screened by the three layers |
| GET | `/v1/workflows/{id}/events` | patient | SSE: replay from `Last-Event-ID`, then live |
| POST | `/v1/workflows/{id}/transitions` | admin | Move the workflow; `complete: false` streams and commits nothing |
| POST | `/v1/workflows/{id}/bookings` | admin | The booking write (§9's gate). `outcome` (`confirmed`/`unverified`, or omitted — `external_ref` then decides it) and `external_ref` are the scheduler's report, and they travel with the write |
| GET | `/v1/admin/workflows/{id}` | admin | One workflow, as a coordinator sees it |
| GET | `/v1/admin/metrics` | admin | Both latencies, and the guardrail layers that actually ran |

A patient token presented at an admin route is refused by the verifier with `403` — the split is in
the dependency, not in a branch inside a handler.

## Running it locally

```bash
export JWT_PATIENT_SECRET=dev-patient-secret-at-least-32-bytes
export JWT_ADMIN_SECRET=dev-admin-secret-at-least-32-bytes
export APP_ENV=demo
PYTHONPATH=services/gateway/src:packages/contracts/python/src:packages/domain/src \
  uv run --no-sync python -m referraleasy_gateway
```

In the compose profile none of that is needed: `env_file` supplies the fixture secrets and the base
image runs the module directly.

## A demo pass, end to end

```bash
GW=http://localhost:8080
ADMIN=$(curl -s $GW/v1/auth/admin-session -H 'content-type: application/json' \
        -d '{"subject":"coordinator-1"}' | python -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')

WF=$(curl -s $GW/v1/demo/workflows -H "authorization: Bearer $ADMIN" -H 'content-type: application/json' \
     -d '{"patient_ref":"patient-1"}' | python -c 'import json,sys;print(json.load(sys.stdin)["workflow_id"])')

PATIENT=$(curl -s $GW/v1/auth/patient-session -H 'content-type: application/json' \
          -d '{"subject":"patient-1","patient_ref":"patient-1"}' | python -c 'import json,sys;print(json.load(sys.stdin)["access_token"])')

# The coordinator moves the workflow; the move is what issues the card.
curl -s $GW/v1/workflows/$WF/transitions -H "authorization: Bearer $ADMIN" -H 'content-type: application/json' \
  -d '{"command_id":"c-1","expected_state_version":1,"to_state":"PROVIDER_MATCHING"}'
curl -s $GW/v1/workflows/$WF/transitions -H "authorization: Bearer $ADMIN" -H 'content-type: application/json' \
  -d '{"command_id":"c-2","expected_state_version":2,"to_state":"AWAITING_HOSPITAL_CHOICE",
       "props":{"options":[{"id":"h-1","name":"Demo General"},{"id":"h-2","name":"Demo University"}]}}'

# The patient sees the card and the stream.
curl -s $GW/v1/workflows/$WF -H "authorization: Bearer $PATIENT"
curl -sN "$GW/v1/workflows/$WF/events?token=$PATIENT"

# The booking write is refused while the guardrail model layers are unconfigured — and the static
# help card keeps working. That is the §9 rule this phase is measured on.
curl -s -o /dev/null -w '%{http_code}\n' $GW/v1/workflows/$WF/help -H "authorization: Bearer $PATIENT"
```

## Environment

| Variable | Meaning | Default |
| --- | --- | --- |
| `APP_ENV` | `demo`/`test`/`ci` enable the fixture routes; anything else does not | `demo` |
| `HOST`, `PORT` | bind address | `0.0.0.0`, `8080` |
| `JWT_PATIENT_SECRET`, `JWT_ADMIN_SECRET` | the two signing keys. No default — a blank key is a startup failure | — |
| `JWT_ISSUER`, `JWT_PATIENT_AUDIENCE`, `JWT_ADMIN_AUDIENCE` | issuer/audience claims | `referraleasy-local`, `referraleasy-patient`, `referraleasy-admin` |
| `JWT_TTL_SECONDS`, `JWT_CLOCK_SKEW_SECONDS` | session lifetime, tolerated skew | `3600`, `30` |
| `GUARDRAIL_MODE`, `GUARDRAIL_DECISION_MODEL`, `GUARDRAIL_LOCAL_MODEL` | layer 2/3 configuration. Empty means **not configured**, which is a refusal for writes, not a fallback to allow | `three_stage`, ``, `` |
| `GATEWAY_STREAM_HOLD_MS`, `GATEWAY_STREAM_HEARTBEAT_MS`, `GATEWAY_STREAM_REPLAY_LIMIT` | stream behaviour. `0` drains and closes | `25000`, `15000`, `500` |
| `GATEWAY_CONTROL_BACKEND`, `DATABASE_URL` | `memory` (default) or `postgres`. The postgres profile opens `DATABASE_URL` at startup and refuses to serve unless it answers *and* has the migrations (see below) | `memory`, — |
| `GATEWAY_WORKER_ID` | the identity §7's optimistic check claims workflows under. Defaults to `gateway:<host>:<pid>`; only read by the postgres profile | — |

## What is not implemented, and why

Stated here rather than in a commit message, because a reader of this directory is the person who
would otherwise assume the opposite:

1. **The postgres profile is implemented, and the boundaries it cannot yet cross are recorded as
   tests.** `PostgresControlState` performs all eight port operations against the schema, so §7's
   transition table, ownership, optimistic version check, command dedup and §9's approval binding are
   enforced *by PostgreSQL* rather than mirrored in Python; `app._control_state` selects it and calls
   `PostgresControlState.ping()` before the app is returned, so a wrong DSN or an unmigrated database
   is a refusal to start rather than a process that is healthy and fails every request. The `refuse_*`
   function this replaces is gone rather than narrowed: no profile is unimplemented now, and the
   question it asked ("is this label true?") is answered by the database. What the profile still
   cannot do, each one asserted in `tests/gateway/test_postgres_control_state.py`:
   * **Two clocks.** §9's expiry is the database's `now()`; the gateway's clock is its own. They agree
     because `PostgresApprovalLedger` returns the row the database stamped, which means a test cannot
     expire a consent by moving a `ManualClock`.
   * **§12's event log does not survive a restart, in either profile.** There is no table for it —
     `audit` is the tamper-evident chain, not the transport log — so a reconnecting client is served
     from a store that knows where the workflow is and a log that knows nothing about how it got
     there. Tested rather than described.
   Two boundaries were gaps here and are not any more; they are described because a reader who knew
   the earlier version would otherwise still assume them:
   * **A booking and its transition are one transaction.** `record_booking` takes the outcome the
     scheduler reported and, when the caller passes `advance_to`, writes the row and moves the
     workflow in a single transaction — which is what lets a *confirmed* booking reach `BOOKED`, since
     014's `workflow_booking_consent_guard` asks for one, and what keeps a refused transition from
     leaving a booking row behind. §7's rule is about transactions that outlive the work; this one
     commits its statements and ends. One statement is not available: data-modifying CTEs in a
     single statement cannot see each other's rows, so an INSERT + UPDATE + transition cannot share
     one, and 008's `booking_request_guard` is a BEFORE UPDATE trigger — the outcome can only arrive
     by updating the row the insert wrote.
   * **Two vocabularies for one state.** The contract's `BookingState` is six values; the column's is
     `pending|confirmed|failed|cancelled`. The two meet in `control._BOOKING_COLUMN` and nowhere else;
     `BookingRow.state` is the contract's, in both profiles. Only four of the six are *recordable*:
     `PENDING_REVIEW` and `CANCEL_PENDING` describe a booking nobody has written yet.
   A booking request speaks the same vocabulary: `outcome` is `confirmed` or `unverified`. The field
   may be left out, and then the `external_ref` beside it decides what was meant — a hospital's own
   reference is a confirmation, and nothing beside it is `unverified` (`commands._outcome_for`, the
   one place that inference is made). An *explicit* `confirmed` with no reference is refused as a
   `422` naming 008's `booking_request_confirmed_has_ref`, rather than derived: a caller claiming a
   confirmation it cannot evidence is telling the gateway something provably incomplete. Tests that
   cannot pass
   in the postgres profile carry `memory_only` and are listed, with reasons, in `MEMORY_ONLY` in that
   module; a guard test fails if the set grows or goes stale. It is down to two — §9's expiry, which is
   a question about whose clock decides it, and one test that reaches its assertion by moving a booked
   workflow back into `BOOKING_IN_FLIGHT`, which 014 refuses — after the booking outcome closed the
   nine that were the same finding.
2. **Guardrail layers 2 and 3 have no implementation.** `DecisionModelGuardrail` and
   `LocalClassifierGuardrail` take an injected `screen` callable and are constructed *without* one,
   so `is_configured()` is `False` whatever the environment claims. They raise `GuardrailUnavailable`
   rather than returning "allow", the pipeline's `require_complete()` turns that into a `503`, and a
   booking therefore cannot be written in this phase. Layer 1 (regex: emergency, injection,
   exfiltration, profanity-with-a-request, and short-reply state awareness) is real and tested.
3. **Sessions are HS256 with stdlib `hmac`.** No JWT library is a dependency of this workspace, so
   `auth.py` implements HS256 verification directly: base64url, constant-time compare, and explicit
   refusals for `alg: none`, a tampered signature, a wrong audience, a wrong kind, and an expired
   token. What is *not* covered is the algorithm-agility and key-management surface a full library
   brings — this verifier accepts exactly one algorithm because it is a literal in the module.
4. **The `X-Trace-Id` header is correlation only.** It is never read as an identity, and a request
   carrying a trace id and no credential is refused like any other (tested).
5. **The Dockerfile is unbuilt.** It is written to the compose contract (repo-root context, `curl`
   for the probe, exec-form `CMD`), but `uv sync --frozen` inside it requires `uv.lock` to contain
   `referraleasy-gateway`, which happens when the workspace member is locked at the repository level.
   No image has been built from it in this phase.
6. **`apps/web` does not yet send a credential.** The browser client currently issues unauthenticated
   fetches, so interop needs one of: the demo fixture token routes plus a query-token stream (the
   demo profile allows both), or a real provider in the Cloud Run profile. This is an integration
   gap between phases, not a gateway decision — the gateway refuses anonymous requests by design.

## Tests

```bash
# No database: the memory profile, plus the tests that ask PostgreSQL what it enforces (skipped,
# because there is nothing to ask; CI runs them in the job that provides a server).
uv run --no-sync pytest tests/gateway -q
uv run --no-sync ruff check services/gateway tests/gateway

# Both profiles, the same assertions twice — every test is parameterised over the control state:
TEST_DATABASE_URL=postgresql://referraleasy:referraleasy@localhost:55433/referraleasy \
  uv run --no-sync pytest tests/gateway -q
```

`tests/gateway/conftest.py` adds the three source roots to `sys.path` — the same fallback
`tests/skills/conftest.py` uses — so the suite runs without the workspace being synced.
