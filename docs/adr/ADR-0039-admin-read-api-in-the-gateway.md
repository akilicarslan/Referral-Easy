# ADR-0039 — The admin read API is the gateway's, over an index-restricted telemetry query

- **Status:** Accepted
- **Date:** 2026-10-15
- **Phase:** Phase 7.2 (§12's nine admin pages, §15's indices and metrics, §16's kill switch)
- **Depends on:** §12 (what the nine pages must show and the rule that no fake chart may appear),
  §15 (the trace/event/evaluation indices and the separation from the directory index), §16 (alerts
  and the kill switch), ADR-0023 (compose topology), ADR-0031 (the gateway's SSE and auth), ADR-0032
  (telemetry into OpenSearch), ADR-0037 (the release registry), ADR-0027 (the patient renderer, whose
  boundary this file follows rather than reuses)

## Question

§12 lists nine admin pages and §15's diagram draws the data path as

```
OS["OpenSearch: trace and event indices"] --> Admin["Admin API / charts / trace waterfall"]
```

— a node labelled **Admin API** that does not exist in this repository. `services/` holds agents,
gateway, mock-api and workers; `compose.yaml` has no admin service. The gateway has two `/v1/admin/*`
routes and nothing that reads OpenSearch, and `GatewayDeps` has no OpenSearch handle at all.

So: what serves the nine pages, and what is allowed to read the telemetry indices?

## Context

**The gateway already owns the admin surface.** `/v1/admin/workflows/{workflow_id}` and
`/v1/admin/metrics` exist, both behind `require_admin` and the shared error envelope, and the admin
issuer/verifier are already constructed in `GatewayDeps`. An admin API elsewhere would need its own
JWT verification, its own envelope, its own compose entry, healthcheck, Dockerfile and resource
limits — five kinds of machinery for no capability the gateway lacks.

**The browser reaches the gateway same-origin, and that is not incidental.** `apps/web` is a Vite
static bundle behind nginx, and `apps/web/nginx.conf` proxies `/v1/*` to the gateway; `compose.yaml`
says so explicitly and explains why the URL is not an environment variable. A second admin service
would need a second proxy location, its own CORS story, and a second base URL in the bundle — for
reads that are already authenticated by the same admin token the page holds.

**§15's actual requirement is about indices, not about processes.** *"Directory and telemetry
indices are separate"* separates the directory from telemetry, and `TelemetryQuery` enforces it at
construction:
an index outside `ALLOWED_QUERY_PATTERNS` raises rather than querying. Retrieved directory text is
untrusted content, and a query that joined the two families would let a document that was *retrieved*
be read as a document that was *recorded*. That property is a constructor argument, so where the
caller lives does not weaken it — but a caller that could build the query with an arbitrary index
would.

**The read side already exists and is complete.** `packages/telemetry/.../query.py` implements
`event_count`, `outcome_breakdown`, `agent_triggers`, `latency`, `api_failures`, `time_series`,
`model_usage`, `hitl`, `guardrail_decisions`, `evaluation_scores`, `feedback` and `index_health`, on
top of `TimeRange` (day / month / custom, `America/Chicago`) and `Filters` over the §15 attributes
(agent role and version, release, model, synthetic vs live-demo, deployment annotation). Every filter
§12 requires is an attribute §15 already reserves. Nothing needs inventing; it needs a route, a
dependency and a sample count.

## Decision

**The nine pages read `/v1/admin/*` on the gateway, which reads the telemetry indices through
`TelemetryQuery` and nothing else.**

1. **A query port, not an OpenSearch client in the routes.** `GatewayDeps` gains an optional
   `telemetry: TelemetryPort | None`, built from a new `OPENSEARCH_URL` setting and constructed
   through `TelemetryQuery`, whose index allow-list is therefore not a route's choice to make.
2. **Optional, because the gateway must boot without it.** A stack without OpenSearch is a stack whose
   admin charts have no data — not one that fails to start. The dependency is `None` when the setting
   is unset, and every admin route that needs it answers with the "no source" shape below.
3. **A page with no source says so; it does not render a zero.** §12's *"fabricated static success
   charts are not used"* and the `[!]` invariant behind it mean an empty chart must be
   distinguishable from a measured zero, and `Sample` already carries `count`, `duplicated` and the
   eligible total for exactly that. A route whose source is missing returns an explicit
   `{"available": false, "reason": …}` rather than an empty series that a chart would draw as flat.
4. **The library's own restriction stays the only one.** No route may pass `index=`; the pattern is
   fixed at construction per query family.

## Alternatives rejected

- **A separate `admin-api` service.** Closest to §15's diagram as a literal reading, and rejected on
  cost against capability: a fifth Python service with its own auth, envelope, image, healthcheck and
  Compose block, a second nginx proxy location, and a second base URL in the bundle — to serve reads
  that the same admin token already authorises. §15's diagram is a statement about *data flow*
  (OpenSearch → the admin-facing API), and the gateway satisfies it. If the gateway ever becomes
  patient-only for a reason this file does not foresee, the port in (1) is the seam the move would
  cut along, and that is a deliberate part of the design.
- **The browser queries OpenSearch directly.** It would put the cluster behind a browser-reachable
  address, hand the client the query vocabulary §15 keeps server-side, and make the directory-family
  exclusion a client convention rather than a constructor guarantee. §15's separation rule exists to
  stop a retrieved document being read as a recorded one; that is not a rule to enforce in the page.
- **Reading the indices through the gateway's existing `psql`/`commands` layer.** Wrong store: the
  spans and events are in OpenSearch by design (ADR-0032), and Postgres holds workflow and approval
  state. The admin pages need both, and the split is by what each store is authoritative for rather
  than by convenience — the Human Queue reads Postgres, the Trace Explorer reads spans.

## Consequences

- `GatewayDeps` grows one optional member, and `app.create_app` builds it from the environment.
  Existing gateway tests construct `GatewayDeps` without it, which is why it is optional and not a
  required field.
- The admin app gains one base path and no new origin, so no CORS and no second proxy.
- Every chart surface must carry its sample count, because a route that cannot say how many events it
  looked at cannot be checked against a claim. This is a requirement on the routes rather than on the
  views: a view cannot invent a sample it was not given.
- The kill switch (§16) and the Alerts page (§16) are **not** covered here. They are Phase 7.3, and
  the kill switch already has a home in `ControlState`; what this ADR settles is only where the reads
  come from.
- Pages whose source does not exist yet — Evaluations has a package but no replay harness, Fault
  Scenarios is Phase 4.1, Alerts is Phase 7.3 — render their "no source" state from this decision
  rather than a placeholder chart. When the source lands, the route starts answering and the page
  draws; no view changes.
