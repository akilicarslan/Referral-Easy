# ADR-0032 — Telemetry: numeric log events to OpenSearch, and what the live pipeline cost

- **Status:** Accepted
- **Date:** 2026-10-15
- **Phase:** §15 analytics — `packages/telemetry`, the collector and Data Prepper pipeline, the
  index templates, and the admin app's query layer
- **Depends on:** §15 (measurements, the seven ids, retention and isolation), ADR-0008 (the metrics
  spike: OpenSearch does not ingest native OTel metrics), §12 (the filter set and the sample count),
  §14 (synthetic traffic is labelled), `ReferralEasy_Architecture_v3.md` §15 is the source of truth
  for every vocabulary used here

## Context

§15 asks for one `trace_id` end to end, an event stream from which the admin app can draw every
measurement it lists, feedback collapsed to the last vote per response, PII masked before export,
7-day telemetry retention beside a 30-day evaluation window, and an outage that cannot fail a
booking. The stack was already running: OTel Collector, Data Prepper, OpenSearch, and two index
templates.

Three facts about the environment decided most of what follows.

1. **OpenSearch does not ingest native OTel metrics.** ADR-0008 records the spike. The whole §15
   measurement set therefore has to travel as log records — the fallback is not a preference, it is
   the only shape that arrives.
2. **The vendor OTLP exporter was not in the shared virtualenv**, and this phase may not run
   `uv sync`: several phases share one venv and one `node_modules`, and re-syncing would rewrite the
   resolution under work in progress elsewhere.
3. **A mocked exporter proves the serialisation and not the pipeline.** The task said so, and the
   measurement bore it out: the four defects that mattered were all invisible to the mocked tests and
   were found the first time a real query ran against the real cluster.

## Decision

`packages/telemetry` (`referraleasy-telemetry`) holds every convention, every emission and the only
reader. The collector, Data Prepper and the index templates stay in `infra/`, versioned beside the
code that depends on their exact behaviour.

### Measurements are numeric structured log events

Every §15 measurement is one log record carrying `metric.name`, `metric.value` and `metric.unit`,
plus the dimensions the chart needs (`agent.role`, `workflow.state`, `outcome.class`, …). Counts,
latencies and costs are then the same document shape and aggregate the same way, and a new
measurement needs no template change.

`metric.value` is stored as a number, so it is mapped without a `.keyword` sub-field, while a string
dimension is dynamically mapped as `text` **with** one. Every filter therefore goes through
`keyword_field()` and every numeric comparison through `numeric_field()`. Getting that backwards is
silent in both directions — measured, both ways, in `test_query_live.py`.

### The user-visible outcome classes are fixed at six

`success_full`, `success_empty`, `success_partial`, `error`, `blocked`, `fallback`. `success_empty`
is a **success**: §15 says an empty result is not an error, and the failure mode is not in the index
but in the reading, where empty answers get counted as failures and the failure rate then disagrees
with the number of things that actually failed. `outcome_breakdown()` returns all six keys always —
a missing key renders as a missing bar and reads as "this never happens".

An empty result is stored at INFO severity and is not reachable by an `error` term query. Asserted on
both readings, against the live index.

### The exporter is written here, and it is the cost of the shared venv

`referraleasy_telemetry.export` speaks OTLP/HTTP JSON directly: protobuf-JSON conventions (int64 as
strings, ids as 32/16 hex, enums as integers, lowerCamelCase). It is the only part of this phase that
exists because of the environment rather than the design, and it is the part most worth replacing.
`setup.configure_telemetry` is the single seam: swapping in `opentelemetry-exporter-otlp-proto-http` is
one line plus a sync. Until then, `pyproject.toml` lists the API and the SDK and deliberately does
not list an exporter package nothing imports.

The serialisation is verified against the SDK's real object shapes (`span_factory`,
`log_record_factory` in `tests/telemetry/conftest.py` build real `ReadableSpan` and
`ReadableLogRecord` values), which is what caught the three encoding bugs below.

### Identity is one owner, and the ids are never equalised

`identity.py` owns the seven ids. `conversation_id`, `workflow_id`, `command_id` and
`booking_request_id` are distinct; so are the A2A trio `task_id`, `context_id` and `message_id`,
which are namespaced `a2a.*` on the wire. `CorrelationIds` refuses two ids sharing a value — §15 says
they are not equalised, and a copy-paste that makes two of them one number is exactly the failure
that looks like a working pipeline.

