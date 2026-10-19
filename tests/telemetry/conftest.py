"""Shared fixtures for the telemetry tests.

Three groups of test live here, and the split is the point:

* **Unit tests** need nothing but Python. Masking rules, event-id derivation, the feedback
  arithmetic, the query bodies. These are fast and they run anywhere.
* **Integration tests** (marked ``integration``) need the compose stack. They are the ones that
  matter: they emit through the *real* collector, wait for Data Prepper, and read the document back
  out of OpenSearch. A test with a mocked exporter proves the serialisation and not the pipeline,
  and this suite says which is which in every docstring that could be ambiguous.
* **Live-stack helpers** — :func:`wait_for_document` and :data:`live_stack` — that turn "did the
  document arrive?" into one call rather than a sleep.

The stack is expected at the ports ``compose.yaml`` publishes: collector ``4318``, OpenSearch
``9200``, collector metrics ``8888``. Nothing here is destructive: the tests write records with
unique markers and read them back, and never delete an index that other work may be using.
"""

from __future__ import annotations

import os
import socket
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Making the package importable
# ---------------------------------------------------------------------------
# `referraleasy-telemetry` is a workspace member and is declared as a dependency in the root
# `pyproject.toml`, exactly like the five members before it. Declaring it is not enough to make it
# importable *right now*: `uv sync` is what writes
# `_editable_impl_referraleasy_telemetry.pth` into the shared virtualenv, and this repository is
# being edited by several phases at once, so the virtualenv is shared and must not be re-synced
# from here (`uv sync` would also rewrite the resolution for work in progress elsewhere).
#
# So the source root is put on `sys.path` for these tests, and ONLY when the package is not
# already importable. The moment anyone runs `uv sync --all-packages`, the import succeeds and
# this block does nothing at all. It is a bridge for the window before that sync, kept deliberately
# dumb — no import hooks, no magic — so that removing it is a two-line deletion.
_SRC = Path(__file__).resolve().parents[2] / "packages" / "telemetry" / "src"
try:  # pragma: no cover - depends on whether the workspace has been synced
    import referraleasy_telemetry  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(_SRC))


#: Where the stack lives. Overridable so the same tests can be pointed at another demo box.
COLLECTOR_ENDPOINT = os.environ.get("TEST_OTEL_ENDPOINT", "http://localhost:4318")
OPENSEARCH_URL = os.environ.get("TEST_OPENSEARCH_URL", "http://localhost:9200")
COLLECTOR_METRICS_URL = os.environ.get("TEST_OTEL_METRICS_URL", "http://localhost:8888")


