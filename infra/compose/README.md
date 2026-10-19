# Running the stack

Everything here was measured on this repository's own machine, not estimated.
Where a number appears, the exact command that produced it appears next to it.

- Topology decisions and alternatives: `docs/adr/ADR-0023-compose-topology.md`
- The compose file itself: `compose.yaml` (repository root — that is the file
  the root `Makefile` drives, not a copy in this directory).

---

## 1. Profiles

`compose.yaml` defines exactly three profiles. `showcase` and `dashboards` are
**additive to `core`**, they do not replace it.

| Profile | Services | What it is for |
| --- | --- | --- |
| `core` | `postgres`, `opensearch`, `otel-collector`, `data-prepper`, `mock-api`, `gateway`, `worker`, `scheduler`, `local-guardrail` | The platform: storage, telemetry pipeline, and the services that do the work. |
| `showcase` | `web` | The demo surface. Needs `core` running underneath it. |
| `dashboards` | `opensearch-dashboards` (plus `opensearch`, which carries both the `core` and `dashboards` profile so this profile is coherent on its own) | Look at indices, spans and events in a UI. |

```bash
# the platform only (this is what `make up` does with the default PROFILE)
docker compose --profile core up -d --wait

# the full demo: platform plus UI
docker compose --profile core --profile showcase up -d --wait
make up PROFILE="core --profile showcase"          # same thing via the Makefile

# just the ops view (brings its own single-node OpenSearch)
docker compose --profile dashboards up -d --wait

# stop everything, including orphans from removed services
make down
docker compose --profile core --profile showcase --profile dashboards down --remove-orphans
```

`docker compose --profile showcase up` on its own starts **only** `web` — that
is what a profile means in Compose. Always pair the additive profiles with
`core`.

---

## 2. What each service is for

| Service | Image (pinned) | Purpose |
| --- | --- | --- |
| `postgres` | `postgres:18.6-alpine` | Relational store **and the work queue**: jobs are rows claimed with `SELECT … FOR UPDATE SKIP LOCKED`. There is no message broker anywhere in this stack. |
| `opensearch` | `opensearchproject/opensearch:3.9.0` | One single-node cluster doing two jobs: the **vector store** for directory RAG (`referraleasy-directory-*`) and the **telemetry store** (`otel-traces-*`, `otel-events-*`, `referraleasy-evaluation-*`). No separate vector database, no Prometheus. |
| `otel-collector` | `referraleasy/otel-collector:dev`, built from `infra/otel/Dockerfile` on `otel/opentelemetry-collector-contrib:0.163.1` | OTLP intake (gRPC 4317, HTTP 4318), bounded memory, PII/secret masking, and the only exporter — to Data Prepper, on this network. No exporter leaves the machine. |
| `data-prepper` | `opensearchproject/data-prepper:2.16.0` | Two pipelines: OTLP traces → `otel-traces-<date>`, OTLP logs/events → `otel-events-<date>`. Directory/vector indices are deliberately not written here. |
| `opensearch-dashboards` | `opensearchproject/opensearch-dashboards:3.9.0` | Ops UI on <http://localhost:5601>. |
| `mock-api` | built from `services/mock-api/Dockerfile` | Deterministic mock hospital API, port 4010, fixed `MOCK_FAULT_SEED`. Dockerfile exists; the local container was observed healthy during the 2026-10-18 review. |
| `gateway` | built from `services/gateway/Dockerfile` | Public API for the UI, port 8080. The review also ran the current checkout separately on port 18080. |
| `worker` | built from `services/workers/Dockerfile` | Queue consumer. The local worker container was observed healthy; PostgreSQL worker integration tests ran separately. |
| `scheduler` | built from `services/workers/Dockerfile` | Scheduled driver. Its full evaluation/alert loop remains incomplete; see the architecture review. |
| `local-guardrail` | built from `services/agents/Dockerfile` | Legacy standalone classifier description. The gateway currently builds the local classifier in process. |
| `web` | built from `apps/web/Dockerfile` | Patient/admin UI, port 3000. The review ran the current checkout on Vite port 3001. |

