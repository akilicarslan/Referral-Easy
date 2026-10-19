# ADR-0023 — Compose topology, telemetry pipeline and retention

- **Status:** Accepted
- **Date:** 2026-10-14
- **Phase:** 0
- **Supersedes:** —
- **Owns:** `compose.yaml`, `infra/otel/**`, `infra/data-prepper/**`, `infra/opensearch/**`, `infra/compose/**`

## Context

The demo has to come up on a developer laptop with one command per lifecycle step
(`make up` / `make down`), from a clean clone, **with zero credentials and zero
outbound calls to model providers**, while still being an honest representation
of the production shape: a referral platform whose agents must be traceable,
evaluable and auditable.

Constraints that shaped everything below:

1. One OpenSearch node must serve **both** the vector store for directory
   retrieval and the telemetry store. No second datastore, no Prometheus.
2. No message broker is available in the demo: work must be claimed from
   PostgreSQL.
3. `make up` = `docker compose --profile core up -d --wait` and
   `make down` = `… --profile core --profile showcase --profile dashboards down
   --remove-orphans`. The compose file must satisfy those invocations exactly.
4. Telemetry loss must never be silent. A demo that quietly drops spans is worse
   than one that reports it is dropping them.

## Decisions

### D1 — Three profiles; `showcase` and `dashboards` are additive to `core`

`core` is the platform (storage, telemetry pipeline, gateway, mock API, workers,
scheduler, local guardrail). `showcase` adds `web`. `dashboards` adds
`opensearch-dashboards`.

`opensearch` carries **both** `core` and `dashboards`, so `--profile dashboards`
is coherent on its own (a cluster to look at) while `--profile core --profile
dashboards` still starts exactly one cluster — Compose runs a service once even
when several of its profiles are enabled.

Rejected: putting `web` in `core` (then the profile distinction carries no
meaning and the default `make up` drags the UI into every backend-only task);
making `showcase`/`dashboards` complete stacks of their own (duplicated
dependencies, and `make down` would have to tear down two clusters).

### D2 — One OpenSearch node for both jobs, with an enforced index-family separation

The vector store and the telemetry store share a process but **never share an
index**:

| Index family | Data class | Retention | Written by |
| --- | --- | --- | --- |
| `otel-traces-*` | telemetry (spans) | 7 days | Data Prepper trace pipeline |
| `otel-events-*` | telemetry (events/logs) | 7 days | Data Prepper event pipeline |
| `referraleasy-evaluation-*` | evaluation (sampled turns + judge scores) | 30 days | evaluation CLI / scheduler |
| `referraleasy-directory-*` | directory vector (RAG retrieval text) | none (rebuilt from fixtures) | indexing script |

The isolation rule is recorded in the template metadata
(`_meta.referraleasy.never_joined_with` and `isolation_rule`), because the
failure it prevents is not technical but semantic: retrieved directory text is
untrusted content, and if it could be written into, aliased over, or merged with
a structured telemetry or evaluation record, a passage of prose could override a
record of what the system actually did. Anything that later wants "unified
search" must join these families by explicit query, never by alias or index
merge.

`index_type: custom` in both Data Prepper sinks is part of this decision: it
stops Data Prepper from installing its own `otel-v1-apm-span` templates, which
would compete with the templates this repository ships as the single source of
truth. (`otel-v1-apm-span-*` is still listed in the trace template's patterns so
that an OpenSearch Dashboards trace view finds its expected index family.)

### D3 — No message broker: the queue is a PostgreSQL table

`compose.yaml` deliberately has no `kafka`, no `rabbitmq`, no `redis`. The work
queue is rows claimed with `SELECT … FOR UPDATE SKIP LOCKED`, and `worker`
scales by running more consumers against the same table. Adding a broker would
add a second durability domain and a second thing to keep healthy, for a
workload whose contention is measured in dozens of rows.

### D4 — Telemetry path: collector → Data Prepper → OpenSearch, nothing leaves the machine

