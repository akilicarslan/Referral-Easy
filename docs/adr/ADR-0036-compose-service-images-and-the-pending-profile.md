# ADR-0036 — Compose service images: one agents image, one workers image, and the `pending` profile

- **Status:** Accepted
- **Date:** 2026-10-15
- **Phase:** Phase 0 → Phase 1 boundary — the local topology `make up` starts (§3, §5, §7, §18, §22)
- **Depends on:** §18 (one agent image, role by `AGENT_ROLE`), §7 (the outbox queue is a Postgres table),
  §22 (this ADR is the only file in `docs/adr/` this phase adds), ADR-0005 (the local guardrail
  classifier), ADR-0023 (compose topology), ADR-0030 (the agent runtime the agents image executes),
  ADR-0035 (the workers package the workers image executes)

## Context

`compose.yaml` described services that could not exist. Five of its entries pointed at Dockerfiles
that were not in the repository (`worker` and `scheduler` at `services/workers/Dockerfile`,
`local-guardrail` at `services/agents/Dockerfile`), and the seven specialist agents §3 and §18 require
had no entries at all. `make up` is `docker compose --profile core up -d --wait`, so every one of those
gaps reached the user as a failed command.

Three of the missing files were this phase's to write. Two of the *entries* were not fixable by writing
a Dockerfile, and finding out which was which is most of the work this ADR records:

1. **`local-guardrail` is not an agent.** §18 lists it beside the seven roles as an *addition* ("Ekler:
   web, gateway, mock-api, … worker, scheduler, local-guardrail"), and §9's layer 3 is a local ONNX
   sentence encoder (ADR-0005) — a CPU model, not an A2A service: it publishes no Agent Card, holds no
   tool allowlist, and answers no task envelope. The Phase 0 entry built it from
   `services/agents/Dockerfile`, which was the guess that the agents image would host it as an eighth
   role. There is no eighth role, and `roles.py` has no vocabulary for one.
2. **`scheduler` has no implementation.** The only long-running process in `services/workers` is the
   outbox drain, and `referraleasy_workers/__main__.py` is explicit that a second one would be a second
   owner of the same `SELECT ... FOR UPDATE SKIP LOCKED` rows.

The two entries that could not be honestly implemented were therefore the test: the topology's job was
either to say so, or to keep printing alarms about a Dockerfile and leave the impression that a file was
all that was missing.

## Decision

### One image per *role family*, the role chosen at run time

Two images, both built from the repository root context because both are uv workspace members and the
install is driven by the root lockfile:

| Image | Dockerfile | Serves | Chosen by |
| --- | --- | --- | --- |
| `referraleasy/agents:dev` | `services/agents/Dockerfile` | seven specialist agents | `AGENT_ROLE` |
| `referraleasy/worker:dev` | `services/workers/Dockerfile` | `worker`, and `scheduler`'s refusal | `SERVICE_ROLE` |

Seven Dockerfiles would be seven places for the tool allowlist (§3), the budget caps (§8) and the pinned
model (§8) to drift apart. The runtime already treats a role as deployment configuration, not as code
(`packages/agent-runtime/.../roles.py`), so the image can too: `services/agents/Dockerfile` copies its
seven entry-point directories under `/app/roles/<role code>` and its CMD is
`python "/app/roles/$AGENT_ROLE/__main__.py"`.

The **role code, not the directory name, is the deployment identifier**: the repository's directories are
service names (`intake`, `coverage`) and the role codes are agent names (`referral_intake`,
`coverage_network`). Only the role code goes on the wire, so the image's directories and the compose
service names are both named after the code. `service_name(role)` is `agent-<role code>`, which is why
each compose entry is `agent-care_coordinator` … `agent-recovery_handoff`, and why the peer map can be
one variable:

```yaml
A2A_BASE_URL: http://agent-{role}:8080
```

`HttpDelegation.url_for` substitutes the *target* role's code (`self._pattern.replace("{role}",
role.value)`), so this one line resolves to the service names above — rather than seven URLs that can
disagree with the services they point at.

### `scheduler` keeps its entry and refuses its own role

`services/workers/Dockerfile` serves `scheduler` in the only way that is true today: its arm of the CMD
prints why the role has no implementation and exits 64. Nothing reads `EVAL_CRON` or `EVAL_TIMEZONE`
(they appear in `compose.yaml`, in `.env.example`, and in no source file), `referraleasy_eval` does not
exist as a package, and the Phase 0 probe on port 8082 described a server that was never written.

The alternative — falling through to the outbox loop — would run a second owner of the same queue rows
while claiming to be a scheduler, which is worse than not starting.

### `local-guardrail` is declared, not faked

The service needs an image of its own over `packages/guardrails`: a small HTTP wrapper listening on
`PORT` with `/healthz` beside it. Three measurements say that is a service that does not exist yet:

- `packages/guardrails` is a library. It is not a uv workspace member (it is not in `uv.lock`), it has
  no `__main__`, and it has no HTTP app.
- The gateway consumes the three-layer pipeline **in-process** — its own config resolves
  `GUARDRAIL_MODE` — so nothing in this stack opens a socket to port 8090. Grepping the repository for
  `local-guardrail` finds `compose.yaml`, `infra/compose/README.md` and `IMPLEMENTATION_TODO.md`, and
  nothing else.
- ADR-0005 leaves layer 3's threshold unmeasured, which makes the stage announce itself inert
  (`threshold_unmeasured`) even when it is correct.

So the entry keeps the interface it was written against and points its `build.dockerfile` at
`services/guardrails/Dockerfile` — a file that does not exist, named because naming it is the point.
`services/agents/Dockerfile` states in its header that it does not and could not serve this service.

### The gap is a profile, not a probe

`scheduler` and `local-guardrail` carry `profiles: [pending]`. `make up` activates `core` only, so no
run reaches them, and `make down` does not name `pending` because nothing in it can be running. The
entries stay in the file because a reviewer reading the topology should see what the platform intends
and what it cannot yet do, in the same place.

They carry **no `depends_on` and no `healthcheck`**, for two reasons that are the same reason:

- A container whose command exits 64 has nothing to wait for, and a probe could only ever report
  "unhealthy" — which is how the Phase 0 entries described services that did not exist.
- A dependency on a `core` service makes `--profile pending` on its own an invalid project
  (`service "local-guardrail" depends on undefined service "otel-collector"`), turning a stated gap into
  an error about profiles. Measured: with the dependencies removed, `docker compose --profile pending
  config -q` passes and `docker compose --profile core --profile pending config -q` passes.

### The worker's probe observes two things, and says what it does not

`worker` opens no port, so no probe can ask it a question over HTTP. The entry probes what can actually
fail:

1. **PID 1 is still the drain loop** — `tr '\0' ' ' < /proc/1/cmdline | grep -q "referraleasy_workers
   outbox"`. The CMD `exec`s the loop, so a dead loop is an *exited container* (restart policy
   `unless-stopped`), not an unhealthy one; this arm catches a substituted or non-exec'd command.