### Ports

All published ports bind to `127.0.0.1`. This stack provides local fixture sessions
without login and runs OpenSearch without authentication. Keep it on the local
machine. See [security notes](../../SECURITY.md) before any public deployment.

| Host port | Service |
| --- | --- |
| 3000 | `web` |
| 4010 | `mock-api` (`MOCK_API_URL` in `.env.example`) |
| 4317 / 4318 | `otel-collector` OTLP gRPC / HTTP |
| 5601 | `opensearch-dashboards` |
| 5432 | `postgres` (`DATABASE_URL` in `.env.example`) |
| 8080 | `gateway` |
| 8090 | `local-guardrail` |
| 8888 | `otel-collector` own metrics (the drop metric lives here) |
| 9200 | `opensearch` |
| 13133 | `otel-collector` health endpoint |
| 21890 / 21892 / 4900 | `data-prepper` trace source / log source / API |

`POSTGRES_HOST_PORT` (default `5432`) is the one host port that is
parameterised, because 5432 is the most contested port on a developer machine.
If something else already listens there, set it in `.env` **and** change the
port in `DATABASE_URL` to match:

```dotenv
POSTGRES_HOST_PORT=55433
DATABASE_URL=postgresql://referraleasy:referraleasy@localhost:55433/referraleasy
```

---

## 3. Measured resources while running

`docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}'` with the whole
`core` profile plus the `dashboards` profile up, after telemetry had flowed
through every stage (host: 18 GiB):

| Service | Memory used | Limit set in compose | CPU |
| --- | --- | --- | --- |
| `opensearch` | **1.472 GiB** | 2g (JVM heap `-Xms1g -Xmx1g`) | 5.2% |
| `data-prepper` | **395.8 MiB** | 1g (JVM heap `-Xms512m -Xmx512m`) | 2.2% |
| `opensearch-dashboards` | **226.2 MiB** | 1g | — |
| `postgres` | **60.06 MiB** | 1g | 0.0% |
| `otel-collector` | **38.07 MiB** | 512m (config limit 320 MiB) | 0.1% |
| **total, storage + telemetry tier** | **≈2.2 GiB** | — | — |

The application services are not included: they cannot be built yet (see §6).
Budget accordingly — the limits add up to more than the measured usage on
purpose, because limits are ceilings for a runaway process, not reservations.

Re-measure with:

```bash
docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}'
```

---

## 4. Index templates and retention (a manual step, by design)

OpenSearch has no init hook, so the templates and ISM policies are applied by an
explicit, idempotent script rather than at container boot:

```bash
OPENSEARCH_URL=http://localhost:9200 infra/opensearch/apply-index-templates.sh
```

It applies, in order:

| File | Effect |
| --- | --- |
| `infra/opensearch/ism-policies/referraleasy-telemetry-7d.json` | **7 day** retention for `otel-traces-*`, `otel-events-*`, `otel-v1-apm-span-*` |
| `infra/opensearch/ism-policies/referraleasy-evaluation-30d.json` | **30 day** retention for `referraleasy-evaluation-*`, with a `read_only` warm state from day 7 |
| `infra/opensearch/index-templates/otel-telemetry-traces.json` | 1 shard / 0 replicas, attaches the 7 day policy |
| `infra/opensearch/index-templates/otel-telemetry-events.json` | 1 shard / 0 replicas, attaches the 7 day policy |
| `infra/opensearch/index-templates/referraleasy-evaluation.json` | 1 shard / 0 replicas, attaches the 30 day policy |
| `infra/opensearch/index-templates/referraleasy-directory-vector.json` | k-NN (`knn_vector`, 768 dims, Lucene engine, cosine), separate index family, no age-based deletion |

Run it **before** the first telemetry arrives (or delete the empty index it
left behind): an index created before its template exists keeps the default
1 replica and shows up as an unassigned shard on a single node.

