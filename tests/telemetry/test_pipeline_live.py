# ruff: noqa: RUF002  # Turkish quoted from §15 of ReferralEasy_Architecture_v3.md is kept verbatim
"""The pipeline, proven end to end: emit through the real collector, read the document back (§15).

    Apps (gateway / A2A / mock API / worker) -> OTel Collector -> Data Prepper -> OpenSearch

**This is the stronger of the two verifications, and the distinction is the point of the file.**
``test_export.py`` builds a payload and hands it to a mock transport; that proves the serialisation
and nothing else. These tests start a real span, call the real emitter, export through the package's
real OTLP/HTTP JSON exporter to the *running* collector, wait for Data Prepper to flush, and then
search OpenSearch for the document that should exist. Every claim §15 makes about the pipeline is
checked here or it is not checked at all.

The tests need the compose stack. When it is not reachable ``live_stack`` skips them — loudly, with
the name of the missing service, because a run where these skipped verified nothing about the
pipeline and the report has to say so rather than counting them as passes.

Two consequences of testing against a running stack are designed around rather than tolerated:

* **Every test writes under its own ``deployment.annotation``.** The indices are shared with the
  demo and with anything else emitting on this machine, so each test stamps its writes and scopes
  every read by the stamp. A zero in an assertion here means the pipeline did not carry the record.
* **Waiting is part of the measurement.** Three buffers sit between the emitter and the index (the
  SDK's batch processor, the collector's, and Data Prepper's 2s sink flush), and the events template
  refreshes every 10s. The tests wait through that instead of refreshing around it, because the
  timing is part of what §15 claims works.
"""

from __future__ import annotations

import json

import pytest

from referraleasy_telemetry.conventions import OutcomeClass, Synthetic
from referraleasy_telemetry.identity import CorrelationIds, current_ids, resume_after_wait

pytestmark = pytest.mark.integration

#: A value that must never appear in a document, planted in an attribute the policy knows about.
PLANTED_EMAIL = "planted.patient@example.test"
#: A second one, planted under a key the policy knows about for a different reason.
PLANTED_TOKEN = "Bearer planted-9f8e7d6c5b4a3210"
#: A third, planted in free text under a key nobody anticipated — the case a key list cannot cover.
PLANTED_IN_FREE_TEXT = f"upstream said: {PLANTED_EMAIL} (api_key=planted-9f8e7d6c5b4a3210)"


