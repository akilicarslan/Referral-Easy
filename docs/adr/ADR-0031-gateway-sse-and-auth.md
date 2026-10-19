# ADR-0031: Gateway SSE transport, replay, and session authentication

**Status:** Accepted — Phase 2.1, demo/offline profile. Deployment-profile items are explicitly
deferred; see Consequences.
**Date:** 2026-10-15
**Supersedes:** nothing. **Related:** ADR-0001 (language and tooling), ADR-0030 (agent runtime).

## Context

§12 requires a live patient stream: the patient sees progress while the coordinator's agent is still
working, and **nothing partial is ever committed as final state**. The stream must be resumable after
a dropped connection, and resumption must be *inert* with respect to the workflow — re-reading a
stream must not create a second booking.

§7 and §9 require separate patient and coordinator sessions, validated for real, with strictly
different write authority.

The constraints the environment actually imposes: the demo profile runs with no external identity
provider and no cloud services, and the browser's built-in stream consumer is `EventSource`, which
**cannot set an `Authorization` header** and reconnects on its own schedule.

## Decision

1. **SSE, not WebSocket.** The stream is one-way; `EventSource` supplies automatic reconnect and
   cursor maintenance for free; and the transport stays plain HTTP through the same ASGI app, so
   there is no second protocol to authenticate.

2. **Event identity and replay.** Every frame carries the contract's `event_id` and a monotonically
   increasing per-workflow `seq`. The client's cursor (`Last-Event-ID` header, or a `last_event_id`
   query parameter) selects a **replay position only** — replay re-emits recorded frames and executes
   no work. An unknown id replays from the beginning rather than erroring, because that is
   recoverable and an error is not.

3. **Two event classes.** `{surface_update, workflow_state, terminal}` are **authoritative**;
   `{agent_progress, notice, partial_text}` are **not**. A client folds only authoritative frames to
   build state, which makes §12's "partial agent output is never committed as final state"
   **structural** rather than a discipline someone must remember: partial text can stream
   indefinitely and the folded state never moves. The terminal event carries no event-level progress
   reading; its payload carries `final_elapsed_ms` and the turn's *first* progress reading.

4. **Auth: two audiences, two secrets, two mint routes.** HS256 verified on stdlib
   `hmac`/`hashlib` — constant-time compare, an `alg` allow-list, and `exp`/`iat`/`aud`/`sub` checks —
   because PyJWT is not a dependency here. Every route verifies for real. The demo profile also mints
   fixture tokens (`/v1/auth/patient-session`, `/v1/auth/admin-session`) so the app is exercisable
   offline without becoming a bypass.

5. **A query-string token is permitted only in the demo profile** (`allow_query_token=settings.is_demo`),
   and only because `EventSource` cannot set headers. The production profile refuses it: tokens in
   URLs end up in access logs, referrers and browser history.

6. **Latency is two measurements, never one (§12).** `progress_elapsed_ms` travels on progress
   events; `final_elapsed_ms` on the terminal's payload; samples keep both; the admin metrics report
   two means over their own populations and state
   `progress_latency_measured_separately: true`. A turn that showed the patient nothing reports **no**
   progress latency rather than a flattering zero. The two are never blended into an average.

7. **The guardrail seam sits at the write.** With any guardrail layer unconfigured, a booking write is
   refused (503) while read-only surfaces — the help card — still serve. §9's rule is that safety
   degrades to "cannot book", never to "books unscreened".

## Consequences

**Positive.** Resumption is inert by construction: a test asserts exactly **one** booking row across
replays, rather than asserting the client's discipline. The demo profile is fully offline. The two
latencies cannot be collapsed by accident, because they are separate fields over separate samples.

**Negative and limits, stated plainly.**

- **A hand-rolled JWT verifier to maintain** until PyJWT or an IdP lands. It is deliberately a
  one-module swap behind an issuer/verifier seam.
- **Query-string tokens exist** in the demo profile, so a demo operator must expect tokens in logs.
- **SSE is one-way.** Any future acknowledgement-bearing push would need a different transport.
- **The PostgreSQL control profile is unimplemented, and the gateway refuses to start under it**
  (`GATEWAY_CONTROL_BACKEND=postgres` → refuse) rather than serving in-memory state under a
  `postgres` label. This is the honest failure, but it is a real gap: every §7 rule the database
  would enforce — the transition table, the optimistic version check, `UNIQUE(approval_id)`, command
  dedup — is currently mirrored in an in-memory profile and asserted by tests. It is a stand-in, not
  a simulation. Implementing it is tracked in `IMPLEMENTATION_TODO.md` under 2.1.
- **The event log is in-memory**, so replay survives a client disconnect but not a gateway restart.
  Nothing tested multi-process replay or the stream's behaviour under load.

## Alternatives considered

- **WebSocket.** Rejected: bidirectional, so it would need its own auth handshake and reconnect
  logic, for a stream that is one-way. `EventSource`'s automatic reconnect and cursor handling is
  exactly the behaviour §12 asks for.
- **Long-polling.** Rejected: it would make "replay creates no work" a property of each poll rather
  than of the transport, and it cannot distinguish progress from final state as cleanly.
- **Persisting the event log in Postgres now.** Deferred rather than done badly — it belongs with the
  control profile above, which is the same piece of work.