`make demo` calls this script as step 3 of 8, right after the cluster is up and
before anything creates an index; it exits non-zero if any template or policy
fails, so it works as a real step rather than decoration. `make index` does not
call it — indexing creates an index, so the ordering that matters is the one
`make demo` enforces.

---

## 5. Verifying a running stack

```bash
# cluster up and green, k-NN plugin present
curl -s 'localhost:9200/_cluster/health?pretty'
curl -s 'localhost:9200/_cat/plugins?v' | grep knn

# telemetry path: send one span and one event through the collector
curl -s -X POST http://localhost:4318/v1/traces -H 'Content-Type: application/json' \
  -d '{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"smoke"}}]},
       "scopeSpans":[{"spans":[{"traceId":"5b8efff798038103d269b633813fc60c","spanId":"eee19b7ec3c1b174",
       "name":"smoke","kind":2,"startTimeUnixNano":"'$(date +%s)'000000000",
       "endTimeUnixNano":"'$(date +%s)'000000000"}]}]}]}'
curl -s 'localhost:9200/otel-traces-*/_count'

# telemetry loss is visible, not silent (refusals from the memory limiter and
# failed exports are counters on the collector's own metrics endpoint)
curl -s localhost:8888/metrics | grep -E 'refused_spans|otelcol_exporter_send_failed'
```

Measured end-to-end latency with `flush_timeout: 2000`: **4 seconds** from an
HTTP OTLP export to the document being searchable in `otel-traces-*`. With
Data Prepper's default flush timeout the same span took 10-15 seconds, which is
why the pipelines set it explicitly.

### What you should see for PII

A span carrying `patient.mrn=MRN-12345`, `patient.email=jane.doe@example.org`
and an `authorization: Bearer …` header is stored as:

```
span.attributes.patient@mrn                     = "[REDACTED]"
span.attributes.patient@email                   = "[REDACTED]"
span.attributes.patient@identifier_hash         = "b283ab42833b66c08ad9…"   (SHA-256 pseudonym)
span.attributes.http@request@header@authorization = "[REDACTED]"
```

A grep of the stored documents for the original values returns zero hits.
Masking happens in the collector, before export, so an unmasked value never
reaches OpenSearch.

---

## 6. Current verification scope

All application Dockerfiles now exist. The 2026-10-18 review observed healthy local containers for
PostgreSQL, OpenSearch, Data Prepper, the collector, the mock API, gateway, web, worker and all seven
agent roles. This does not establish that every planned feature is wired into a complete journey.

The current checkout was run separately for browser verification: Vite on 3001, gateway on 18080,
an isolated mock API on 4011 and separate PostgreSQL review databases. The existing Compose demo
state was preserved. A successful container healthcheck is distinct from these behavior checks.

The detailed results, repaired connections and remaining orchestration/evaluation/alerting work are
in [the architecture review](../../docs/architecture/review-2026-10-18.md). The earlier resource
measurements in this document describe the storage/telemetry tier when measured, not the complete
current application footprint.

---

## 7. Environment

`compose.yaml` reads `.env` when it exists (`make env` creates it from
`.env.example`) and overrides the values that must differ **inside** the
network, because `.env.example` is written for host-run processes:

| Variable | `.env.example` (host) | Inside compose |
| --- | --- | --- |
| `DATABASE_URL` | `…@localhost:5432/…` | `…@postgres:5432/…` |
| `MOCK_API_URL` | `http://localhost:4010` | `http://mock-api:4010` |
| `OPENSEARCH_URL` | `http://opensearch:9200` | unchanged |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4318` | unchanged |

No credentials are required anywhere: the OpenSearch security plugin is
disabled (`DISABLE_SECURITY_PLUGIN`), Data Prepper runs without TLS or
authentication, and `MODEL_BACKEND`/`EMBEDDING_BACKEND`/`RERANK_BACKEND`
default to `offline`.

Optional line worth adding to `.env.example` (only needed if 5432 is occupied):

```dotenv
# Host port published by the `postgres` service. Must match the port inside
# DATABASE_URL above. Defaults to 5432.
POSTGRES_HOST_PORT=5432
```