class TestTheEventPipeline:
    """One measurement, from the emitter to a document that a query can read."""

    def test_an_emitted_measurement_arrives_with_the_attributes_the_charts_need(
        self, live_pipeline
    ) -> None:
        with live_pipeline.span():
            live_pipeline.emitter.agent_trigger(
                agent_role="scheduling", synthetic=Synthetic.SYNTHETIC
            )
        live_pipeline.flush()

        document = live_pipeline.document("agent_trigger")

        assert document["log.attributes.event@name"] == "agent_trigger"
        assert document["log.attributes.metric@name"] == "event.count"
        assert document["log.attributes.metric@value"] == 1.0
        assert document["log.attributes.metric@unit"] == "count"
        assert document["log.attributes.agent@role"] == "scheduling"
        assert document["log.attributes.synthetic"] == "synthetic"
        # The stamp every read in this file is scoped by, so it has to be readable.
        assert document["log.attributes.deployment@annotation"] == live_pipeline.marker

    def test_the_document_names_the_section_and_the_service_that_produced_it(
        self, live_pipeline
    ) -> None:
        """A document in a shared index has to be attributable, and §15's pipeline is a claim about
        provenance: which service, and which part of the specification asked for the record."""
        with live_pipeline.span():
            live_pipeline.emitter.agent_trigger(
                agent_role="scheduling", synthetic=Synthetic.SYNTHETIC
            )
        live_pipeline.flush()

        document = live_pipeline.document("agent_trigger")

        assert document["serviceName"] == live_pipeline.service_name
        assert document["resource.attributes.service@name"] == live_pipeline.service_name
        assert document["resource.attributes.referraleasy@spec@section"] == "§15"
        assert document["instrumentationScope.name"] == "referraleasy.telemetry"

    def test_the_severity_survives_the_pipeline(self, live_pipeline) -> None:
        """The live regression test for a defect that shipped into a whole index.

        ``SeverityNumber`` is a plain ``Enum`` in this SDK rather than an ``IntEnum``, so
        ``int(member)`` raises ``TypeError``. The exporter's guard caught it and returned 0, which
        means **every** document reached OpenSearch with ``severityNumber: 0`` and an empty
        ``severityText`` — an error-severity filter would have returned nothing, with no error
        anywhere. The pre-fix documents are still in the neighbouring index, so both readings can be
        seen side by side: they show ``severityNumber: 0``.
        """
        live_pipeline.emitter.attempt(
            agent_role="scheduling",
            duration_ms=9000.0,
            number=1,
            outcome=OutcomeClass.ERROR,
            error_type="timeout",
            synthetic=Synthetic.SYNTHETIC,
        )
        live_pipeline.emitter.attempt(
            agent_role="scheduling",
            duration_ms=120.0,
            number=1,
            outcome=OutcomeClass.SUCCESS_FULL,
            synthetic=Synthetic.SYNTHETIC,
        )
        live_pipeline.flush()

        failed = live_pipeline.document(
            "latency", extra=[{"term": {"log.attributes.outcome@class.keyword": "error"}}]
        )
        assert failed["severityNumber"] == 17, "ERROR is 17 in the OTLP severity numbering"
        assert failed["severityText"] == "ERROR"

        succeeded = live_pipeline.document(
            "latency", extra=[{"term": {"log.attributes.outcome@class.keyword": "success_full"}}]
        )
        assert succeeded["severityNumber"] == 9, "INFO is 9"
        assert succeeded["severityText"] == "INFO"

    def test_an_empty_result_is_a_success_and_is_stored_as_one(self, live_pipeline) -> None:
        """§15: "Empty geçerli sonuç error değildir". The failure this guards against is not in the
        index — it is in the *reading*, where an empty answer gets counted as a failure and the
        failure rate then disagrees with the number of things that actually failed."""
        live_pipeline.emitter.attempt(
            agent_role="provider_matching",
            duration_ms=140.0,
            number=1,
            outcome=OutcomeClass.SUCCESS_EMPTY,
            synthetic=Synthetic.SYNTHETIC,
        )
        live_pipeline.flush()

        document = live_pipeline.document(
            "outcome", extra=[{"term": {"log.attributes.outcome@class.keyword": "success_empty"}}]
        )
        assert document["log.attributes.outcome@class"] == "success_empty"
        assert document["severityText"] == "INFO", "an empty result is not an alert"
        assert document["severityNumber"] == 9

        # And the other direction: the same record is not reachable as an error.
        response = live_pipeline.search(
            live_pipeline.event_index,
            {
                "size": 0,
                "query": live_pipeline.scope(
                    {"term": {"log.attributes.outcome@class.keyword": "error"}},
                    {"term": {"log.attributes.agent@role.keyword": "provider_matching"}},
                ),
                "track_total_hits": True,
            },
        )
        assert response["hits"]["total"]["value"] == 0