`otel-collector` (OTLP gRPC 4317 / HTTP 4318) is the only ingress;
`data-prepper` is the only exporter target; `opensearch` is the only store. There
is no exporter to a cloud backend, and the config says so in a comment rather
than leaving it to inference.

Two properties are required of the collector specifically:

- **Bounded, not elastic.** `memory_limiter` runs first with `limit_mib: 320`
  against a 512 MiB container limit, so a telemetry burst cannot OOM the laptop.
  When the bound is reached the collector **refuses** data instead of buffering
  without limit.
- **Loss is a number.** Refusals surface as
  `otelcol_processor_memory_limiter_refused_{spans,metric_points,log_records}`
  plus `otelcol_receiver_refused_*`, and transport failures as
  `otelcol_exporter_send_failed_*`, on the collector's own `:8888/metrics`.
  Neither claim is theoretical:
  - During bring-up the trace pipeline was not yet running, and the span that
    could not be delivered was visible afterwards as
    `otelcol_exporter_send_failed_spans{error_type="Unavailable"} 1`.
  - A deliberate probe with the bound lowered to 4 MiB (spike 1 MiB) answered
    the OTLP export with **HTTP 503** and published
    `otelcol_processor_memory_limiter_refused_spans{processor="memory_limiter"} 1`
    and `otelcol_receiver_refused_spans{receiver="otlp",transport="http"} 1`.
  - Note the exact metric name: the shorter `otelcol_processor_refused_spans`,
    which older documentation suggests, does **not** exist in 0.162.0. The
    counters are created on first refusal, so an idle collector does not
    publish them yet.

### D5 — Retention is configuration a reader can see: 7 days telemetry, 30 days evaluation

Both halves of the distinction live in files, not in code:

- `infra/opensearch/index-templates/*.json` — mappings plus
  `plugins.index_state_management.policy_id`, and a `_meta` block stating the
  retention in days and why.
- `infra/opensearch/ism-policies/*.json` — the ISM policies themselves, each
  carrying an `ism_template` that binds it to the right index patterns.

Why 30 days for evaluation and 7 for telemetry: evaluation records are sampled
at `EVAL_SAMPLE_RATE` (1%), so 30 days of them cost far less than 7 days of raw
traces, and a quality baseline is only meaningful if it spans longer than the
telemetry it was derived from. A reader should be able to see that asymmetry in
the config without asking anyone.

OpenSearch has no init hooks, so this is applied by
`infra/opensearch/apply-index-templates.sh` — idempotent, exits non-zero on
failure, because a decorative step that silently does nothing is how a retention
policy becomes a rumour. The script also pins
`.opendistro-ism-config` to 0 replicas: the ISM plugin creates its own config
index with 1 replica, which on a single node leaves one shard unassigned and the
cluster permanently yellow (measured).

### D6 — Every service has a real health probe

`CMD true` is not used anywhere. Probes are: `pg_isready` (postgres),
`_cluster/health?wait_for_status=yellow` (OpenSearch, a query rather than a TCP
connect so `--wait` cannot succeed before the cluster can answer),
`/metrics/prometheus` (Data Prepper), `/api/status` (Dashboards), `/healthz`
application endpoints, and the collector's `health_check` extension.

The collector needed an image to make that possible: the upstream
`otel/opentelemetry-collector-contrib` image is built `FROM scratch` — measured,
`docker run --entrypoint sh …` fails with *executable file not found* — so no
container-level probe can be expressed against it. `infra/otel/Dockerfile` is a
thin wrapper that copies **the pinned upstream binary unchanged** onto a pinned
`alpine:3.22` base so busybox `wget` can hit `:13133/health/status`. No
collector code is rebuilt.

The collector config is bind-mounted rather than baked into that image, so
editing it needs a container restart, not an image rebuild.

### D7 — Pinned versions, verified rather than assumed; no CANDIDATE PINs remain

Every image is pinned to an exact version and every pin was confirmed against
the registry with `docker manifest inspect` **and** by actually starting it:

| Image | Pin | Verified |
| --- | --- | --- |
| `opensearchproject/opensearch` | `3.9.0` | manifest + running, healthy |
| `opensearchproject/opensearch-dashboards` | `3.9.0` | manifest + started, healthy, `/api/status` 200 |
| `opensearchproject/data-prepper` | `2.16.0` | manifest + running, healthy, wrote documents |
| `otel/opentelemetry-collector-contrib` | `0.162.0` | manifest + running, healthy |
| `postgres` | `18.6-alpine` | manifest + running, healthy |
| `alpine` (collector base) | `3.22` | manifest + built |

One pin deserves a note: the collector's **latest release is 0.163.0**
(published 2026-10-14, the day of writing), but no `0.163.0` image exists on
Docker Hub yet — `docker manifest inspect
otel/opentelemetry-collector-contrib:0.163.0` reports NOT FOUND while `0.162.0`
is present and healthy. Rolling the pin forward is a one-line change once the
image lands.

There are no `# CANDIDATE PIN` markers in `compose.yaml` because nothing in it
is a guess requiring a spike: the versions above were checked, and the
version-interaction questions that *would* have needed a spike (does Data
Prepper 2.16 talk to OpenSearch 3.9? does the OpenSearch 3.x security posture
still allow a credential-free start?) were settled here by running the stack —
see D10.

### D8 — Offline-first and credential-free is a property of the topology, not a promise

`DISABLE_SECURITY_PLUGIN` + `DISABLE_INSTALL_DEMO_CONFIG` on OpenSearch (no
demo certificates, no admin password), no authentication on Data Prepper's
sources or server, no credentials in any health probe, and
`MODEL_BACKEND`/`EMBEDDING_BACKEND`/`RERANK_BACKEND` defaulting to `offline`.
The `postgres` user/password pair is a local fixture that already appears
verbatim in `.env.example`'s `DATABASE_URL`; it is not a secret, and the stack
must not require one to boot.

Anything that would leave this machine — a vendor trace backend, an evaluation
upload, a model provider call — is out of scope for this profile by
construction.

### D9 — Resource limits with bounded heaps, and a measured footprint

Every service declares `deploy.resources.limits`, and the JVM-based services set
their heap to roughly half the container limit so the container limit is the
thing that actually binds: OpenSearch `-Xms1g -Xmx1g` under 2g, Data Prepper
`-Xms512m -Xmx512m` under 1g.

Measured with `docker stats --no-stream` while running (host: 18 GiB):

| Service | Measured | Limit |
| --- | --- | --- |
| `opensearch` | 1.472 GiB | 2g |
| `data-prepper` | 395.8 MiB | 1g |
| `opensearch-dashboards` | 226.2 MiB | 1g |
| `postgres` | 60.06 MiB | 1g |
| `otel-collector` | 38.07 MiB | 512m |
| **total** | **≈2.2 GiB** | — |

### D10 — Measured behaviour that overrode the documentation

These changed the config; each was observed on this stack, not inferred:

1. **`knn.algo_param.ef_search` in `opensearch.yml` prevents boot.** OpenSearch
   3.9.0 exits 70 with `SettingsException[unknown setting
   [knn.algo_param.ef_search] …]`. Search-time tuning moved into the k-NN index
   template (`index.knn.algo_param.ef_search`) where it applies only to the
   index family that holds vectors.
2. **The example pipelines shipped inside the Data Prepper 2.16.0 image are
   stale for their own version.** `record_type: event` is rejected —
   `Unrecognized field "record_type" (class …OTelTraceSourceConfig)` — and the
   pipeline is *skipped while the container still reports healthy*. This is
   exactly the silent gap D4 exists to prevent, and it is why the container
   health probe checks the API rather than the process.
