"""Identity: the seven ids that must stay distinct, and the span link after an async wait.

§15's two identity rules are both the kind that fail silently. Assigning ``workflow_id`` the
``conversation_id`` value breaks nothing at runtime and produces charts that are wrong in a way
nobody notices; modelling a day-long wait as one trace breaks nothing either, and moves every p95
in the system. So both are enforced here.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager

import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from referraleasy_telemetry.identity import (
    A2A_METADATA_KEYS,
    TRACEPARENT,
    CorrelationIds,
    IdentityConflictError,
    a2a_carrier_from_metadata,
    a2a_metadata_with_context,
    current_ids,
    extract_context,
    extract_span_context,
    inject_context,
    remote_span_context,
    resume_after_wait,
)


@pytest.fixture
def tracer():
    """A private provider, so nothing here touches the process-global one."""
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    yield provider.get_tracer("test.identity")
    provider.shutdown()


class TestDistinctIdentifiers:
    """§15: the four workflow ids and the three A2A ids serve separate purposes and are never
    equated."""

    def test_a_full_set_is_accepted(self) -> None:
        ids = CorrelationIds(
            conversation_id="conv",
            workflow_id="wf",
            command_id="cmd",
            booking_request_id="breq",
            task_id="task",
            context_id="ctx",
            message_id="msg",
        )
        assert len(ids.attributes()) == 7

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("conversation_id", "workflow_id"),
            ("workflow_id", "command_id"),
            ("command_id", "booking_request_id"),
            ("booking_request_id", "task_id"),
            ("task_id", "context_id"),
            ("context_id", "message_id"),
            ("conversation_id", "message_id"),
        ],
    )
    def test_two_ids_sharing_a_value_are_refused(self, left: str, right: str) -> None:
        """The refusal names both fields, so the fix is obvious from the traceback."""
        with pytest.raises(IdentityConflictError) as caught:
            CorrelationIds(**{left: "same", right: "same"})
        assert left in str(caught.value)
        assert right in str(caught.value)
        assert "§15" in str(caught.value)

    def test_absent_ids_are_not_equal_ids(self) -> None:
        """`None` means "not known yet" and must not collide with another `None`."""
        ids = CorrelationIds(conversation_id="conv", workflow_id=None)
        assert ids.attributes() == {"conversation_id": "conv"}

    def test_an_empty_string_is_refused(self) -> None:
        """An empty string is a value, not an absence, and it would correlate everything to
        everything."""
        with pytest.raises(IdentityConflictError, match="present but empty"):
            CorrelationIds(workflow_id="")

    def test_a2a_ids_are_namespaced(self) -> None:
        ids = CorrelationIds(task_id="t", context_id="c", message_id="m")
        assert ids.attributes() == {
            "a2a.task_id": "t",
            "a2a.context_id": "c",
            "a2a.message_id": "m",
        }

    def test_workflow_ids_keep_their_bare_names(self) -> None:
        ids = CorrelationIds(workflow_id="wf")
        assert ids.attributes() == {"workflow_id": "wf"}


class TestPropagation:
    """W3C context injection and extraction, in process. The over-the-wire proof is in
    ``test_propagation_integration.py``."""

    def test_inject_then_extract_preserves_the_trace(self, tracer) -> None:
        with tracer.start_as_current_span("parent"):
            carrier = inject_context()
            trace_id_before, span_id_before = current_ids()

        assert TRACEPARENT in carrier
        assert carrier[TRACEPARENT].startswith("00-")

        with trace.use_span(trace.get_current_span(extract_context(carrier)), end_on_exit=False):
            extracted = trace.get_current_span().get_span_context()

        assert f"{extracted.trace_id:032x}" == trace_id_before
        assert f"{extracted.span_id:016x}" == span_id_before

    def test_an_empty_carrier_yields_no_span_context(self) -> None:
        """A client that does not propagate must not break the service: the trace starts here."""
        ctx = extract_context({})
        assert not trace.get_current_span(ctx).get_span_context().is_valid

    def test_an_invalid_carrier_is_ignored_rather_than_raising(self) -> None:
        ctx = extract_context({TRACEPARENT: "not-a-traceparent"})
        assert not trace.get_current_span(ctx).get_span_context().is_valid

    def test_the_carrier_is_w3c_shaped(self, tracer) -> None:
        """``traceparent`` is ``version-traceid-spanid-flags``; a consumer that parses it by
        position depends on that."""
        with tracer.start_as_current_span("parent"):
            carrier = inject_context()
        version, trace_id, span_id, flags = carrier[TRACEPARENT].split("-")
        assert version == "00"
        assert len(trace_id) == 32
        assert len(span_id) == 16
        assert len(flags) == 2


def _without_an_ambient_span() -> AbstractContextManager[None]:
    """Run a block with no active span, so ``extract_span_context``'s ``None`` is unambiguous.

    The W3C propagator returns the *current* context when the carrier holds nothing usable, so
    "the carrier has no valid context" and "the process has no valid context" are the same
    question. Without this, a test asserting ``None`` would pass or fail depending on whether some
    other test left a span attached. Attaching an invalid, non-recording span makes the ambient
    state explicit rather than assumed.
    """
    from opentelemetry.trace.span import INVALID_SPAN_CONTEXT, NonRecordingSpan

    return trace.use_span(NonRecordingSpan(INVALID_SPAN_CONTEXT), end_on_exit=False)


class TestExtractSpanContext:
    """``extract_span_context`` promises ``None`` when there is nothing valid to link to.

    It could not keep that promise: the body ended in ``...get_span_context() or None``, and the
    value the SDK returns for an empty carrier — a non-recording span's all-zero ``SpanContext`` —
    is neither ``None`` nor falsy, so the fallback was dead code and every input took the same
    branch. A caller writing ``if ctx is None`` would have linked to span
    ``00000000000000000000000000000000``. ``mypy`` reported it as "right operand of ``or`` is never
    evaluated"; these tests pin what the function should have returned.
    """

    def test_an_empty_carrier_yields_none(self) -> None:
        with _without_an_ambient_span():
            assert extract_span_context({}) is None

    def test_a_malformed_traceparent_yields_none(self) -> None:
        with _without_an_ambient_span():
            assert extract_span_context({TRACEPARENT: "not-a-traceparent"}) is None

    def test_the_all_zero_traceparent_yields_none(self) -> None:
        """W3C's invalid value, spelled out: a *parsable* header carrying no trace."""
        with _without_an_ambient_span():
            assert extract_span_context({TRACEPARENT: f"00-{'0' * 32}-{'0' * 16}-01"}) is None

    def test_a_real_carrier_yields_the_remote_context(self, tracer) -> None:
        with tracer.start_as_current_span("remote"):
            carrier = inject_context()
            trace_id, span_id = current_ids()

        ctx = extract_span_context(carrier)
        assert ctx is not None
        assert f"{ctx.trace_id:032x}" == trace_id
        assert f"{ctx.span_id:016x}" == span_id
        assert ctx.is_valid


