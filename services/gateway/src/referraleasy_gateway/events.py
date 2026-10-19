# ruff: noqa: RUF002  # Turkish quoted from §12 of ReferralEasy_Architecture_v3.md is kept verbatim
"""The event log behind the stream, and the frames it produces (§12).

Three properties of §12 are decided here, so they are decided once:

**`seq` is monotonic per workflow.** A client that sees a gap knows it missed something and replays.
The counter lives beside the log that assigns it, so no caller can emit an event without one.

**`event_id` is content-addressed, not a counter.** It is the first 32 hex characters of a SHA-256
over `(workflow_id, seq, type, payload)`. Two consequences the contract depends on: replaying from an
id reproduces the sequence that id belongs to (the id *is* the identity of that event, so it cannot
be re-pointed at a different one by a later append), and a duplicate delivery of the same event
carries the same id, which is what lets §15 deduplicate authoritative events rather than
double-counting them.

**Some events are not state.** `partial_text` and `agent_progress` are explicitly *not* final state
(§12: "Kısmi agent çıktısı final state olarak commit edilmez"), and `notice` is information about
the surface rather than a change to it. `AUTHORITATIVE_EVENT_TYPES` names the three that are, and
`EventLog.committed_events` is the only way to fold the log into state — so a partial cannot be
committed by forgetting to filter it.

**The wire shape is the contract's, not this file's.** Events are built by constructing
`referraleasy_contracts.generated.sse.StreamEvent`, the generated binding of `schemas/sse.json`. An
event this service can produce but the schema forbids raises here, at the moment of production,
rather than surfacing as a client-side parse failure in a browser.

**Frames carry no `event:` name.** `apps/web` consumes the stream through `EventSource.onmessage`,
which only fires for the default event name; naming the event would silently deliver nothing. The
event's *type* travels inside the JSON, where the contract puts it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from threading import Lock
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel

from referraleasy_contracts.generated.sse import StreamEvent, StreamEventType

__all__ = [
    "AUTHORITATIVE_EVENT_TYPES",
    "NON_AUTHORITATIVE_EVENT_TYPES",
    "EventLog",
    "content_event_id",
    "frames",
    "to_sse_frame",
]

#: The event types that change what is true. A `surface_update` replaces a rendered instance, a
#: `workflow_state` moves the workflow, and a `terminal` ends the stream with an outcome.
AUTHORITATIVE_EVENT_TYPES: Final[frozenset[StreamEventType]] = frozenset(
    {
        StreamEventType.surface_update,
        StreamEventType.workflow_state,
        StreamEventType.terminal,
    }
)

#: Everything else. `partial_text` is the one §12 names ("partial agent output is never committed");
#: `agent_progress` is telemetry about work in progress; `notice` is an inline explanation.
NON_AUTHORITATIVE_EVENT_TYPES: Final[frozenset[StreamEventType]] = frozenset(
    {
        StreamEventType.partial_text,
        StreamEventType.agent_progress,
        StreamEventType.notice,
    }
)

#: Every type must be classified, or a new one would default to "not state" silently.
assert set(StreamEventType) == AUTHORITATIVE_EVENT_TYPES | NON_AUTHORITATIVE_EVENT_TYPES, (
    "every StreamEventType must be classified as authoritative or not"
)

_EVENT_ID_LENGTH: Final = 32


def content_event_id(
    workflow_id: UUID, seq: int, event_type: StreamEventType, payload: Mapping[str, Any] | None
) -> str:
    """The event's content-addressed id: 32 hex characters, per the schema's pattern.

    Canonical JSON (sorted keys, no insignificant whitespace) so the same logical event always hashes
    the same way, whatever order a dict happened to be built in.
    """
    material = json.dumps(
        {
            "workflow_id": str(workflow_id),
            "seq": seq,
            "type": event_type.value,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:_EVENT_ID_LENGTH]


def _payload_dict(payload: BaseModel | Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Normalise a payload model to its wire form.

    `exclude_none=True`: an optional field that is absent and one that is null mean the same thing in
    the contract (both are `["x", "null"]` optional), and omitting them keeps a frame readable in a
    browser's network tab.
    """
    if payload is None:
        return None
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json", exclude_none=True)
    return dict(payload)