class TestTheTracePipeline:
    """The trace side, which is a second Data Prepper pipeline writing a second index."""

    def test_the_a2a_ids_reach_the_span_when_the_service_passes_them(self, live_pipeline) -> None:
        """§15: "A2A task_id/context_id/message_id trace'e attribute olarak eklenir".

        The service starts its own spans, so the ids ride along as span attributes —
        ``correlation.attributes()`` is what the adopting service passes to
        ``start_as_current_span(..., attributes=...)``. Nothing in this package can do it on the
        service's behalf, so what is verified here is that the pattern works and that the ids survive
        the pipeline: a reader can find every span of one A2A task from the trace index alone.

        The three ids are **distinct values**, because §15 says they are never equated and
        ``CorrelationIds`` refuses a set that shares one.
        """
        correlation = CorrelationIds(
            conversation_id="conv-1",
            workflow_id="wf-1",
            booking_request_id="br-1",
            task_id="task-live-1",
            context_id="ctx-live-1",
            message_id="msg-live-1",
        )
        with live_pipeline.tracer.start_as_current_span(
            "live.a2a", attributes=correlation.attributes()
        ):
            trace_id, _ = current_ids()

        live_pipeline.flush()
        document = live_pipeline.trace(trace_id)[0]

        assert document["span.attributes.a2a@task_id"] == "task-live-1"
        assert document["span.attributes.a2a@context_id"] == "ctx-live-1"
        assert document["span.attributes.a2a@message_id"] == "msg-live-1"
        # The four workflow ids keep their bare names, so a filter on workflow_id cannot match an
        # A2A task by accident.
        assert document["span.attributes.workflow_id"] == "wf-1"
        assert document["span.attributes.conversation_id"] == "conv-1"

    def test_the_log_document_carries_the_span_that_produced_it(self, live_pipeline) -> None:
        """§15's identity claim, end to end. The ``traceId`` in the *document* is the trace id of
        the span that emitted it, which is the whole basis for joining a log line to a waterfall."""
        with live_pipeline.tracer.start_as_current_span("live.identity"):
            trace_id, span_id = current_ids()
            live_pipeline.emitter.agent_trigger(
                agent_role="scheduling", synthetic=Synthetic.SYNTHETIC
            )
        live_pipeline.flush()

        document = live_pipeline.document("agent_trigger")

        assert len(trace_id) == 32 and int(trace_id, 16) > 0
        assert document["traceId"] == trace_id
        assert document["spanId"] == span_id

    def test_a_child_span_arrives_under_its_parent(self, live_pipeline) -> None:
        with live_pipeline.tracer.start_as_current_span("live.parent") as parent:
            parent.set_attribute("probe.marker", live_pipeline.marker)
            parent_ids = current_ids()
            with live_pipeline.tracer.start_as_current_span("live.child") as child:
                child.set_attribute("probe.marker", live_pipeline.marker)
                child_ids = current_ids()
        live_pipeline.flush()

        spans = {
            document["name"]: document for document in live_pipeline.trace(child_ids[0], count=2)
        }

        assert set(spans) == {"live.parent", "live.child"}
        assert spans["live.child"]["parentSpanId"] == parent_ids[1]
        assert spans["live.parent"]["parentSpanId"] in ("", None), "the parent is a root span"
        assert spans["live.child"]["durationInNanos"] > 0
        assert spans["live.child"]["span.attributes.probe@marker"] == live_pipeline.marker

    def test_the_kind_is_the_wire_enum_and_not_the_api_one(self, live_pipeline) -> None:
        """The Python ``SpanKind`` is offset by one from the OTLP wire enum — API ``INTERNAL`` is 0
        while the wire's ``SPAN_KIND_INTERNAL`` is 1 — and the exporter shipped the API value first.
        A span that OpenSearch classified as ``SPAN_KIND_UNSPECIFIED`` is a span no filter matches,
        so the assertion is on the name rather than on the number."""
        with live_pipeline.tracer.start_as_current_span("live.kind") as span:
            span.set_attribute("probe.marker", live_pipeline.marker)
            trace_id, _ = current_ids()
        live_pipeline.flush()

        document = live_pipeline.trace(trace_id)[0]
        assert document["kind"] == "SPAN_KIND_INTERNAL"

    def test_an_async_resume_starts_a_new_trace_linked_to_the_parked_one(
        self, live_pipeline
    ) -> None:
        """§15: "Async worker/HITL uzun bekleme sonrası yeni trace + span link oluşturur".

        Both halves are asserted against the stored documents: the resumed work is in a *different*
        trace (so the waterfall of an hours-long wait does not contain the wait), and its ``links``
        array names the parked span (so the two are still connected).
        """
        with live_pipeline.tracer.start_as_current_span("live.parked") as parked:
            parked.set_attribute("probe.marker", live_pipeline.marker)
            parked_ids = current_ids()

        with resume_after_wait(
            parked.get_span_context(),
            "live.resumed",
            tracer=live_pipeline.tracer,
            attributes={"probe.marker": live_pipeline.marker},
        ) as resumed:
            resumed_ids = current_ids()
            # The link is on the span, so it is asserted on the span as well as on the document:
            # a link that the SDK records and the exporter drops is a different bug from one that
            # was never recorded, and both are worth telling apart when this fails.
            links_on_the_span = resumed.links
            assert len(links_on_the_span) == 1
            assert links_on_the_span[0].context.trace_id == parked.get_span_context().trace_id

        live_pipeline.flush()

        assert resumed_ids[0] != parked_ids[0], "a new trace, not a continuation"
        document = live_pipeline.trace(resumed_ids[0])[0]
        assert document["traceId"] == resumed_ids[0]
        assert document["parentSpanId"] in ("", None), "a root span, so the wait is not in its path"

        links = document["links"]
        assert len(links) == 1
        assert links[0]["traceId"] == parked_ids[0]
        assert links[0]["spanId"] == parked_ids[1]
        # Data Prepper flattens every dot in an attribute key, including inside a link.
        assert links[0]["attributes"]["link@reason"] == "resumed_after_wait"