3. **`otel_trace_source` is deprecated in 2.16** (log: *"Consider using the
   updated plugin name 'otlp_traces'"*) and is replaced for the same reason.
4. **The legacy `otel_traces` processor silently produces nothing from the new
   source**: `trace_pipeline_otel_traces_recordsIn_total=1` while
   `recordsOut_total=0`, with no error at any log level. The processor was
   removed; the current OTLP source already emits the documents the sink
   expects.
5. **The ISM `retry` action is rejected** by 3.9's policy validator (`Action
   inside state is null`), so the policies use plain states and transitions.
6. **`.opendistro-ism-config` ships with 1 replica**, which a single node can
   never assign; `apply-index-templates.sh` sets it to 0 so green means green.
7. **Data Prepper's default sink flush left a single span invisible for 10-15
   seconds.** `flush_timeout: 2000` on both sinks brought measured
   export-to-searchable latency down to **4 seconds**, which is the difference
   between a demo that looks alive and one that looks broken.

### D11 — Metrics are not a third store

The collector's OTLP receiver accepts all three signals, but only traces and
logs have pipelines: spans and events are stored in OpenSearch and any metric
points offered to the receiver are **refused**, visibly, as
`otelcol_receiver_refused_metric_points`. Adding a metrics store would mean a
second retention policy and a second thing to keep green, for numbers the
evaluation and alert paths can derive from the same event documents. If a later
phase needs first-class metrics, it should add a pipeline and an OpenSearch
sink for it deliberately — and then D5's retention table grows a row.

## Consequences

**Positive**

- One command brings up a stack that is genuinely offline, credential-free and
  bounded; nothing in it can call out to the internet.
- Telemetry loss, PII leakage and retention are all observable from outside:
  counters for loss, a grep for leakage, config files for retention.
- The PII masking is verified against stored data, not against intent: a span
  carrying `patient.mrn`, `patient.email` and an `Authorization` header is
  stored with those fields `[REDACTED]` and a SHA-256 pseudonym added, and a
  grep for the original values finds nothing.
- The measured footprint (≈1.96 GiB for storage + telemetry) fits the intended
  laptop demo with room for the application services.

**Negative / accepted**

- `apply-index-templates.sh` is a manual step. The alternative — a bootstrap
  container that applies templates and exits — adds a service to a topology
  whose service list is fixed and gives `--wait` something to wait for that is
  not a service.
- Disabling the OpenSearch security plugin is fine for a local demo and would be
  a defect in a deployment; a deployed profile replaces it wholesale with
  certificate-based security and audience-checked service identity.
- OpenSearch and Data Prepper both run JVMs with fixed heaps. That is ~1.9 GiB
  that a smaller telemetry design would not spend, and it is the price of using
  the same technology the production design names.

## Open questions / follow-ups

1. **`traceGroup` and `traceGroupFields` are null** in stored spans, because the
   processor that populated them could not be used (D10 #4). It only affects
   trace grouping in the Dashboards trace-analytics view.
   `data-prepper-otel-trace-group-processor-2.16.0.jar` is present in the image
   and is the likely fix once a compatible source/processor pairing is found.
2. **The application Dockerfiles do not exist yet** (`apps/web`, `services/*`),
   so `web`, `gateway`, `mock-api`, `worker`, `scheduler` and `local-guardrail`
   are declared with an interface but are **UNVERIFIED** — they cannot be built
   or started. `make up` cannot succeed end to end until they land.
3. **Build context convention.** These services assume the repository root as
   build context with a per-service `dockerfile:` path, which suits the
   root-level `uv.lock` / `pnpm-lock.yaml`. If the owning phase prefers a
   service-local context, only the `build:` block in `compose.yaml` changes.
4. **`referraleasy-directory-*` is fixed at 768 dimensions**, matching
   `EMBEDDING_DIMENSIONS=768`. Changing the embedding model without changing the
   template requires a reindex — the template says so in its `_meta`.
5. **`POSTGRES_HOST_PORT`** exists because 5432 was already occupied on the
   development machine used here. The default is unchanged (5432); when it is
   overridden, `DATABASE_URL` must be changed to match.