class EventLog:
    """Append-only, per workflow, in process. The stream's source of truth and its replay cursor.

    In-process on purpose for Phase 2.1: the durable event store is a table the migrations do not yet
    have, and inventing one here would put a second source of truth next to `workflow`. What this log
    owns is the *transport* view — seq, ids, timings — while every fact it reports about the workflow
    comes from the control state. Losing the log loses a replay window, not a booking, which is the
    right failure for a transport-side cache and is asserted in `test_stream_replay.py`.
    """

    def __init__(self, *, synthetic: bool, per_workflow_limit: int = 5000) -> None:
        self._by_workflow: dict[UUID, list[StreamEvent]] = {}
        self._lock = Lock()
        self._synthetic = synthetic
        self._limit = per_workflow_limit

    def append(
        self,
        workflow_id: UUID,
        event_type: StreamEventType,
        payload: BaseModel | Mapping[str, Any] | None = None,
        *,
        created_at: datetime,
        progress_elapsed_ms: int | None = None,
    ) -> StreamEvent:
        """Append an event and return it. `progress_elapsed_ms` is the turn's progress reading."""
        body = _payload_dict(payload)
        with self._lock:
            events = self._by_workflow.setdefault(workflow_id, [])
            seq = (events[-1].seq + 1) if events else 0
            event = StreamEvent(
                event_id=content_event_id(workflow_id, seq, event_type, body),
                workflow_id=workflow_id,
                seq=seq,
                type=event_type,
                payload=body,
                progress_elapsed_ms=progress_elapsed_ms,
                created_at=created_at,
                synthetic=self._synthetic,
            )
            events.append(event)
            if len(events) > self._limit:  # pragma: no cover - long demos only
                del events[: len(events) - self._limit]
            return event

    def events_for(self, workflow_id: UUID) -> tuple[StreamEvent, ...]:
        with self._lock:
            return tuple(self._by_workflow.get(workflow_id, ()))

    def replay(
        self,
        workflow_id: UUID,
        *,
        after_event_id: str | None = None,
        after_seq: int | None = None,
        limit: int | None = None,
    ) -> tuple[StreamEvent, ...]:
        """The events a reconnecting client should receive.

        The cursor is the contract's `event_id`. An id that is not in the log (an evicted window, a
        client from another workflow, a typo) starts from the beginning rather than from nowhere:
        a client that cannot be resumed is resumed from zero, because showing the patient the whole
        sequence again is recoverable and showing them nothing is not. `after_seq` is the fallback
        for a client that has a sequence but no id.

        A replayed event is the *same object* that was stored — same `event_id`, same `seq`, same
        payload — which is what makes a replay idempotent on the client, and what
        `test_stream_replay.py` asserts byte-for-byte.
        """
        events = self.events_for(workflow_id)
        if after_event_id:
            index = next(
                (i for i, event in enumerate(events) if event.event_id == after_event_id), None
            )
            if index is not None:
                events = events[index + 1 :]
            # An unknown id leaves `events` as the full sequence, deliberately.
        elif after_seq is not None and after_seq >= 0:
            events = tuple(event for event in events if event.seq > after_seq)
        return events[:limit] if limit is not None else events

    def latest(self, workflow_id: UUID) -> StreamEvent | None:
        events = self.events_for(workflow_id)
        return events[-1] if events else None

    def has_terminal(self, workflow_id: UUID) -> bool:
        """Whether the workflow's stream is closed. §12: `terminal` closes the stream, and a client
        must not infer completion from the stream merely ending."""
        return any(event.type is StreamEventType.terminal for event in self.events_for(workflow_id))

    def committed_events(self, workflow_id: UUID) -> tuple[StreamEvent, ...]:
        """The log, filtered to what is allowed to be state.

        The single filter for §12's "partial agent output is never committed as final state". Every
        projection — what state the workflow is in, which surfaces exist — folds *this* sequence, so
        a partial event cannot reach state by a route that forgot to check.
        """
        return tuple(
            event
            for event in self.events_for(workflow_id)
            if event.type in AUTHORITATIVE_EVENT_TYPES
        )

    def non_authoritative(self, workflow_id: UUID) -> tuple[StreamEvent, ...]:
        """The complement, for tests and for a UI that wants to show progress separately."""
        return tuple(
            event
            for event in self.events_for(workflow_id)
            if event.type in NON_AUTHORITATIVE_EVENT_TYPES
        )


def to_sse_frame(event: StreamEvent) -> str:
    """One SSE frame: `id:` from the contract's `event_id`, `data:` a single-line JSON document.

    No `event:` line — see the module docstring: `apps/web` reads frames through
    `EventSource.onmessage`, so naming the event would deliver nothing at all.
    """
    body = json.dumps(event.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)
    return f"id: {event.event_id}\ndata: {body}\n\n"


def frames(events: Iterable[StreamEvent]) -> Iterable[str]:
    """The frames for a sequence of events."""
    return (to_sse_frame(event) for event in events)
