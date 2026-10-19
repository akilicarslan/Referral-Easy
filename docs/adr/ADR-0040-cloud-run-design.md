# ADR-0040 — Cloud Run: what the code already satisfies, and the four things it does not

- **Status:** Accepted as a design. **Nothing is deployed, and this file does not claim otherwise** —
  every claim below is either a property this repository tests or a gap it names, and the difference
  is marked. The deployment itself is not attempted: the demo's budget is a laptop, and a deployed
  stack would spend money to prove something the design can state.
- **Date:** 2026-10-15
- **Phase:** Phase 8.3 (§22's Cloud Run paragraph, §18's profiles, §9's service identity)
- **Depends on:** §22 (the four requirements and the two warnings), §9 (session identity, which the
  profile changes), §7 (workflow ownership under a second replica), §16 (the kill switch), §18
  (profiles), ADR-0023 (compose topology), ADR-0031 (SSE and auth), ADR-0035 (workers and the human
  queue), ADR-0039 (the admin read API)

## The requirement

§22 says, in full:

> Cloud Run yaklaşımı: stateless request handler, paylaşımlı session/task, load balancer, graceful
> shutdown, concurrency/min/max instance tasarımı. İki replica ile aynı command test edilir. A2A tek
> başına autoscaling sağlamaz; Compose da Cloud Run autoscaler'ı değildir. Request sonrası background
> process'in çalışacağına güvenilmez; worker ayrı servis olur.

Five requirements, two warnings about things that look like they would work and do not. Each is taken
in turn below, with what this repository has.

## What already holds

**Stateless request handlers.** The gateway holds no request state across calls: a session is a
signed JWT, a workflow is a row, a booking is a row, and the in-process pieces are *caches* rather
than sources of truth. This is a property of the `GATEWAY_CONTROL_BACKEND=postgres` profile, and it is
the profile's whole point — the `memory` profile is a test double for the port, not a deployment mode.
The one thing a replica must not share loosely is `GATEWAY_WORKER_ID`: §7 gives a workflow exactly one
owning worker, and two replicas presenting the same id would take each other's claims. `config.py`
already resolves it to `gateway:<hostname>:<pid>` by default and documents `GATEWAY_WORKER_ID` as the
override for a deployment that wants a name outliving the process — which Cloud Run needs, since a
revision's pid is not stable. **The value to set is per-revision, not per-service**, and that is a
deployment decision rather than a code one.

**Shared session and task state.** §22's `paylaşımlı session/task` is satisfied by PostgreSQL. A
second replica reads the same `workflow`, `session`, `task` and `command_dedup` rows through the same
SQL functions, and the functions are where the compare-and-set lives. This is tested rather than
asserted: `tests/workers/test_ownership.py::test_two_replicas_cannot_both_own_one_workflow` claims one
workflow from two connections and requires exactly one winner, refusing the loser at both the stale
generation and the current one. *(Database-gated: it skips without `TEST_DATABASE_URL`, and CI's
database job fails the build on a skip.)*

**The load balancer, with one caveat.** Cloud Run fronts every service with one, so this is a
deployment property rather than something to build. The caveat is SSE: `/v1/workflows/{id}/events`
holds a connection open, and Cloud Run's request timeout must exceed the stream's hold —
`GATEWAY_STREAM_HOLD_MS` defaults to 25 s and the heartbeat is 15 s, both well inside the 300 s
default and both configurable. `proxy_buffering off` in `apps/web/nginx.conf` is the same requirement
one layer up.

**Execution topology needs an update.** `compose.yaml` runs the existing outbox worker as a separate
service. ADR-0041 adds a durable coordination poller to the continuously running gateway container.
Before a request-only Cloud Run deployment, move that execution loop to a separate service. A
stored job alone does not provide CPU after the request ends.

**The A2A warning is respected.** §22 says A2A alone provides no autoscaling, and this repository does
not pretend it does: the agent services are ordinary HTTP containers, and nothing scales them by
convention.

**Compose is not the Cloud Run autoscaler.** Also respected, and stated because it is the mistake
that would look like progress: `compose.yaml`'s `deploy.resources.limits` are a host-level cap that
Docker Compose honours and nothing scales on. A second `gateway` container in compose proves the code
survives two replicas; it proves nothing about Cloud Run's concurrency or its instance bounds.

## What does not hold, and needs work before a deployment

**1. A shutdown path exists, and the stream hold can outlast Cloud Run's grace period.** This is the
one gap that is a *number* rather than a missing feature, and the first version of this section got it
wrong in a way worth recording: "no graceful shutdown exists" is false. `services/gateway/Dockerfile`
ends with an exec-form `CMD`, so SIGTERM reaches the process rather than a shell;
`__main__` relies on uvicorn's own handler, which stops accepting and drains in-flight requests; and
telemetry is flushed from `atexit` *after* those handlers have drained, which is documented in place
because a flush that ran earlier would lose exactly the spans a shutdown is worth tracing for. The
mock API has an explicit `shutdown('SIGTERM')`, and the agents and worker images take the same path.

What is unverified is the interaction: `uvicorn.run` is called **without
`timeout_graceful_shutdown`**, so uvicorn waits for open connections indefinitely, and Cloud Run sends
`SIGKILL` when its grace period expires (10 s by default, configurable up to 3600 s). A stream holds
for `GATEWAY_STREAM_HOLD_MS` — 25 s by default — so a revision replaced while a stream is open is
killed rather than drained, and the connection ends without the close it was designed to give. The
fix is one of two numbers rather than a feature: set `timeout_graceful_shutdown` below Cloud Run's
deadline, or set the hold below both. Which of the two is right depends on how long a patient may
reasonably wait for the next frame, which is a product decision this file does not make.

**2. The kill switch is process-local.** ADR-0039's sibling, `killswitch.py`, says so in its own
docstring: one replica honours its own switch. On a laptop that is one container and it works. With
Cloud Run's default multi-instance behaviour, an operator engaging the switch stops bookings on the
instance they happened to reach and leaves the others writing. This is the failure mode a kill switch
exists to prevent, and it is not a subtle one — it is the whole point of the control. A deployment
needs the switch in the shared store before it needs autoscaling.

**3. §9's session identity is not implemented for this profile.** The gateway's two session routes are
demo fixtures that validate no credential, and they are registered only when `settings.is_demo` — so a
Cloud Run deployment gets *no* way to obtain a session rather than an insecure one, which is the right
default and not a working profile. §9's replacement is an audience-checked service identity, and the
seam is `auth.build_verifiers`. Until it exists, a deployed gateway can be reached and cannot be
used.

**4. Concurrency and instance bounds are undesigned.** §22 asks for
`concurrency/min/max instance tasarımı` and this file does not supply numbers, because a number here
would be a guess: the correct concurrency for the gateway depends on how many SSE connections one
instance holds at 25 s each, and the correct minimum depends on whether a cold start's ~1 s is
acceptable for the patient surface. Both are measurable *after* a deployment and not before. What can
be said now: the request path is CPU-light and I/O-bound on Postgres and OpenSearch, and the SSE
routes pin a connection for as long as the hold — so concurrency should be set well below the
default 80, and the first thing to measure is how the hold interacts with the instance's request cap.

## Consequences

- The three code-shaped items — the stream hold against the grace period, the kill switch's store,
  and §9's identity — are listed in `IMPLEMENTATION_TODO.md` as Cloud Run prerequisites rather than as
  this ADR's work, because two of them are §16's and §9's regardless of where the stack runs. A kill
  switch that only works on one replica is wrong on a laptop with two workers too.
- **The first version of this file claimed no shutdown path existed, and that was wrong.** The
  evidence was a grep for `SIGTERM` in `services/**/*.py`, which misses the Dockerfile's exec-form
  `CMD` and uvicorn's own handler — so the search was for the wrong string in the wrong layer, and the
  conclusion was about the absence of a feature that was there. Corrected above. It is recorded rather
  than quietly fixed because the same mistake is easy to repeat in a design document, where nothing
  fails when a claim is wrong.
- Nothing here changes the demo's profile, and no environment variable is added speculatively. §22's
  numbers are the ones a deployment measures, and inventing them now would be a second set to keep in
  step with the real ones.
- `services/gateway/Dockerfile` already takes a service-shaped image — one process, one port, no
  volume, no local state — so a deployment would push the existing artefact rather than a variant.