class TestA2ACarrier:
    """The A2A transport's metadata is a protobuf ``Struct``, so the carrier is a plain mapping.
    The wire-level proof is in ``test_propagation_integration.py``."""

    def test_metadata_round_trips_the_context(self, tracer) -> None:
        with tracer.start_as_current_span("parent"):
            trace_id, _ = current_ids()
            metadata = a2a_metadata_with_context({"existing": "kept"})

        carrier = a2a_carrier_from_metadata(metadata)
        ctx = extract_context(carrier)
        assert trace.get_current_span(ctx).get_span_context().trace_id == int(trace_id, 16)

    def test_existing_metadata_is_preserved(self) -> None:
        metadata = a2a_metadata_with_context({"conversation": "c-1"})
        assert metadata["conversation"] == "c-1"

    def test_only_the_reserved_keys_are_read(self) -> None:
        carrier = a2a_carrier_from_metadata(
            {TRACEPARENT: "00-" + "a" * 32 + "-" + "b" * 16 + "-01", "other": "x"}
        )
        assert set(carrier) <= set(A2A_METADATA_KEYS)

    def test_a_non_string_traceparent_is_dropped_not_coerced(self) -> None:
        assert a2a_carrier_from_metadata({TRACEPARENT: 12345}) == {}

    def test_none_metadata_yields_an_empty_carrier(self) -> None:
        assert a2a_carrier_from_metadata(None) == {}
        assert a2a_carrier_from_metadata({}) == {}

    def test_the_real_struct_type_has_no_get_and_that_is_why_none_is_used(self) -> None:
        """The reason is checkable, so it is checked.

        ``google.protobuf.Struct`` is *virtually* registered as a ``collections.abc.Mapping``, so
        ``isinstance`` passes and the annotation in this module reads as satisfied — while the ABC's
        mixin methods, ``get`` included, are not actually provided. Calling ``metadata.get(key)`` on
        a live ``Message.metadata`` raises ``AttributeError``. Every test here used to hand the
        function a plain ``dict``, so nothing caught it.
        """
        struct = pytest.importorskip("google.protobuf.struct_pb2").Struct()

        assert isinstance(struct, Mapping), "the annotation is satisfied, which is the trap"
        assert not hasattr(struct, "get"), "and yet .get is not there"
        with pytest.raises(ValueError):
            struct["absent"]

    def test_a_real_struct_is_read_correctly(self) -> None:
        struct_pb2 = pytest.importorskip("google.protobuf.struct_pb2")
        metadata = struct_pb2.Struct()
        metadata[TRACEPARENT] = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
        metadata["unrelated"] = "x"

        assert a2a_carrier_from_metadata(metadata) == {
            TRACEPARENT: "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
        }

    def test_a_non_string_in_a_struct_is_dropped_too(self) -> None:
        struct_pb2 = pytest.importorskip("google.protobuf.struct_pb2")
        metadata = struct_pb2.Struct()
        metadata[TRACEPARENT] = 12345.0
        assert a2a_carrier_from_metadata(metadata) == {}

    def test_the_outgoing_metadata_keeps_the_applications_own_nested_values(self, tracer) -> None:
        """``metadata`` belongs to the A2A application. Returning only the string-valued keys
        deleted everything else the caller had put there — a nested object being the obvious one.

        Emitted inside a span, because outside one there is no active context to inject and the
        absence of a ``traceparent`` would then be a fact about the test rather than about the
        function.
        """
        with tracer.start_as_current_span("sender"):
            metadata = a2a_metadata_with_context({"conversation": "c-1", "nested": {"a": "b"}})

        assert metadata["conversation"] == "c-1"
        assert metadata["nested"] == {"a": "b"}
        assert TRACEPARENT in metadata

    def test_the_outgoing_metadata_reads_a_live_struct(self, tracer) -> None:
        """The sending half too: a caller that hands over its own ``Struct`` gets a mapping back
        with its keys intact and a traceparent added."""
        struct_pb2 = pytest.importorskip("google.protobuf.struct_pb2")
        existing = struct_pb2.Struct()
        existing["conversation"] = "c-1"

        with tracer.start_as_current_span("sender"):
            trace_id, _ = current_ids()
            metadata = a2a_metadata_with_context(existing)

        assert metadata["conversation"] == "c-1"
        assert a2a_carrier_from_metadata(metadata)[TRACEPARENT].split("-")[1] == trace_id