def _reachable(url: str, timeout: float = 1.0) -> bool:
    """True when a TCP connection to ``url``'s host:port succeeds."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def live_stack() -> tuple[str, str]:
    """``(collector_endpoint, opensearch_url)``, or a skip that says what is not running.

    The skip is a skip and not a failure, but it is a *loud* one: this suite's whole claim is that
    the pipeline was verified end to end, and a run where these tests skipped verified nothing. The
    final report of the phase has to say which happened, so the reason string names the exact
    service that was missing.
    """
    missing = [
        name
        for name, url in (
            ("otel-collector", COLLECTOR_ENDPOINT),
            ("opensearch", OPENSEARCH_URL),
        )
        if not _reachable(url)
    ]
    if missing:
        pytest.skip(
            f"the compose stack is not reachable: {', '.join(missing)}. "
            f"Start it with `docker compose --profile core up -d --wait`. "
            f"Until then, the live-pipeline tests verify nothing."
        )
    return COLLECTOR_ENDPOINT, OPENSEARCH_URL


def unique_marker(prefix: str) -> str:
    """A marker no other run will produce, so a live assertion cannot match someone else's data.

    Every live test writes with one of these into its ``deployment.annotation`` dimension and
    filters every read by it. The telemetry indices are shared with the running demo and with
    whatever else is emitting, so a test that queried "the events in the last five minutes" would
    be reading other people's traffic and would flake for reasons that have nothing to do with it.
    """
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def wait_for_document(
    *,
    opensearch_url: str,
    index: str,
    query: Mapping[str, Any],
    timeout: float = 20.0,
    poll: float = 0.5,
) -> dict[str, Any] | None:
    """Poll ``index`` until ``query`` matches, and return the first hit's ``_source``.

    Returns ``None`` on timeout rather than raising, so the caller decides what a missing document
    means — and writes a message that names what it was looking for.

    The timeout is generous because of two buffering stages that are both deliberate: the SDK's
    batch processor flushes on a timer, and the Data Prepper sink has ``flush_timeout: 2000``. The
    pipeline's own config records that a single span can take 10-15s to appear with the default
    flush timeout; 20s is the measured worst case plus margin, not a guess.
    """
    import httpx

    body = {"size": 1, "query": query, "sort": [{"_doc": {"order": "asc"}}]}
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=5.0) as client:
        while time.monotonic() < deadline:
            try:
                response = client.post(f"{opensearch_url}/{index}/_search", json=body)
            except Exception:
                time.sleep(poll)
                continue
            if response.status_code < 300:
                hits = response.json().get("hits", {}).get("hits", [])
                if hits:
                    return hits[0].get("_source", {})
            elif response.status_code != 404:
                # A 404 is "the daily index does not exist yet", which is normal before the first
                # flush of the day. Anything else is worth surfacing in the failure message.
                return None
            time.sleep(poll)
    return None


def wait_for_count(
    *,
    opensearch_url: str,
    index: str,
    query: Mapping[str, Any],
    count: int,
    timeout: float = 60.0,
    poll: float = 2.0,
) -> int:
    """Poll ``index`` until at least ``count`` documents match ``query``; return what was found.

    The counterpart of :func:`wait_for_document` for the tests that need several documents before
    they can assert anything — a latency distribution of one reading is not a distribution.

    Returns the count rather than raising, so the caller's failure message can name what it was
    waiting for and how much of it arrived. That is the difference between ``assert 0 >= 3`` and
    "three attempt records were expected and one arrived"; the first is what a bare assertion
    prints, and it is useless at 3am.

    **No refresh here, deliberately.** The events template sets ``refresh_interval: 10s`` and the
    Data Prepper sink flushes on a timer, so the delay being waited through *is* part of what is
    under test. Refreshing would make the test faster and would stop measuring the pipeline's own
    timing. A test that writes with the OpenSearch API directly wants :func:`refresh` instead.
    """
    import httpx

    body = {"size": 0, "query": query, "track_total_hits": True}
    deadline = time.monotonic() + timeout
    found = 0
    with httpx.Client(timeout=10.0) as client:
        while time.monotonic() < deadline:
            try:
                response = client.post(f"{opensearch_url}/{index}/_search", json=body)
            except Exception:
                time.sleep(poll)
                continue
            if response.status_code < 300:
                found = int(response.json().get("hits", {}).get("total", {}).get("value", 0) or 0)
                if found >= count:
                    return found
            elif response.status_code != 404:
                return -1
            time.sleep(poll)
    return found


def refresh(opensearch_url: str, index: str) -> None:
    """Force a refresh so a search after a write sees it.

    Only used by tests that write directly with the OpenSearch API rather than through the
    pipeline; the pipeline path is asserted against its own ``refresh_interval: 10s`` instead,
    because refreshing for it would hide whether the pipeline's timing works.
    """
    import contextlib

    import httpx

    with contextlib.suppress(Exception):
        # An index that does not exist yet needs no refresh, and a stack that just went away is not
        # this helper's problem to report: the assertion that follows will say what it found.
        httpx.post(f"{opensearch_url}/{index}/_refresh", timeout=5.0)


@pytest.fixture(scope="session")
def stack_facts(live_stack: tuple[str, str]) -> Iterator[dict[str, str]]:
    """The endpoint triple, as a mapping, for tests that want it named."""
    collector, opensearch = live_stack
    yield {
        "collector": collector,
        "opensearch": opensearch,
        "collector_metrics": COLLECTOR_METRICS_URL,
    }


class LivePipeline:
    """One test's own slice of the running pipeline: emit through the collector, read it back.

    Two things about it are deliberate.

    **Every test gets its own ``deployment.annotation``.** The telemetry indices are shared with the
    running demo and with whatever else is emitting on this machine, so a test that queried "the
    events from the last five minutes" would be reading other people's traffic and would fail for
    reasons that have nothing to do with it. Every write is stamped and every read is scoped by the
    stamp, which is what makes a zero a finding rather than a race.

    **It uses the real exporter, not a mock.** ``configure_telemetry`` with an endpoint builds the
    package's own OTLP/HTTP JSON exporter against the real collector URL, so what these tests
    measure is the whole path — span to payload to collector to Data Prepper to OpenSearch — and not
    the serialisation. ``set_global=False`` because a test that installed its providers globally
    would leave them behind for every test after it.
    """

    def __init__(self, collector: str, opensearch: str) -> None:
        from referraleasy_telemetry.conventions import (
            EVENT_INDEX_PATTERN,
            TRACE_INDEX_PATTERN,
            Synthetic,
        )
        from referraleasy_telemetry.setup import TelemetryConfig, configure_telemetry

        self.collector = collector
        self.opensearch = opensearch
        self.event_index = EVENT_INDEX_PATTERN
        self.trace_index = TRACE_INDEX_PATTERN
        self.service_name = "referraleasy-telemetry-live-tests"
        self.marker = unique_marker("live")
        self.telemetry = configure_telemetry(
            self.service_name,
            version="0.1.0",
            config=TelemetryConfig(
                service_name=self.service_name,
                service_version="0.1.0",
                endpoint=collector,
                synthetic=Synthetic.SYNTHETIC,
                deployment_annotation=self.marker,
            ),
            set_global=False,
        )
        self.tracer = self.telemetry.tracer
        self.emitter = self.telemetry.emitter

    # -- writing -------------------------------------------------------------

    def flush(self) -> None:
        """Push what the SDK is holding to the collector now, instead of on its batch timer.

        Only the *first* buffer is skipped this way. The exporter's batch processor (5s default),
        the collector's batch processor and the Data Prepper sink each buffer on their own schedule,
        and those are the delays the waits below are for — forcing them would delete the timing
        being tested.
        """
        for provider in (self.telemetry.logger_provider, self.telemetry.tracer_provider):
            if provider is not None:
                provider.force_flush(timeout_millis=15_000)

    def shutdown(self) -> None:
        self.telemetry.shutdown()

    def span(self, name: str = "live.emit") -> Any:
        """An ambient span for an emission to happen inside.

        Not a convenience. :func:`~referraleasy_telemetry.events.derive_event_id` **refuses** an
        emission that has no span and no discriminator, because two such emissions are genuinely
        indistinguishable and hashing them together would quietly under-count. A test that emitted
        from outside any span would therefore be testing that refusal rather than the pipeline.

        Real services emit inside the span for the work being measured — that is what puts the
        ``traceId`` on the document the admin app joins its waterfalls by — so the live tests do the
        same. The refusal itself is covered by
        :class:`tests.telemetry.test_isolation.TestTheGapAboveTheIsolatedLogger`.
        """
        return self.tracer.start_as_current_span(name)

    # -- reading -------------------------------------------------------------

    def scope(self, *clauses: Mapping[str, Any]) -> dict[str, Any]:
        """A query scoped to this test's own writes, with any extra clauses beside the stamp."""
        return {
            "bool": {
                "filter": [
                    {"term": {"log.attributes.deployment@annotation.keyword": self.marker}},
                    *[dict(clause) for clause in clauses],
                ]
            }
        }

    def search(self, index: str, body: Mapping[str, Any]) -> dict[str, Any]:
        import httpx

        response = httpx.post(f"{self.opensearch}/{index}/_search", json=dict(body), timeout=10.0)
        response.raise_for_status()
        return response.json()

    def documents(
        self,
        event_name: str,
        *,
        count: int = 1,
        extra: Sequence[Mapping[str, Any]] = (),
        timeout: float = 60.0,
    ) -> list[dict[str, Any]]:
        """Wait for ``count`` documents of one event name, then return every matching document.

        Returns *all* the matching documents rather than the first, because several of the claims
        below are about how many things are in the index rather than about one of them.
        """
        query = self.scope({"term": {"log.attributes.event@name.keyword": event_name}}, *extra)
        found = wait_for_count(
            opensearch_url=self.opensearch,
            index=self.event_index,
            query=query,
            count=count,
            timeout=timeout,
        )
        assert found >= count, (
            f"expected {count} `{event_name}` document(s) carrying marker {self.marker!r} to reach "
            f"OpenSearch within {timeout:.0f}s and {found} arrived. The document was emitted with "
            f"the same query body used here, so a shortfall means the pipeline did not carry it — "
            f"not that the filter was wrong. Check the collector and Data Prepper logs."
        )
        response = self.search(
            self.event_index,
            {"size": 100, "query": query, "sort": [{"_doc": {"order": "asc"}}]},
        )
        return [hit["_source"] for hit in response.get("hits", {}).get("hits", [])]

    def document(self, event_name: str, **kwargs: Any) -> dict[str, Any]:
        return self.documents(event_name, **kwargs)[0]

    def trace(
        self, trace_id: str, *, count: int = 1, timeout: float = 60.0
    ) -> list[dict[str, Any]]:
        """Wait for the documents of one trace, found by trace id.

        Scoped by ``traceId`` rather than by the marker: the id is generated in this test, the field
        is a mapped keyword, and the match is exact — nothing else on the machine can collide with
        it. It is also the only exact key available, since a trace document carries the deployment
        annotation as a *resource* attribute, which is a dynamically mapped field.
        """
        query = {"term": {"traceId": trace_id}}
        found = wait_for_count(
            opensearch_url=self.opensearch,
            index=self.trace_index,
            query=query,
            count=count,
            timeout=timeout,
        )
        assert found >= count, (
            f"expected {count} span document(s) for trace {trace_id} within {timeout:.0f}s and "
            f"{found} arrived"
        )
        response = self.search(self.trace_index, {"size": 100, "query": query})
        return [hit["_source"] for hit in response.get("hits", {}).get("hits", [])]

    def cardinality(self, query: Mapping[str, Any], *, field: str) -> tuple[int, int]:
        """``(distinct values of ``field``, matching documents)`` — the dedup pair."""
        response = self.search(
            self.event_index,
            {
                "size": 0,
                "query": dict(query),
                "track_total_hits": True,
                "aggs": {
                    "distinct": {"cardinality": {"field": field, "precision_threshold": 40_000}}
                },
            },
        )
        documents = int(response["hits"]["total"]["value"])
        distinct = int(response["aggregations"]["distinct"]["value"])
        return distinct, documents

    def percentiles(
        self, query: Mapping[str, Any], *, field: str, percents: Sequence[int]
    ) -> dict[str, float]:
        """A percentiles aggregation, so a test can assert a distribution without the query layer."""
        response = self.search(
            self.event_index,
            {
                "size": 0,
                "query": dict(query),
                "aggs": {
                    "p": {
                        "percentiles": {
                            "field": field,
                            "percents": list(percents),
                            "keyed": True,
                        }
                    }
                },
            },
        )
        return response["aggregations"]["p"]["values"]