class TestWhatMustNotReachTheIndex:
    def test_a_planted_identity_never_appears_in_the_stored_document(self, live_pipeline) -> None:
        """The claim is not "the payload is masked", it is "this value is not in it".

        So a value is planted, emitted through the real collector, and then looked for in the raw
        JSON of the document OpenSearch stored. Both layers are exercised by doing it this way: the
        package masks before the payload leaves the process, and the collector's own
        ``transform/mask_pii`` and ``redaction/scrub`` run on everything that arrives, so a service
        that skipped ``configure_telemetry`` is still covered. If either layer had a hole, the value
        would be in this document.
        """
        with live_pipeline.span():
            live_pipeline.emitter.agent_trigger(
                agent_role="scheduling",
                synthetic=Synthetic.SYNTHETIC,
                extra_fields={
                    # A key the policy redacts, and a key it pseudonymises.
                    "patient.email": PLANTED_EMAIL,
                    "api_key": PLANTED_TOKEN,
                    # A key nobody anticipated, holding a value shape the scrub patterns cover.
                    "upstream_response": PLANTED_IN_FREE_TEXT,
                },
            )
        live_pipeline.flush()

        document = live_pipeline.document("agent_trigger")
        raw = json.dumps(document)

        assert PLANTED_EMAIL not in raw, "an e-mail reached the index"
        assert "planted-9f8e7d6c5b4a3210" not in raw, "a token reached the index"
        # Not vacuous: the record itself made it, so the two assertions above are about masking
        # rather than about a document that never arrived.
        assert document["log.attributes.event@name"] == "agent_trigger"
        assert document["log.attributes.deployment@annotation"] == live_pipeline.marker
        assert "[REDACTED]" in raw, (
            "the masking is visible in the document, not only in the outcome"
        )