class TestResumeAfterWait:
    """§15: "Async workers/HITL create a new trace + span link after a long wait"."""

    def test_the_resumed_trace_is_a_new_trace(self, tracer) -> None:
        with tracer.start_as_current_span("park"):
            parked = trace.get_current_span().get_span_context()
            parked_trace = parked.trace_id

        with resume_after_wait(parked, "resume", tracer=tracer) as span:
            resumed = span.get_span_context()

        assert resumed.trace_id != parked_trace, (
            "resuming inside the parked trace would make one span whose duration is the patient's "
            "lunch break, and every latency percentile would be dominated by human thinking time"
        )

    def test_the_resumed_span_links_back(self, tracer) -> None:
        with tracer.start_as_current_span("park"):
            parked = trace.get_current_span().get_span_context()

        with resume_after_wait(parked, "resume", tracer=tracer) as span:
            links = span.links

        assert len(links) == 1
        assert links[0].context.trace_id == parked.trace_id
        assert links[0].context.span_id == parked.span_id
        assert links[0].attributes["link.reason"] == "resumed_after_wait"

    def test_resuming_without_the_parked_context_still_starts_a_trace(self, tracer) -> None:
        """Losing the link is recoverable; inventing one is not."""
        with resume_after_wait(None, "cold_start", tracer=tracer) as span:
            assert span.get_span_context().is_valid
            assert span.links == ()

    def test_the_new_trace_is_a_root(self, tracer) -> None:
        """A worker resuming from a queue has no ambient parent, and a resumed span that silently
        became a child of whatever span happened to be current would be in the old trace."""
        with tracer.start_as_current_span("park") as parked_span:
            parked = parked_span.get_span_context()
        with resume_after_wait(parked, "resume", tracer=tracer) as span:
            assert span.parent is None


class TestRemoteSpanContext:
    """Reading a link target back from an id, which arrives from OpenSearch as hex."""

    def test_hex_ids_are_accepted(self) -> None:
        ctx = remote_span_context("a" * 32, "b" * 16)
        assert ctx.trace_id == int("a" * 32, 16)
        assert ctx.span_id == int("b" * 16, 16)

    def test_integer_ids_are_accepted(self) -> None:
        ctx = remote_span_context(1, 2)
        assert (ctx.trace_id, ctx.span_id) == (1, 2)

    @pytest.mark.parametrize(
        ("trace_id", "span_id"),
        [
            ("short", "b" * 16),
            ("a" * 32, "short"),
            ("z" * 32, "b" * 16),
        ],
    )
    def test_a_wrong_width_id_is_refused(self, trace_id: str, span_id: str) -> None:
        """A link to an id of the wrong width resolves to nothing, and fails silently."""
        with pytest.raises(ValueError):
            remote_span_context(trace_id, span_id)

    def test_the_all_zero_id_is_refused(self) -> None:
        with pytest.raises(ValueError, match="non-zero"):
            remote_span_context("0" * 32, "b" * 16)


class TestCurrentIds:
    def test_outside_a_span_there_are_no_ids(self) -> None:
        assert current_ids() == ("", "")

    def test_inside_a_span_the_ids_are_hex(self, tracer) -> None:
        with tracer.start_as_current_span("s"):
            trace_id, span_id = current_ids()
        assert len(trace_id) == 32
        assert len(span_id) == 16
        assert int(trace_id, 16) > 0