@pytest.fixture
def live_pipeline(live_stack: tuple[str, str]) -> Iterator[LivePipeline]:
    """A pipeline wired to the running stack, for one test.

    Function-scoped, and therefore one provider pair per test: it costs microseconds, and a
    session-scoped one would have every test writing under one marker, which is the shared-state
    problem the marker exists to avoid.
    """
    collector, opensearch = live_stack
    pipeline = LivePipeline(collector, opensearch)
    try:
        yield pipeline
    finally:
        pipeline.shutdown()


class RecordingLogger:
    """A logger that keeps what it was asked to emit.

    The SDK's own logger needs a provider and a processor to be observable, which makes a test
    about *what gets emitted* into a test about the SDK. This stands in for the logger in the unit
    tests — :class:`~referraleasy_telemetry.events.TelemetryEmitter` only ever calls ``emit`` — so
    those tests assert on the record and nothing else.
    """

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def emit(self, **kwargs: Any) -> None:
        self.records.append(kwargs)

    # -- assertions the tests share ------------------------------------------
    def by_event(self, event_name: str) -> list[dict[str, Any]]:
        """Every emitted record whose ``event.name`` attribute matches."""
        return [
            record
            for record in self.records
            if record.get("attributes", {}).get("event.name") == event_name
        ]

    def by_metric(self, metric_name: str) -> list[dict[str, Any]]:
        """Every emitted record whose ``metric.name`` attribute matches."""
        return [
            record
            for record in self.records
            if record.get("attributes", {}).get("metric.name") == metric_name
        ]

    def attribute(self, key: str) -> Any:
        """The value of one attribute on the single emitted record, asserting there is exactly one."""
        assert len(self.records) == 1, f"expected one record, got {len(self.records)}"
        return self.records[0]["attributes"].get(key)