class TestTheCounts:
    def test_a_duplicate_delivery_is_counted_once_by_the_event_id(self, live_pipeline) -> None:
        """``event.id`` is derived rather than generated, so a retried emission carries the same id
        and the pipeline can be at-least-once without the chart being wrong.

        Both numbers are asserted, because "one event" and "two documents" are both true: OpenSearch
        has no upsert here, so the dedup is a property of the *count* and the excess is visible on
        purpose. A reader who sees them disagree has learned that a retry happened.
        """
        for _ in range(2):
            live_pipeline.emitter.state_transition(
                from_state="INTAKE", to_state="BOOKED", synthetic=Synthetic.SYNTHETIC
            )
        live_pipeline.flush()

        documents = live_pipeline.documents("state_transition", count=2)
        assert len(documents) == 2, "both deliveries were stored"
        assert documents[0]["log.attributes.event@id"] == documents[1]["log.attributes.event@id"]

        distinct, stored = live_pipeline.cardinality(
            live_pipeline.scope(
                {"term": {"log.attributes.event@name.keyword": "state_transition"}}
            ),
            field="log.attributes.event@id.keyword",
        )
        assert distinct == 1, "one event, however many times it was delivered"
        assert stored == 2

    def test_two_different_transitions_are_two_events(self, live_pipeline) -> None:
        """The mirror case, so the test above cannot pass by counting nothing: the discriminator
        includes the transition, so a different one is a different event.

        The two state names are checked against the 16-state vocabulary by the emitter itself — it
        raises :class:`UnknownDimensionError` for a state that is not one of them, which is why the
        first draft of this test, spelled ``COMPLETED``, failed instead of quietly emitting a
        workflow state the dashboard has never heard of.
        """
        live_pipeline.emitter.state_transition(
            from_state="INTAKE", to_state="BOOKED", synthetic=Synthetic.SYNTHETIC
        )
        live_pipeline.emitter.state_transition(
            from_state="BOOKED", to_state="CANCELLED", synthetic=Synthetic.SYNTHETIC
        )
        live_pipeline.flush()

        live_pipeline.documents("state_transition", count=2)
        distinct, stored = live_pipeline.cardinality(
            live_pipeline.scope(
                {"term": {"log.attributes.event@name.keyword": "state_transition"}}
            ),
            field="log.attributes.event@id.keyword",
        )
        assert (distinct, stored) == (2, 2)


class TestTheDistributionsIncludeTheirFailures:
    def test_the_percentiles_aggregation_reports_min_and_max_as_percentiles(
        self, live_pipeline
    ) -> None:
        """A live check of the aggregation's shape, which is not what it looks like.

        OpenSearch's ``percentiles`` returns *only* the percents it was asked for. Measured here with
        23 documents in range: ``{"50.0": 1.0, "95.0": 2.0, "99.0": 1234.5}`` and no ``min``/``max``
        keys at all. The query layer read ``values["min"]`` for a while and presented ``None`` as
        "no data" beside a valid p95; it now asks for the 0th and 100th percentile, and this test
        asserts that asking that way is what returns the bounds.
        """
        for duration in (100.0, 120.0, 9000.0):
            live_pipeline.emitter.attempt(
                agent_role="scheduling",
                duration_ms=duration,
                number=1,
                outcome=OutcomeClass.SUCCESS_FULL,
                synthetic=Synthetic.SYNTHETIC,
            )
        live_pipeline.flush()
        live_pipeline.documents("latency", count=3)

        query = live_pipeline.scope(
            {"term": {"log.attributes.metric@name.keyword": "latency.attempt.ms"}}
        )
        bounds = live_pipeline.percentiles(
            query, field="log.attributes.metric@value", percents=[0, 50, 95, 99, 100]
        )
        assert bounds["0.0"] == pytest.approx(100.0)
        assert bounds["100.0"] == pytest.approx(9000.0)
        assert bounds["50.0"] == pytest.approx(120.0)

        named = live_pipeline.percentiles(
            query, field="log.attributes.metric@value", percents=[50, 95, 99]
        )
        assert "min" not in named and "max" not in named, (
            "so reading them by name returns nothing, silently"
        )


class TestTheIndexIsWrittenByThePipeline:
    def test_the_document_landed_in_the_daily_index_for_today(self, live_pipeline) -> None:
        """The index name is the retention unit: the 7-day lifecycle policy acts on indices, so a
        document in an index nobody rotates is a document nobody expires."""
        from datetime import UTC, datetime

        with live_pipeline.span():
            live_pipeline.emitter.agent_trigger(
                agent_role="scheduling", synthetic=Synthetic.SYNTHETIC
            )
        live_pipeline.flush()
        query = live_pipeline.scope(
            {"term": {"log.attributes.event@name.keyword": "agent_trigger"}}
        )
        live_pipeline.documents("agent_trigger")

        response = live_pipeline.search(live_pipeline.event_index, {"size": 1, "query": query})
        index_name = response["hits"]["hits"][0]["_index"]
        assert index_name.endswith(datetime.now(UTC).strftime("%Y.%m.%d"))