`event.id` is **derived**, not generated, from the trace, the span, the event and the metric names,
and a discriminator (an attempt number, a state transition). Deterministic rather than random is the
whole dedup mechanism: counting distinct ids counts the event once however many copies arrived. An
emission with no span and no discriminator is refused rather than hashed, because two such emissions
are genuinely indistinguishable and counting them as one would under-count. OpenSearch is not asked
to enforce uniqueness — duplicates are stored, and the query layer reports `count` (deduplicated) and
`documents` (raw) side by side so the duplication is visible instead of averaged away.

### Propagation: W3C over HTTP, and the same context inside the A2A `Struct`

Plain HTTP uses `traceparent`. A2A carries the context in `Message.metadata`, which is a
`google.protobuf.Struct`, and only `A2A_METADATA_KEYS` are read from it. A resumed worker starts a
**new** trace with a span **link** to the parked one (§15's rule): the daytime wait then does not sit
inside the resumed span's duration, and the two are still connected. Propagation is proven over a
real socket and over real protobuf bytes, and both halves are then read back out of OpenSearch.

### Masking happens before the payload leaves the process

`masking.py` runs in the exporter path, so a service that configured telemetry is covered, and the
collector's own `transform/mask_pii` and `redaction/scrub` cover everything that arrives regardless.
The test plants a value, emits it through the real collector, and asserts the value is **not** in the
stored document's JSON while `[REDACTED]` **is** and the document itself is confirmed present — an
assertion that a masked payload is absent is worthless if the payload is absent too.

### Isolation is a counter, not an absence of exceptions

`IsolatedLogger` wraps the SDK logger and swallows ordinary failures into `DropCounter`, keyed by
reason (an encode failure is this package's bug, a validation failure is the caller's, an export
failure is the collector being unavailable). `KeyboardInterrupt` and `SystemExit` are not swallowed —
a logger that catches them makes the process un-interruptible. The collector's own drop counters are
read from its metrics endpoint, and an unreachable collector reports `available: False` rather than a
healthy-looking zero.

**The boundary, stated because it is real:** the wrapper sits *below* the emitter's validation, so an
emission outside any span still raises into the caller. On a path that must not fail, the emission is
wrapped, not just the logger — `outage_guard(drops, reason="booking_emit")`. Both the gap and the
guard are demonstrated in `test_isolation.py`.

The critical audit record stays in PostgreSQL; `assert_not_audit_path()` refuses an audit table name
if one is ever passed through this package, and the telemetry indices are a separate family that the
query object can reach and the directory indices cannot.

## What the live pipeline cost — defects found by running it

Each of these was invisible to the mocked tests and appeared as a wrong number, a silent empty
result, or a `400`. They are recorded because the *pattern* repeated: in four of them the stub
reproduced the same mistake as the code, so the test could not have failed.

| # | Defect | Symptom | How it was found |
| --- | --- | --- | --- |
| 1 | `int(SeverityNumber.MEMBER)` — it is a plain `Enum`, not an `IntEnum`, so this raises; a guard caught it and returned 0 | **every** document in the index carried `severityNumber: 0` and an empty `severityText`; an error-severity filter returned nothing, with no error anywhere | reading a live document back |
| 2 | `SpanKind` shipped as the **API** enum, which is offset by one from the OTLP wire enum | spans stored as `SPAN_KIND_UNSPECIFIED` | live document |
| 3 | A batch with one span missing `timeUnixNano` was rejected as a whole | the *other* spans in the same batch vanished too | a span that never arrived |
| 4 | `ReadableSpan.attributes` is a `MappingProxyType`; the masking wrote to it and the write was a no-op | masking silently did nothing for spans | live document still holding the planted value |
| 5 | Pseudonymisation was applied twice to the same value | the hash of a hash, so an identity could not be joined across records | live document |
| 6 | The evaluation judge's name collided with a dimension the emitter validates | an event that could not be emitted | first live emission |
| 7 | `feedback()` read `_source` by a **multi-field** name (`...value.keyword`). Source filtering does not follow multi-fields; OpenSearch answers `200` and omits the path | a populated index returned `raw_count: 0` — a plausible answer, wrong | live feedback query |
| 8 | `latency()` read `values["min"]` / `values["max"]` from a `percentiles` aggregation, which returns only the percents requested | `min`/`max` were `None` on every query, beside a valid p95 | live latency query |
| 9 | `_sample_agg()` returned a **named** aggregation where callers that wrote `{"sample": _sample_agg()}` needed a body | every query carrying a sample count was rejected: `400 Unknown aggregation type [distinct_events]`, before reaching a shard | first live query run |
| 10 | …and the two callers that wrote `sub=_sample_agg()` needed the **named** form, so one shape could never serve both | the outcome and API-failure breakdowns failed differently: `400 Expected [START_OBJECT] under [field]` | same run, second fix |
| 11 | `Struct` is *virtually* registered as `collections.abc.Mapping`, so `isinstance` passes, but the mixin methods are not provided — `metadata.get(key)` raises `AttributeError` | the A2A receiver path crashed on a real `Message.metadata`; every test passed a plain `dict` | probing the installed `a2a` package |

Defects 9 and 10 are the clearest case: `test_query.py` had 101 passing tests, and its fabricated
responses reproduced the same nesting the builder produced. A stub that agrees with the bug cannot
fail on the bug. The response stubs have been corrected, and `test_query.py` now asserts the request
*paths* as well as the numbers.

### What the type checker cost — four defects the live pipeline could not see

`mypy` runs on this repository only now (`strict = true`, `warn_unreachable = true`), and its first run
reported ten errors in this package. Five were typing-level rather than behavioural: an unused
`# type: ignore`, a `MutableMapping` returned as `dict`, `contextmanager` used as a *type* in
`resume_after_wait`'s return annotation, `MetricName` imported from a module that imports it without
re-exporting it, and `response.json()` (typed `Any`) returned from a typed method. The other five lines
described three bugs, and fixing them exposed a fourth:

1. **The exporters were not implementations of the SDK's base classes.** Both
   `BatchSpanProcessor(exporter)` calls were `arg-type` errors, and the reason is specific rather than
   a checker limitation: measured on the installed SDK, `SpanExporter` is a *plain class* — not a
   `typing.Protocol`, not an ABC — so nominal inheritance is the only conformance mypy can see; and
   `LogRecordExporter` really *is* an `abc.ABC`, with **two** abstract methods (`export` and
   `shutdown`, and no `force_flush` at all) that a duck-typed class satisfies without being registered
   as an implementation. Both exporters now subclass. The ABC link on the log side converts a future
   SDK release that grows that interface into a startup `TypeError` instead of a runtime one.
2. **…and hiding behind that: the log exporter returned the deprecated result enum.**
   `LogRecordExporter.export` is declared `-> LogRecordExportResult`, and the exporter returned
   `LogExportResult`, which in this SDK version is not an alias but a *second* `enum.Enum` class with
   the same members: `LogExportResult.SUCCESS is not LogRecordExportResult.SUCCESS`. Nothing in the
   SDK compares the value it gets back, so the whole suite — 36 live tests included — passed while the
   exporter answered the interface with the wrong type. This one needed *both* halves of the fix to
   become visible — a class that does not inherit the ABC is never checked against it, and its `export`
   was annotated `-> Any`, so even with the inheritance there was nothing to compare. Measured on a
   scratch copy before deleting it: the same return value is silent under `-> Any` and
   `Incompatible return value type (got "LogExportResult", expected "LogRecordExportResult")` under the
   declared annotation.
3. **`extract_span_context`'s `or None` was dead code.** `get_span_context()` never returns `None`, and
   the value it returns for a carrier with no context — a non-recording span's all-zero `SpanContext` —
   is not falsy either, so the fallback could not fire. The docstring promised `None` "if there is no
   valid one", and no input produced it: a caller writing `if ctx is None` would have taken the other
   branch and linked to span `00000000000000000000000000000000`. It is gated on `is_valid` now.
4. **The health check would have raised for an exporter without `.stats`.** `Telemetry.export_stats`
   guarded `self.span_exporter is not None` and then reached for `.stats.as_dict()` unconditionally, so
   the vendor-exporter swap this ADR recommends would have crashed the endpoint instead of reporting
   `null`. Latent, not cosmetic: no test calls `export_stats`, which is the point of the two
   `getattr` calls that replaced it.

The mechanism of the blindness is worth recording, because it was in this package's own docstring: the
exporters "satisf[ied] `SpanExporter` structurally, without subclassing it". A duck-typed exporter has
no declared contract, so a checker that could not see the contract could not see the wrong enum either
— and neither could the mocked tests (nothing there compares a result enum) or the live ones (they
*did* pass with the wrong enum). The inheritance is pinned now, with the abstract-method completeness
and the enum identity, by `TestTheExportersAreSDKExporters`.

Two further measurements are recorded in the code rather than fixed, because fixing them would change
the shape of indices that already exist:

* **`time` has millisecond resolution** (`strict_date_optional_time||epoch_millis`), so two records
  written within one millisecond tie on a sort by time. The feedback collapse adds `_doc` as a
  tiebreaker, which resolves a flip-flop written by one batch to the later write — measured, with a
  pair 43 µs apart. A person cannot thumb up and down inside a millisecond; a batch can.
* **`window_days` is calendar days, not elapsed ones.** Python subtracts aware datetimes sharing a
  `tzinfo` by wall-clock fields, so a DST day reports `1.0` while the window spans 25 hours. That is
  the right unit for choosing a histogram bucket and the wrong unit for anything asking how much time
  passed, which its docstring now says.

## Consequences

* **Adoption is not wired.** `services/gateway`, `services/mock-api`, `apps/web` and
  `packages/agent-runtime` are owned by other phases and were not edited. The seam is
  `configure_telemetry(...)` plus `outage_guard` on paths that must not fail, and the correlation ids
  are passed by the service that starts the span —
  `start_as_current_span(name, attributes=correlation.attributes())`, which is what puts the A2A ids
  on the trace rather than only on its events. `packages/telemetry/README.md` documents it;
  `test_pipeline_live.py` verifies the pattern end to end.
* **The event indices hold documents written before defects 1, 2 and 7 were fixed.** They stay:
  deleting indices that other phases may be reading is not this phase's call, and the old and new
  documents are distinguishable by their severity. A reader of the index should know that pre-fix
  rows exist.
* **The top-level `synthetic` field in the events template is unpopulated.** The label travels as
  `log.attributes.synthetic` and the filters that exist all use that path. The template's extra field
  is redundant, and leaving it unpopulated is a smaller lie than writing it in two shapes.
* **`percentiles` costs two extra keys per response** (the 0th and 100th) — the price of having bounds
  at all, measured as two numbers in a response already being sent.
* **Retention is per index**: 7 days for telemetry, 30 for evaluation, via the index lifecycle on the
  daily indices. A document in an index nobody rotates is a document nobody expires, which is why the
  live tests assert the daily index name.
* **No chain-of-thought is captured.** §15: "gizli düşünce zinciri toplamak hedef değildir". The
  package has no API for it — there is no field for a prompt, an output or a deliberation, only
  typed measurements and dimensions — and `model.usage` records tokens and cost, which is what a cost
  chart needs. It is a non-goal rather than a filter: `extra_fields` accepts arbitrary attributes, so
  a service that wanted to ship a prompt through it could. The masking policy handles PII, not
  deliberation, and adoption (which is not wired) is where that would have to be refused.

## Alternatives considered

* **Native OTel metrics.** Rejected by measurement, not by preference — see ADR-0008. It would have
  been the smaller implementation and would have produced empty dashboards.
* **Writing documents straight to OpenSearch from the services.** Rejected: it makes every service
  depend on the search cluster's availability inside the request path, which is the shape §15 forbids
  ("telemetry kesintisi booking'i düşürmez"). The collector's bounded buffer and the SDK's batch
  processor keep the outage on the telemetry side of the line.
* **Collapsing feedback to the last vote at emission time.** Rejected: the emitter is stateless and in
  a process that may not have seen the earlier vote. "Last active" is a query property, and
  `top_hits` computes it in the search rather than by moving every document to the client.
* **Storing the outcome roll-up as its own document.** Rejected: it would be a second source of truth
  that can disagree with the records it summarises, and the breakdown is one aggregation away.
* **Joining telemetry and directory indices.** Refused by construction: retrieved directory text is
  untrusted content, and a query layer that could join them would let a *retrieved* document be read
  as a *recorded* one.