@pytest.fixture
def recorder() -> RecordingLogger:
    return RecordingLogger()


@pytest.fixture
def emitter(recorder: RecordingLogger):
    """A ``TelemetryEmitter`` writing into the recorder, with no provider and no pipeline."""
    from referraleasy_telemetry import TelemetryEmitter

    return TelemetryEmitter(recorder)


@pytest.fixture
def local_tracer() -> Iterator[Any]:
    """A tracer on a private provider, for tests that need an ambient span.

    Emissions carry a ``trace_id`` and ``span_id`` only when one is active, and the derived
    ``event.id`` depends on them: an emission with neither and no discriminator is refused outright
    by :func:`referraleasy_telemetry.events.derive_event_id`. Real services emit inside spans, so
    the tests that exercise the emitter use one of these rather than working around the refusal —
    a workaround would leave the trace-id path untested.
    """
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider(resource=Resource.create({"service.name": "telemetry-tests"}))
    tracer = provider.get_tracer("referraleasy.telemetry.tests", "0.1.0")
    yield tracer
    provider.shutdown()


@pytest.fixture
def span_factory() -> Iterator[Callable[..., Any]]:
    """Build a *real* ``ReadableSpan`` by recording one through the SDK and ending it.

    Real, not a stub, because the things worth testing about the exporter are the SDK's own shapes:
    ``attributes`` being a ``MappingProxyType``, ``instrumentation_scope`` carrying a version,
    ``kind`` being the API enum that is offset from the OTLP one. A hand-rolled fake would take
    whatever shape the test expected and would have hidden every one of the encoding bugs found
    here.

    The returned callable takes ``name``, ``attributes``, ``kind``, ``links``, ``events`` and
    ``status``. Each call returns the span it just recorded.
    """
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "telemetry-tests"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("referraleasy.telemetry.tests", "0.1.0")

    def build(
        *,
        name: str = "probe",
        attributes: Mapping[str, Any] | None = None,
        kind: Any = None,
        links: Sequence[Any] = (),
        events: Sequence[tuple[str, Mapping[str, Any]]] = (),
        status: Any = None,
        parent: Any = None,
    ) -> Any:
        from opentelemetry.trace import set_span_in_context
        from opentelemetry.trace.span import NonRecordingSpan

        kwargs: dict[str, Any] = {}
        if kind is not None:
            kwargs["kind"] = kind
        if links:
            kwargs["links"] = list(links)
        if parent is not None:
            # A parent is passed as the *span* that was built before it, so a test can build a real
            # parent/child pair and assert on what ``parentSpanId`` does and does not contain.
            kwargs["context"] = set_span_in_context(NonRecordingSpan(parent.context))
        with tracer.start_as_current_span(
            name, attributes=dict(attributes or {}), **kwargs
        ) as span:
            for event_name, event_attributes in events:
                span.add_event(event_name, dict(event_attributes))
            if status is not None:
                span.set_status(status)
        return exporter.get_finished_spans()[-1]

    yield build
    provider.shutdown()


