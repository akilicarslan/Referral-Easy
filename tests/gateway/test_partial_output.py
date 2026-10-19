# ruff: noqa: RUF002  # Turkish quoted from §12 of ReferralEasy_Architecture_v3.md is kept verbatim
"""Partial agent output is never final state (§12).

§12: *"Kısmi agent çıktısı asla final state olarak commit edilmez."* The sentence is a prohibition, and
the only way to keep a prohibition is to make the illegal thing unrepresentable rather than
discouraged. So the rule lives in two values, and this module tests both doors:

* `PartialBuffer.commit()` refuses while the producer has not signalled completion — a buffer that is
  still filling cannot be read as a result, and its `text` is readable for *display* the whole time,
  which is the distinction the buffer exists to make;
* `AgentDecision.commit()` refuses a decision with `complete=False`.

The gateway then calls `commit` on an incomplete decision **on purpose** and turns the refusal into a
notice, so the write path cannot answer `202` for something it committed: if `commit` ever stopped
refusing, that call site raises instead of lying. That last property is asserted here by replacing the
value with one that lies, because a guard nobody can trigger is a comment.

Over the wire, the turn's shape is pinned too: the text arrives as `partial_text` events, the refusal
travels as a `notice`, and nothing authoritative is emitted — no `workflow_state`, no `surface_update`
— so a client folding the stream's authoritative events sees a workflow that did not move.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gateway_fixtures import Demo

from referraleasy_contracts.generated.common import WorkflowState
from referraleasy_gateway.events import AUTHORITATIVE_EVENT_TYPES
from referraleasy_gateway.surfaces import AgentDecision, PartialBuffer, PartialOutputError

STREAMED = "I am still working through which slots fit the patient's second appointment."


def _frames(response: Any) -> list[dict[str, Any]]:
    return [
        json.loads(line[len("data: ") :])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


# ---------------------------------------------------------------------------
# The values
# ---------------------------------------------------------------------------


def test_a_buffer_that_is_still_filling_cannot_be_committed() -> None:
    """The buffer's whole purpose: "still streaming" is a state the code can be *in*.

    Without this, the rule is a convention about not reading a string too early, and the convention is
    exactly what a fast path at 2 a.m. skips.
    """
    buffer = PartialBuffer()
    buffer.add("The patient ")
    buffer.add("needs a cardiologist")
    assert buffer.text == "The patient needs a cardiologist", (
        "displaying the text so far is the point — the patient watches it arrive"
    )

    with pytest.raises(PartialOutputError) as refusal:
        buffer.commit()
    assert "still streaming" in str(refusal.value)
    assert "never committed as final state" in str(refusal.value)

    buffer.finish()
    assert buffer.commit() == "The patient needs a cardiologist"


def test_a_decision_that_is_still_streaming_cannot_be_committed() -> None:
    """`complete=False` is a shape a streaming decision has, and `commit` is what refuses it."""
    streaming = AgentDecision(
        complete=False, to_state=WorkflowState.PROVIDER_MATCHING.value, text=STREAMED
    )
    with pytest.raises(PartialOutputError) as refusal:
        streaming.commit()
    assert "still streaming" in str(refusal.value)
    assert "§12" in str(refusal.value)

    finished = AgentDecision(complete=True, to_state=WorkflowState.PROVIDER_MATCHING.value).commit()
    assert finished.to_state == "PROVIDER_MATCHING"


def test_the_refusal_is_not_an_http_refusal() -> None:
    """`PartialOutputError` is a `RuntimeError`, and it is not in the refusal mapping.

    That is deliberate and it is checkable: a guard that could be answered to a client would become
    another status code for the client to handle, and the honest answer to "our own value was not
    committed" is that something on our side is broken.
    """
    from referraleasy_gateway.errors import REFUSAL_STATUS

    assert issubclass(PartialOutputError, RuntimeError)
    assert not any(issubclass(PartialOutputError, handled) for handled in REFUSAL_STATUS), (
        "a partial-output refusal must never be reachable as a client-visible status"
    )


# ---------------------------------------------------------------------------
# Over the wire
# ---------------------------------------------------------------------------


def test_a_partial_transition_answers_202_and_changes_nothing(
    client: TestClient, demo: Demo
) -> None:
    """The transport contract of a streaming decision: accepted, not applied.

    `202` rather than `200` is the honest status — the request was fine, the work is not done — and
    the body says so with `committed: false` rather than leaving the client to infer it from a state
    that happens not to have moved.
    """
    workflow_id = demo.seed()
    response = demo.move(
        workflow_id,
        version=1,
        to_state=WorkflowState.PROVIDER_MATCHING,
        complete=False,
        partial_text=STREAMED,
    )
    assert response.status_code == 202, response.text
    assert response.json()["committed"] is False
    assert response.json()["reason"] == "partial_output"
    assert response.json()["state"] == "INTAKE"
    assert response.json()["state_version"] == 1

    snapshot = demo.snapshot(workflow_id).json()
    assert (snapshot["state"], snapshot["state_version"]) == ("INTAKE", 1)
    assert snapshot["components"] == []
    assert demo.admin_view(workflow_id).json()["workflow"]["payload"] == {}


def test_the_partial_text_arrives_as_partial_events_and_nothing_authoritative_follows(
    client: TestClient, demo: Demo
) -> None:
    """What a client sees, event by event: progress, the text, and the refusal.

    The assertion that carries the weight is the negative one — no frame of an authoritative type was
    emitted — because a client that folds `committed_events` must be able to keep folding while text
    is still arriving. The positive assertions are there so the negative one is not vacuous: the turn
    really did stream, so there really was something it might have committed.
    """
    workflow_id = demo.seed()
    moved = demo.move(
        workflow_id,
        version=1,
        to_state=WorkflowState.PROVIDER_MATCHING,
        complete=False,
        partial_text=STREAMED,
    )
    assert moved.status_code == 202, moved.text

    frames = _frames(demo.events(workflow_id))
    kinds = [frame["type"] for frame in frames]
    assert kinds[:2] == ["agent_progress", "notice"], kinds
    assert set(kinds[2:]) == {"partial_text"}, kinds
    assert len(kinds) > 3, "the text arrived in more than one piece, which is what streaming means"
    assert not [frame for frame in frames if frame["type"] in AUTHORITATIVE_EVENT_TYPES]

    notice = frames[1]["payload"]
    assert notice["level"] == "warning"
    assert "forbids committing partial agent output as final state" in notice["text"]
    assert all(
        frame["payload"]["complete"] is False for frame in frames if frame["type"] == "partial_text"
    ), "partial output that claimed to be complete would be final state by another name"
    # Progress is `agent_progress` and the text itself; a `notice` is information and carries no
    # reading, so a sentence about the refusal does not become the turn's progress latency.
    assert frames[0]["progress_elapsed_ms"] is not None
    assert frames[1]["progress_elapsed_ms"] is None
    assert all(
        frame["progress_elapsed_ms"] is not None
        for frame in frames
        if frame["type"] == "partial_text"
    ), "the turn reports progress while it streams, which is the whole point of §12's split"
    streamed = "".join(
        frame["payload"]["text"] for frame in frames if frame["type"] == "partial_text"
    )
    assert streamed == STREAMED


def test_the_partial_is_not_the_workflow_and_the_retry_that_completes_it_is_a_fresh_command(
    client: TestClient, demo: Demo
) -> None:
    """The practical consequence: finishing the same work is a new command with new text, and the
    workflow moves exactly once.

    A client that reuses the partial's `command_id` is refused as a command conflict (the body differs),
    which is what stops a retry from being a silent second move — so the honest retry is the one this
    test performs, and the state after it is what the partial never was.
    """
    workflow_id = demo.seed()
    partial = demo.move(
        workflow_id,
        version=1,
        to_state=WorkflowState.PROVIDER_MATCHING,
        complete=False,
        partial_text=STREAMED,
        command_id="coordinator-decision",
    )
    assert partial.status_code == 202, partial.text

    reused = demo.move(
        workflow_id,
        version=1,
        to_state=WorkflowState.PROVIDER_MATCHING,
        complete=True,
        command_id="coordinator-decision",
    )
    assert reused.status_code == 409, reused.text
    assert reused.json()["error"]["sqlstate"] == "RE005"

    finished = demo.move(
        workflow_id,
        version=1,
        to_state=WorkflowState.PROVIDER_MATCHING,
        complete=True,
        props={"matched": True},
        command_id="coordinator-decision-final",
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["committed"] is True
    assert demo.snapshot(workflow_id).json()["state_version"] == 2


def test_a_value_that_stopped_refusing_partial_output_fails_loudly(
    client: TestClient, demo: Demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard at the call site, triggered by a `commit` that does not guard.

    `_refuse_partial` calls `commit()` on an incomplete decision *on purpose*. The `else` branch is
    unreachable while the value is correct, which is exactly why it is worth pinning: this test
    replaces the value with a lying one and asserts the service raises rather than answering `202`
    for a decision it committed. In production that is a 500 — the right outcome, and not a 202.
    """

    class LyingDecision(AgentDecision):
        def commit(self) -> AgentDecision:  # type: ignore[override]
            return AgentDecision(
                complete=True, to_state=self.to_state, props=self.props, text=self.text
            )

    monkeypatch.setattr("referraleasy_gateway.commands.AgentDecision", LyingDecision)
    workflow_id = demo.seed()
    with pytest.raises(AssertionError) as escape:
        demo.move(
            workflow_id,
            version=1,
            to_state=WorkflowState.PROVIDER_MATCHING,
            complete=False,
            partial_text=STREAMED,
        )
    assert "§12's rule is no longer enforced by the value" in str(escape.value)
    # And the workflow did not move, so the failing guard did not become a move either.
    assert demo.snapshot(workflow_id).json()["state_version"] == 1