2. **The database it drains is reachable** — a `psycopg.connect` to `DATABASE_URL` with
   `connect_timeout=3`.

**The first version of this probe used `pgrep -f "referraleasy_workers outbox"`, and that arm was
vacuous — measured, not reasoned about.** `pgrep -f` matches any process whose command line contains
the pattern, and the shell that runs the probe string carries the pattern as one of its own arguments,
so a container started with the probe's own test string and no worker in it at all still reported a
match. Both `Compose`'s probe and the image's own `HEALTHCHECK` now read `/proc/1/cmdline`, and the
image installs no `procps` because nothing needs it any more. Verified in both directions: exit 0
against the running worker, exit 1 against a container whose PID 1 is `sleep 300` with the same
`DATABASE_URL`, and exit 1 when only the `DATABASE_URL` is unreachable.

What neither arm establishes is **progress**: a loop blocked on an unreachable notification endpoint
passes both. The loop is the cover for that case — a failed delivery is recorded on the row with its
backoff and the pass ends, so it fails visibly instead of idling silently — and the
`outbox: batches=… claimed=… delivered=…` line it logs on every pass is the evidence of progress.

### Neither image needs a model at startup

`.env` may hold `MODEL_BACKEND=live` with real credentials, so this is a property of the images and not
of the profile. `AgentSettings.from_env` resolves the model reference purely (no SDK construction, no
socket), `build_service` builds an httpx client and a lazy psycopg seam without opening either, and
`build_model`/the ADK `Runner` are reached only inside a turn. The workers image has no model
dependency at all. A `GET /healthz` cannot cause a call either: it is the A2A app's one
unauthenticated route, deliberately, because a probe holding a service token would report "unhealthy"
whenever the token was the broken thing.

### Two `uv sync` passes, and why `--no-install-workspace` is load-bearing

Both images copy **every** workspace member's manifest first (nine, not one), because uv resolves the
workspace from the root manifest and fails the whole build with `Distribution not found at:
file:///repo/<member>` when one is missing. The first sync then installs third-party dependencies only:

```
uv sync --frozen --package referraleasy-agents --no-dev --no-editable --no-install-workspace
```

`--no-install-workspace` is not tidiness. That layer runs *before* the sources are copied, so
"install the workspace members" there means building a wheel of a package whose `src/` is not in the
tree: the build succeeds, the member gets a dist-info directory, and what it installs is nothing. The
second sync then sees those members as already satisfied and leaves them empty. That is how the agents
image first came up with `referraleasy_agent_runtime` missing from a venv that had all five members'
metadata. The second pass, after the sources are copied, installs the real wheels.

## Consequences

### What was verified, and with which commands

- `docker compose --profile core build` → **fails**, on two services: `mock-api` and `gateway`. Neither
  failure is in this phase's files (see below). `referraleasy/agents:dev` and `referraleasy/worker:dev`
  build clean.
- `docker compose --profile core up -d --wait` → **exit 1**, for the same `gateway` reason. It does not
  recreate `postgres`, `opensearch`, `otel-collector` or `data-prepper`: the dry run shows them as
  "Healthy"/"Waiting" only, and their `StartedAt` timestamps and restart counts are unchanged after the
  attempt (0 restarts each).
- `docker compose --profile core up -d --wait --no-deps worker agent-…` (all seven) → **exit 0**, eight
  containers "Up (healthy)".
- `docker compose ps` → 12 running: the four storage/telemetry containers, the seven agents, and the
  worker.
- `uv run --no-sync python scripts/health.py` → `All 5 probed service(s) answered.` (postgres,
  opensearch, otel-collector, data-prepper, mock-api). `gateway` reports `NO PROBE (GATEWAY_URL is not
  set)` and is not counted as healthy. The script probes none of the agents and not the worker.
- Per agent: `GET /healthz` → 200 on 8101–8107; `GET /manifest` → 401 without a token, 200 with
  `A2A_SERVICE_TOKEN`; the coordinator's manifest reports
  `component: agent-care_coordinator, role: care_coordinator, app_env: demo`.
- Refusal paths: `docker run --rm referraleasy/agents:dev` (no `AGENT_ROLE`) → message + exit 64;
  `SERVICE_ROLE=scheduler` → message + exit 64; `SERVICE_ROLE=nonsense` → message + exit 64.
- `docker compose logs worker` shows the loop's pass summaries. No provider call was made by any
  container: no container logs a model interaction, and startup resolves the model reference without
  contacting anyone.

### What still does not work, and whose files are in the way

`make up` cannot complete today for two reasons, neither of them a missing file and neither of them in
this phase's scope (`services/gateway/**` and `services/mock-api/**` belong to other phases):

1. **`services/gateway/Dockerfile` predates two workspace members.** It copies seven of the nine member
   manifests, so `RUN uv sync --frozen --all-packages --no-dev --no-editable` fails with
   `Distribution not found at: file:///repo/services/agents`. `--all-packages` also *builds* every
   member's wheel, so the fix is either to add
   `COPY services/workers/pyproject.toml services/workers/` and
   `COPY services/agents/pyproject.toml services/agents/` **plus their `src/` trees**, or — better, and
   what this phase's Dockerfiles do — to select the one package the image serves and use the two-pass
   `--no-install-workspace` sync above.
2. **`services/mock-api/Dockerfile` predates its own source.** `services/mock-api/src/contracts/enums.ts`
   now imports `@referraleasy/contracts`, whose `types` field points at `./dist/index.d.ts`, and `dist/`
   is gitignored (`.gitignore` line 36). The Dockerfile copies only
   `packages/contracts/ts/package.json`, so nothing resolves in the image:
   `error TS2307: Cannot find module '@referraleasy/contracts'`, plus a downstream
   `error TS2322 … 'number | undefined'` at `src/booking/service.ts(61,3)` that does not reproduce on
   the host (`pnpm --filter @referraleasy/mock-api typecheck` passes, because the host has a built
   `dist/`). The fix is to copy `packages/contracts/ts` (`package.json`, `tsconfig.json`, `src`) and
   build it before the service build — an image cannot rely on a gitignored artefact.
3. **Host port 4010 is held by a host-run mock API** (`pnpm --filter @referraleasy/mock-api dev`), so
   the container of the same name cannot bind it. The stack's containers address `mock-api:4010` by
   service name and get nothing until that process stops or a `MOCK_API_HOST_PORT` parameter is added —
   which would be a contract change `.env.example` does not document, so it is not made here. Note that
   `scripts/health.py`'s `mock-api OK` line is answered by *that host process*, not by a container.