@pytest.fixture
def log_record_factory() -> Iterator[Callable[..., Any]]:
    """Build a *real* ``ReadableLogRecord`` by emitting one through the SDK.

    The shape matters and is not guessable: ``timestamp`` is ``None`` unless the producer passes
    one while ``observed_timestamp`` is always set, and ``resource`` lives on the wrapper rather
    than on the ``LogRecord`` inside it. Both of those were discovered by reading a live document
    back out of OpenSearch after a rejected batch, which is why this fixture builds the real thing.
    """
    from opentelemetry._logs import SeverityNumber
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider(resource=Resource.create({"service.name": "telemetry-tests"}))
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    sdk_logger = provider.get_logger("referraleasy.telemetry.tests", "0.1.0")

    def build(
        *,
        body: Any = "probe",
        attributes: Mapping[str, Any] | None = None,
        severity: Any = SeverityNumber.INFO,
        timestamp: int | None = None,
        trace_id: int = 0,
        span_id: int = 0,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "severity_number": severity,
            "body": body,
            "attributes": dict(attributes or {}),
        }
        if timestamp is not None:
            kwargs["timestamp"] = timestamp
        if trace_id or span_id:
            from opentelemetry.trace import SpanContext, TraceFlags, use_span
            from opentelemetry.trace.span import NonRecordingSpan

            context = SpanContext(
                trace_id=trace_id,
                span_id=span_id,
                is_remote=False,
                trace_flags=TraceFlags(0x01),
            )
            with use_span(NonRecordingSpan(context), end_on_exit=False):
                sdk_logger.emit(**kwargs)
        else:
            sdk_logger.emit(**kwargs)
        return exporter.get_finished_logs()[-1]

    yield build
    provider.shutdown()


