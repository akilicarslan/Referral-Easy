# referraleasy-telemetry

OTel spans, structured operation events and the OpenSearch query layer behind §15's analytics. It is
a library: it emits nothing until a service calls `configure_telemetry`, and it reads nothing until
someone constructs a `TelemetryQuery`.

**Adoption is not wired.** No service imports this package yet — `services/gateway`,
`services/mock-api`, `apps/web` and `packages/agent-runtime` are owned by other phases and were not
edited. The seam below is what a service adopts; the pipeline itself is verified end to end against
the running stack (`tests/telemetry/test_pipeline_live.py`).

## The path

    service -> OTLP/HTTP JSON -> OTel Collector -> Data Prepper -> OpenSearch -> TelemetryQuery -> admin app

Two things about it are not the default arrangement, and both were decided by measurement:

* **Events are structured logs, not OTel metrics.** ADR-0008 records the spike: OpenSearch does not
  ingest native OTel metrics. Every §15 measurement is therefore a log record carrying a numeric
  `metric.value` plus its dimensions, so a count, a latency and a cost are all one document shape and
  all aggregatable.
* **The exporter is hand-written.** `referraleasy_telemetry.export` speaks OTLP/HTTP JSON, because
  the vendor exporter package was not in the workspace virtualenv when this phase landed. The
  serialisation is tested against the SDK's own object shapes; ADR-0032 records what it cost.

## Adopting it, in a service

```python
from referraleasy_telemetry import configure_telemetry
from referraleasy_telemetry.identity import CorrelationIds
from referraleasy_telemetry.isolation import read_collector_drops

telemetry = configure_telemetry("gateway", version=os.environ["GIT_SHA"])

correlation = CorrelationIds(
    conversation_id=conversation_id,
    workflow_id=workflow_id,
    booking_request_id=booking_request_id,
)

# One span per unit of work, carrying the correlation ids so they are queryable from the trace
# index (§15: the A2A ids are added to the trace as attributes). The emitter needs a span anyway:
# an emission with no span and no discriminator is refused, because two of them are
# indistinguishable and hashing them together would under-count.
with telemetry.tracer.start_as_current_span(
    "intake.handle", attributes=correlation.attributes()
) as span:
    telemetry.emitter.agent_trigger(
        agent_role="referral_intake",
        synthetic=Synthetic.LIVE_DEMO,
        correlation=correlation,
    )
    telemetry.emitter.state_transition(
        from_state="INTAKE", to_state="COVERAGE_CHECK", correlation=correlation
    )
```

Three things a service must do, and one it must not:

1. **Configure once, at start-up**, before the first request is served. Propagation is installed
   even when export is off, so an inbound `traceparent` is continued either way.
2. **Emit inside the span for the work being measured.** `attempt_span(...)` does this for the
   retry loop; a hand-rolled span works too.
3. **Wrap the emission, not just the logger, on paths that must not fail.** `IsolatedLogger` catches
   what happens *below* it; the emitter's own validation runs above it. §15's promise — a telemetry
   outage does not fail a booking — is kept like this:

   ```python
   from referraleasy_telemetry.isolation import outage_guard

   with outage_guard(telemetry.drops, reason="booking_emit"):
       telemetry.emitter.state_transition(from_state="BOOKING_IN_FLIGHT", to_state="BOOKED")
   ```

   `tests/telemetry/test_isolation.py::TestTheGapAboveTheIsolatedLogger` demonstrates both the gap
   and the guard that closes it.
4. **Do not put a critical audit record here.** The audit stays in PostgreSQL (§15). Telemetry
   indices expire after 7 days; `assert_not_audit_path()` refuses an audit table name if one is ever
   passed in a configuration.

Shut down at exit — `telemetry.shutdown()` — or the batch processors lose their last batch on a
`SIGTERM`.

## Reading it

`TelemetryQuery` is the only reader. It is validated against `ALLOWED_QUERY_PATTERNS`, so it cannot
be pointed at the directory/vector family: retrieved text is untrusted, and a query object that
could join the two would let a *retrieved* document be read as a *recorded* one.

```python
with TelemetryQuery("http://localhost:9200") as query:
    series = query.time_series(
        time_range=TimeRange.day(timezone="America/Chicago"),
        filters=Filters(deployment_annotation=os.environ.get("DEPLOY_TAG"), synthetic=Synthetic.LIVE_DEMO),
    )
```

Four traps, all measured on the running stack, all documented where they bite:

| Trap | What happens | What to do |
| --- | --- | --- |
| A `term` query on an analysed attribute | **zero hits, no error** | use `keyword_field(...)` — every filter already does |
| Reading `_source` by a multi-field name | the field is silently absent | use `source_field(...)`; see `feedback()` |
| `percentiles` for `min`/`max` | the keys do not exist | the query asks for the 0th and 100th percentile |
| An unscoped `outcome_breakdown` | one attempt counts as three | filter by `event_names` — see `test_query_live.py` |

Every aggregate carries a sample count, as §12 requires: `Sample.count` is the deduplicated event
count and `Sample.documents` the raw one, so a duplicated delivery is visible rather than averaged
away.

## Layout

| Module | What it owns |
| --- | --- |
| `conventions.py` | the vocabularies — 16 workflow states, 7 roles, 6 outcome classes, metric and attribute names |
| `identity.py` | the seven ids, W3C propagation, the A2A carrier, span links after a wait |
| `events.py` | `TelemetryEmitter` — one method per §15 measurement, and `event.id` derivation |
| `masking.py` | what may leave the process, applied before export |
| `export.py` | OTLP/HTTP JSON for spans and logs |
| `setup.py` | `configure_telemetry`, from env or from an explicit config |
| `isolation.py` | drop counters, the outage guard, collector drop metrics |
| `feedback.py` | the three feedback numbers and the last-vote collapse |
| `query.py` | `TelemetryQuery` — the §12 filter set and §15's measurements |

## Verifying it

```bash
uv run --no-sync ruff check packages/telemetry tests/telemetry
uv run --no-sync mypy packages/telemetry/src
uv run --no-sync pytest tests/telemetry -q
```

All three, and the order matters less than running all three: the type check is not a formality here.
Its first run found the log exporter returning the deprecated result enum and a dead `or None` in
`extract_span_context` that no test had ever been able to fail — recorded in ADR-0032 under "what the
type checker cost".

The suite has two halves and it is worth knowing which one you are reading. Most tests need nothing
but Python. The ones marked `integration` need the compose stack, emit through the **real**
collector and read the document back out of OpenSearch; they skip loudly when the stack is down,
because a run where they skipped verified nothing about the pipeline.