### Resolved, 2026-10-16 (UTC): every image in `core` builds

`docker compose --profile core build` → **exit 0**, and `docker compose --profile core --profile
showcase up -d --wait` → **exit 0** with `web` serving. Both failures above are fixed, and the way the
first one was fixed is worth recording here, because it broke again twice.

- **`services/gateway/Dockerfile`** copies every workspace member's manifest and uses the two-pass
  `--package` sync this ADR recommended. That is what the fix above said to do, and it is what failed
  next: `packages/eval`, `packages/alerts` and `packages/release-registry` each joined the workspace and
  each broke the build until someone appended it. The breakage is worse than a red build — the gateway
  *depends* on `referraleasy-alerts`, so the image went from unbuildable to "buildable from a stale
  checkout", and nothing in the suite read a Dockerfile, so nothing said so.
- **`services/workers/Dockerfile`** had the same defect in a quieter form: it copied
  `packages/telemetry`'s manifest and not its source, so `referraleasy-workers` — which declares that
  dependency — would have failed its second `uv sync`.
- **`apps/web/Dockerfile`** had it in the frontend: `admin.html` was declared as a third
  `vite.config.ts` build input and left out of the `COPY` line, so the image failed with
  `Could not resolve entry module "admin.html"` after the whole pnpm store had been installed.
- **`services/mock-api/Dockerfile`** copies `packages/contracts/ts` and builds it before the service, as
  fix (2) above describes.

`tests/test_docker_member_lists.py` now fails when a member's manifest is missing from any Dockerfile
that runs `uv sync`, when a member in an image's dependency closure has no source copied, or when a Vite
entry point never reaches the web image. It found the workers gap on its first run.

**The lesson worth keeping:** "add the member to the COPY list" is a remedy that decays. This ADR
recorded it, it was applied, and it failed twice more. The enumeration is the fragile thing and the guard
is the part that holds — which is why the guard exists rather than a longer comment.

### Divergences recorded (files this phase may not edit)

- `infra/compose/README.md` still describes `local-guardrail` as "built from
  `services/agents/Dockerfile`" and lists `scheduler` in the `core` profile's service list. Both are now
  wrong (again: the first was always wrong).
- `.env.example` documents `MODEL_BACKEND=live` and the `EVAL_*` knobs but not `NOTIFICATION_URL` or
  `BOOKING_API_URL`, which `worker` cannot start without. `compose.yaml` sets both to
  `http://mock-api:4010`; the model file should say the same.
- `IMPLEMENTATION_TODO.md` line 253 describes the seven agents as having no compose entry.

## Alternatives considered

**Seven Dockerfiles, one per specialist.** Rejected: §18 says one image and a role. Seven files is seven
places for the tool allowlist, the budget caps and the pinned model to drift, and the role is already
deployment data in the runtime.

**Writing a `scheduler` loop so the entry could start.** Rejected as fabrication: no code reads
`EVAL_CRON` or `EVAL_TIMEZONE`, and a job that samples nothing while reporting healthy is the failure
mode the repository's health and evaluation rules exist to prevent. The role is refused with a reason
and exit 64 instead.

**Serving `local-guardrail` from the agents image because compose once pointed at it.** Rejected: the
image has no role for it, the service has no HTTP contract, and nothing calls it. Building it correctly
means an HTTP wrapper this phase does not own over a package that is not a workspace member, for a
stage ADR-0005 already documents as inert until its threshold is measured.

**Leaving `scheduler`/`local-guardrail` in `profiles: [core]` and letting `--wait` time out.** Rejected:
one service that can never become healthy makes `make up` fail for a reason that has nothing to do with
the platform, which is how a working stack gets the reputation of a broken one.

**Deleting the two unimplementable entries.** Rejected: the topology is the place a reviewer looks to
learn what the platform intends. A profile that says "declared, unbootable, here is why" is more useful
than a service that silently vanished, and `pending` is not a profile any normal command activates.

**`pgrep -f` for the worker probe, with the loop's name in the pattern.** Rejected on measurement: the
probe's own shell matched, so the arm could never fail (see above).

**A `MOCK_API_HOST_PORT` parameter to work around the host process on 4010.** Rejected for now: it is a
contract change (`.env.example` would have to document it), the collision is a property of one
developer's machine rather than of the stack, and the honest report is the cheaper artefact.