@pytest.fixture
def term_query() -> Callable[..., dict[str, Any]]:
    """Build a one-term filter, refusing the form that silently matches nothing.

    **Measured on the running stack.** A ``term`` query on ``log.attributes.deployment@annotation``
    returns **0 hits**, while the identical query on ``log.attributes.deployment@annotation.keyword``
    returns all 7. Data Prepper writes a dotted OTLP attribute key as a nested field, OpenSearch maps
    the string dynamically as ``text`` with a ``keyword`` sub-field, and a ``term`` query against an
    analysed field is compared against a *token* rather than against the whole value. Nothing errors.

    That makes this the worst kind of mistake for a live test: the query does not fail, it returns
    an empty result set, so an assertion phrased as "these documents are not there" passes for
    entirely the wrong reason. The guard below turns it into a message instead. It fires only on
    ``@``-bearing names, which is exactly the set of dynamically mapped attributes — top-level
    fields (``body``, ``time``, ``serviceName``) are mapped by the index template and need no suffix.

    Pass ``numeric=True`` for a numeric attribute: numbers are mapped without a sub-field.
    """

    def build(field_name: str, value: Any, *, numeric: bool = False) -> dict[str, Any]:
        if not numeric and "@" in field_name and not field_name.endswith(".keyword"):
            raise ValueError(
                f"{field_name!r} is a dynamically mapped string attribute, so a term query against "
                f"it matches tokens and returns nothing — not an error, just silence. Use "
                f"{field_name}.keyword (or `referraleasy_telemetry.query.keyword_field`). If the "
                f"attribute is numeric, pass numeric=True."
            )
        return {"term": {field_name: value}}

    return build


def sources_of(response_json: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    """The ``_source`` of every hit in a search response."""
    return [hit.get("_source", {}) for hit in response_json.get("hits", {}).get("hits", [])]
