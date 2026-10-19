"""The SSE stream: event ids, monotonic `seq`, and a replay that adds nothing (§12).

The three properties §12 hangs on, each asserted against a real stream rather than against the
functions that produce it:

* **`seq` is monotonic per workflow and starts at 0.** A client that sees a gap knows it missed
  something.
* **`event_id` is content-addressed.** It is 32 hex characters, and recomputing it from the event's
  own content reproduces it — which is what makes §15's dedup possible and a replay idempotent.
* **Replay is byte-identical.** The frames a reconnecting client receives from a `last_event_id` are
  the *same bytes* the first delivery produced, not a re-rendering that merely looks the same.

The hold is zero in this suite's settings (`gateway_fixtures.SPEC_ENV`), so a stream can be read with
one request. The tests that need a real hold build their own app and say why.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from fastapi.testclient import TestClient
from gateway_fixtures import Demo, profile_settings

from referraleasy_contracts import validate_def
from referraleasy_contracts.generated.common import WorkflowState
from referraleasy_contracts.generated.sse import StreamEventType
from referraleasy_gateway.app import create_app
from referraleasy_gateway.config import GatewaySettings
from referraleasy_gateway.control import SeedableControlState
from referraleasy_gateway.events import content_event_id
from referraleasy_gateway.timing import ManualClock

ID_PATTERN = re.compile(r"\A[0-9a-f]{32}\Z")


class Frame:
    """One SSE frame, as the client sees it: an id, a JSON document, and whether it is a comment."""

    __slots__ = ("data", "event_id", "raw")

    def __init__(self, *, event_id: str | None, data: dict[str, Any] | None, raw: str) -> None:
        self.event_id = event_id
        self.data = data
        self.raw = raw

    @property
    def comment(self) -> bool:
        return self.data is None

    @property
    def type(self) -> str:
        assert self.data is not None
        return str(self.data["type"])

    @property
    def seq(self) -> int:
        assert self.data is not None
        return int(self.data["seq"])

    @property
    def payload(self) -> dict[str, Any]:
        assert self.data is not None
        return dict(self.data["payload"] or {})

    def __repr__(self) -> str:  # pragma: no cover - failure output only
        return f"Frame(event_id={self.event_id!r}, type={self.data and self.data.get('type')!r})"


def frames(body: str) -> list[Frame]:
    """Split a stream body into frames, keeping each frame's original text.

    The raw text is kept because the replay assertion is about bytes: a comparison of parsed
    dictionaries would pass for a re-rendering that changed its whitespace or its key order.
    """
    parsed: list[Frame] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        event_id: str | None = None
        data: dict[str, Any] | None = None
        for line in block.splitlines():
            if line.startswith("id: "):
                event_id = line[len("id: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        parsed.append(Frame(event_id=event_id, data=data, raw=block + "\n\n"))
    return parsed


def streamed(demo: Demo, workflow_id: str) -> list[Frame]:
    response = demo.events(workflow_id)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return frames(response.text)


def moved_once(demo: Demo) -> str:
    """A workflow with a settled log on it: two moves, so nothing is still arriving."""
    workflow_id = demo.seed()
    for version, target, props in (
        (1, WorkflowState.PROVIDER_MATCHING, None),
        (2, WorkflowState.AWAITING_HOSPITAL_CHOICE, {"options": [{"id": "hospital-1"}]}),
    ):
        response = demo.move(workflow_id, version=version, to_state=target, props=props)
        assert response.status_code == 200, response.text
    return workflow_id


# ---------------------------------------------------------------------------
# Frames and ids
# ---------------------------------------------------------------------------


def test_every_frame_is_a_stream_event_the_contract_accepts(client: TestClient, demo: Demo) -> None:
    """The wire shape is the schema's, checked on a real stream rather than on a constructed event.

    A frame that the generated bindings can produce but the schema forbids would reach a browser as a
    parse failure, which is the failure mode this asserts against.
    """
    workflow_id = moved_once(demo)
    stream = streamed(demo, workflow_id)
    assert stream, "a move emits at least a progress event and a state change"
    for frame in stream:
        assert not frame.comment
        validate_def("sse", "StreamEvent", frame.data)
        assert frame.event_id is not None and ID_PATTERN.match(frame.event_id)
    # No `event:` line: `apps/web` reads frames through `EventSource.onmessage`, which only fires for
    # the default event name, so naming the event would deliver nothing at all.
    assert (
        "event: "
        not in client.get(f"/v1/workflows/{workflow_id}/events", headers=demo.patient).text
    )


def test_seq_is_monotonic_from_zero_per_workflow(client: TestClient, demo: Demo) -> None:
    """A gap is how a client knows it missed something, so there must be none."""
    workflow_id = moved_once(demo)
    other = moved_once(demo)
    stream = streamed(demo, workflow_id)
    seqs = [frame.seq for frame in stream]
    assert seqs == list(range(len(stream)))
    # Per workflow, not global: the second workflow's first event is 0 again, so a cursor from one
    # workflow cannot be confused with a position in another.
    assert streamed(demo, other)[0].seq == 0


def test_an_event_id_is_the_hash_of_the_event_it_names(client: TestClient, demo: Demo) -> None:
    """Content-addressed, and reproducibility is the property a client's dedup depends on.

    Recomputing the id from the fields the frame itself carries has to reproduce it: two deliveries of
    the same event then carry the same id (§15 deduplicates on exactly this), and an id can never be
    re-pointed at a different event by a later append.
    """
    workflow_id = moved_once(demo)
    stream = streamed(demo, workflow_id)
    for frame in stream:
        assert frame.data is not None
        recomputed = content_event_id(
            workflow_id=frame.data["workflow_id"],
            seq=frame.seq,
            event_type=StreamEventType(frame.type),
            payload=frame.payload,
        )
        assert recomputed == frame.event_id
    # And it is a function of the *content*, so a different payload at the same position has a
    # different id. Without that, an id would be nothing but a counter wearing a hash's clothing.
    assert content_event_id(
        workflow_id=workflow_id,
        seq=stream[0].seq,
        event_type=StreamEventType(stream[0].type),
        payload={"text": "one"},
    ) != content_event_id(
        workflow_id=workflow_id,
        seq=stream[0].seq,
        event_type=StreamEventType(stream[0].type),
        payload={"text": "two"},
    )


def test_the_first_event_is_the_progress_event(client: TestClient, demo: Demo) -> None:
    """§12's progress latency is the time to the *first* thing the patient sees.

    A turn that emitted its progress event after doing the work would report a number that measures
    nothing, so the ordering is asserted rather than assumed.
    """
    stream = streamed(demo, moved_once(demo))
    assert stream[0].type == "agent_progress"
    assert stream[0].payload["step"] == "decision"


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def test_replaying_from_an_event_id_returns_the_same_bytes(client: TestClient, demo: Demo) -> None:
    """The whole point of a content-addressed cursor: the retry is byte-identical, not merely equal.

    Compared as text on purpose. A parsed comparison would accept a re-rendering that reordered keys
    or changed its whitespace, and a reconnected `EventSource` is deduplicating on what it received.
    """
    workflow_id = moved_once(demo)
    original = streamed(demo, workflow_id)
    assert len(original) >= 3

    cursor = original[1].event_id
    assert cursor is not None
    response = demo.events(workflow_id, last_event_id=cursor)
    assert response.status_code == 200
    assert response.text == "".join(frame.raw for frame in original[2:])


def test_an_unknown_event_id_replays_from_the_beginning(client: TestClient, demo: Demo) -> None:
    """A cursor the server cannot place resumes from zero.

    The alternative — showing nothing — is unrecoverable on the client, and the sequence is idempotent
    there, so the recoverable direction is the one to fail in.
    """
    workflow_id = moved_once(demo)
    original = streamed(demo, workflow_id)
    response = demo.events(workflow_id, last_event_id="0" * 32)
    assert response.text == "".join(frame.raw for frame in original)


def test_the_last_event_id_header_wins_over_the_query_parameter(
    client: TestClient, demo: Demo
) -> None:
    """Which cursor a server honours is not something a client can discover for itself, so it is
    documented and pinned: the header is what a reconnecting `EventSource` sets by itself."""
    workflow_id = moved_once(demo)
    original = streamed(demo, workflow_id)
    assert len(original) >= 3
    response = client.get(
        f"/v1/workflows/{workflow_id}/events",
        params={"last_event_id": original[-1].event_id},
        headers={**demo.patient, "Last-Event-ID": str(original[0].event_id)},
    )
    assert response.status_code == 200
    assert response.text == "".join(frame.raw for frame in original[1:])


def test_the_cursor_the_snapshot_hands_out_is_the_one_the_stream_accepts(
    client: TestClient, demo: Demo
) -> None:
    """A client resumes with the id it was last given, and a client that is up to date gets nothing.

    Rendered as a client would: read the snapshot, then open the stream from the cursor in it. An
    empty tail here is the reader's model of "I am current".
    """
    workflow_id = moved_once(demo)
    snapshot = demo.snapshot(workflow_id).json()
    response = demo.events(workflow_id, last_event_id=snapshot["last_event_id"])
    assert response.status_code == 200
    assert response.text == ""
    assert snapshot["last_seq"] == streamed(demo, workflow_id)[-1].seq


def test_a_workflow_with_no_events_streams_nothing_and_says_so_quietly(
    client: TestClient, demo: Demo
) -> None:
    """No events is not an error, and the response is still a stream a client can hold open."""
    workflow_id = demo.seed()
    response = demo.events(workflow_id)
    assert response.status_code == 200
    assert response.text == ""


def test_a_held_reconnect_does_not_send_events_before_its_cursor(
    settings: GatewaySettings, clock: ManualClock, control: SeedableControlState
) -> None:
    app = create_app(
        settings=profile_settings(settings, stream_hold_ms=100), clock=clock, control=control
    )
    with TestClient(app) as http:
        driver = Demo(http)
        workflow_id = moved_once(driver)
        snapshot = driver.snapshot(workflow_id).json()
        response = driver.events(workflow_id, last_event_id=snapshot["last_event_id"])
        assert not [frame for frame in frames(response.text) if not frame.comment]


def test_a_terminal_beyond_the_first_replay_batch_is_delivered(
    settings: GatewaySettings, clock: ManualClock, control: SeedableControlState
) -> None:
    app = create_app(
        settings=profile_settings(settings, stream_hold_ms=100, stream_replay_limit=2),
        clock=clock,
        control=control,
    )
    with TestClient(app) as http:
        driver = Demo(http)
        workflow_id = driver.seed()
        assert (
            driver.message(
                workflow_id, text="I cannot breathe and I need help now", version=1
            ).status_code
            == 200
        )
        delivered = streamed(driver, workflow_id)
        assert delivered[-1].type == "terminal"
        assert [frame.seq for frame in delivered] == list(range(len(delivered)))


def test_snapshot_keeps_a_display_only_handoff_card(demo: Demo) -> None:
    workflow_id = demo.seed()
    assert (
        demo.message(
            workflow_id, text="I cannot breathe and I need help now", version=1
        ).status_code
        == 200
    )
    snapshot = demo.snapshot(workflow_id).json()
    assert snapshot["components"][0]["component"] == "HumanHandoffCard"
    assert snapshot["surface_id"]
    assert snapshot["terminal"]["type"] == "terminal"


def test_the_replay_is_bounded_by_the_configured_limit(
    settings: GatewaySettings, clock: ManualClock, control: SeedableControlState
) -> None:
    """A replay window is a window. An unbounded replay is a response that grows with the workflow."""
    app = create_app(
        settings=profile_settings(settings, stream_replay_limit=2), clock=clock, control=control
    )
    with TestClient(app) as bounded_client:
        demo = Demo(bounded_client)
        workflow_id = moved_once(demo)
        assert len(streamed(demo, workflow_id)) == 2


def test_a_terminal_event_closes_the_stream_even_while_the_hold_is_running(
    settings: GatewaySettings, clock: ManualClock, control: SeedableControlState
) -> None:
    """Once the outcome has arrived there is no fifth act, so the connection is not held.

    The hold is set far above the assertion's threshold on purpose: if a terminal event stopped
    closing the stream, the request would block for the hold and the elapsed-time assertion would
    fail. That is what makes this a test of the rule rather than of the timeout's arithmetic.
    """
    hold_ms = 5000
    app = create_app(
        settings=profile_settings(settings, stream_hold_ms=hold_ms), clock=clock, control=control
    )
    with TestClient(app) as held_client:
        demo = Demo(held_client)
        workflow_id = demo.seed()
        # An emergency is answered, escalated and terminated: the one path the shipped profile can
        # drive to a terminal event without a model behind the guardrail seam.
        message = demo.message(
            workflow_id,
            text="I cannot breathe and I need help now",
            version=1,
        )
        assert message.status_code == 200, message.text
        assert message.json()["routed_to"] == "HUMAN_HANDOFF"

        started = time.monotonic()
        stream = streamed(demo, workflow_id)
        elapsed_ms = (time.monotonic() - started) * 1000
        assert [frame.type for frame in stream][-1] == "terminal"
        assert elapsed_ms < hold_ms / 2, (
            f"the stream was held for {elapsed_ms:.0f} ms with a terminal event already on it"
        )


def test_an_idle_stream_is_kept_alive_rather_than_closed(
    settings: GatewaySettings, clock: ManualClock, control: SeedableControlState
) -> None:
    """A stream with nothing new on it still emits a comment frame, so an intermediary proxy sees
    traffic instead of reaping an idle connection.

    `EventSource` ignores the comment, which is why it is a comment rather than an event: a synthetic
    event here would be delivered to the client's own message handler and would have to be filtered
    there. The heartbeat interval is set below the hold so at least one is due while the stream is
    open — the shipped 15 s / 25 s pair would make this test sleep.
    """
    from referraleasy_gateway.api import HEARTBEAT_FRAME

    app = create_app(
        settings=profile_settings(settings, stream_hold_ms=250, stream_heartbeat_ms=50),
        clock=clock,
        control=control,
    )
    with TestClient(app) as held_client:
        demo = Demo(held_client)
        # A move makes the workflow *not* terminal but leaves the log settled, so the hold runs with
        # nothing to send and the heartbeat is the only thing it can emit.
        workflow_id = moved_once(demo)
        response = demo.events(workflow_id)
        assert response.status_code == 200
        assert HEARTBEAT_FRAME in response.text
        # The replay still came first, and the heartbeat is not one of its frames.
        assert frames(response.text)[0].data is not None


def test_the_stream_is_scoped_to_the_patient(client: TestClient, demo: Demo) -> None:
    """Scope is checked before the response starts, so the 404 is a status code and not a frame.

    Once a frame is on the wire the status is spent, which is why `stream_events` loads the workflow
    before it returns the `StreamingResponse` at all.
    """
    workflow_id = moved_once(demo)
    other = client.post(
        "/v1/auth/patient-session",
        json={"subject": "patient-2", "patient_ref": "patient-2"},
    ).json()["access_token"]
    response = client.get(
        f"/v1/workflows/{workflow_id}/events",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_the_stream_refuses_an_unauthenticated_request_before_any_frame(
    client: TestClient, demo: Demo
) -> None:
    """No session, no stream — and a 401 with a challenge rather than an empty `text/event-stream`.

    The route is the one place a browser can reach with no header at all (`EventSource` cannot set
    one), so it is also the one place where "no credential" has to be refused explicitly instead of
    being left to a dependency that a future refactor could make optional.
    """
    workflow_id = demo.seed()
    response = client.get(f"/v1/workflows/{workflow_id}/events")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")
    assert response.json()["error"]["code"] == "unauthenticated"


def test_a_token_is_accepted_in_the_query_string_only_in_the_demo_profile(
    client: TestClient,
    demo: Demo,
    settings: GatewaySettings,
    clock: ManualClock,
    control: SeedableControlState,
) -> None:
    """`EventSource` cannot set a header, so the demo accepts `?token=`. The trade is *confined*.

    Asserted in both directions on purpose. A test that only showed the demo accepting the parameter
    would still pass if the production profile accepted it too — and it is the production profile
    where a token in a URL means a token in a proxy's access log.
    """
    workflow_id = moved_once(demo)
    response = demo.events(workflow_id, token=True)
    assert response.status_code == 200
    assert frames(response.text)

    from referraleasy_gateway.auth import JwtIssuer, SessionKind

    production = profile_settings(settings, app_env="production")
    app = create_app(settings=production, clock=clock, control=control)
    with TestClient(app) as production_client:
        token = JwtIssuer(
            production.patient_secret,
            kind=SessionKind.patient,
            audience=production.patient_audience,
            issuer=production.jwt_issuer,
            ttl_seconds=production.session_ttl_seconds,
            clock=clock,
        ).issue("patient-1", patient_ref="patient-1")
        # The demo seeding route does not exist here, so the row is inserted through the same store
        # the app was given.
        row = control.create("patient-1")
        refused = production_client.get(
            f"/v1/workflows/{row.workflow_id}/events", params={"token": token}
        )
        assert refused.status_code == 401
        # The same token over the header the browser cannot send is accepted, which is what makes the
        # refusal about the *parameter* rather than about the credential.
        assert (
            production_client.get(
                f"/v1/workflows/{row.workflow_id}/events",
                headers={"Authorization": f"Bearer {token}"},
            ).status_code
            == 200
        )


def test_the_terminal_frame_carries_the_final_latency_and_a_null_progress_reading(
    client: TestClient, demo: Demo
) -> None:
    """§12's split, as it appears on the wire: `progress_elapsed_ms` is null on the terminal event and
    the turn's own two numbers travel in its payload — one field each, and neither standing in for the
    other."""
    workflow_id = demo.seed()
    assert demo.message(workflow_id, text="I cannot breathe", version=1).status_code == 200
    terminal = next(frame for frame in streamed(demo, workflow_id) if frame.type == "terminal")
    assert terminal.data is not None
    assert terminal.data["progress_elapsed_ms"] is None
    assert isinstance(terminal.payload["final_elapsed_ms"], int)
    assert isinstance(terminal.payload["progress_elapsed_ms"], int)
    assert terminal.payload["final_elapsed_ms"] >= terminal.payload["progress_elapsed_ms"]
    assert terminal.payload["outcome"] == "needs_input"


def test_a_partial_event_is_not_authoritative(client: TestClient, demo: Demo) -> None:
    """`partial_text` is on the stream and is not state (§12).

    The stream is the *only* place a partial appears: the move answers 202, the workflow has not
    moved, and every frame the turn produced is classified as non-authoritative. A reader folding the
    log into state therefore has nothing to fold, whether or not it remembers the rule.
    """
    from referraleasy_gateway.events import AUTHORITATIVE_EVENT_TYPES

    workflow_id = demo.seed()
    partial = demo.move(
        workflow_id,
        version=1,
        to_state=WorkflowState.PROVIDER_MATCHING,
        complete=False,
        partial_text="Looking for hospitals near you…",
    )
    assert partial.status_code == 202
    stream = streamed(demo, workflow_id)
    assert [frame.type for frame in stream].count("partial_text") >= 1
    for frame in stream:
        assert frame.data is not None
        assert StreamEventType(frame.type) not in AUTHORITATIVE_EVENT_TYPES
    assert "workflow_state" not in [frame.type for frame in stream]
    before = demo.snapshot(workflow_id).json()
    assert (before["state"], before["state_version"]) == ("INTAKE", 1)
