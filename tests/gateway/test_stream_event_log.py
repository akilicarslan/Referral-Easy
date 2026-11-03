"""§12's event log, shared between gateway replicas (migration 018, `PostgresEventLog`).

The memory-profile tests pin the two read methods the stream now relies on (`seq_of`,
`has_terminal(through_seq=)`). The `integration` tests are the point of the change: two
`PostgresEventLog` instances stand in for two gateway replicas over one database, and two full
gateway apps serve one workflow's stream. They need `TEST_DATABASE_URL`.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from uuid import UUID

import gateway_fixtures as fx
import pytest
from fastapi.testclient import TestClient

from referraleasy_contracts.generated.common import WorkflowState
from referraleasy_contracts.generated.sse import StreamEventType
from referraleasy_gateway.app import create_app
from referraleasy_gateway.control import PostgresControlState
from referraleasy_gateway.events import EventLog, PostgresEventLog

NOW = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)


def test_seq_of_and_terminal_through_a_cursor() -> None:
    log = EventLog(synthetic=True)
    workflow = UUID("00000000-0000-4000-8000-000000000001")
    first = log.append(workflow, StreamEventType.notice, {"text": "a"}, created_at=NOW)
    log.append(workflow, StreamEventType.terminal, {"outcome": "x"}, created_at=NOW)
    assert log.seq_of(workflow, first.event_id) == 0
    assert log.seq_of(workflow, "f" * 32) is None
    assert log.has_terminal(workflow, through_seq=0) is False
    assert log.has_terminal(workflow, through_seq=1) is True


# ---------------------------------------------------------------------------
# Two replicas, one database
# ---------------------------------------------------------------------------

needs_database = pytest.mark.skipif(
    not fx.TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set; the shared log is a table"
)


def _workflow() -> UUID:
    assert fx.TEST_DATABASE_URL
    fx.reset_database(fx.TEST_DATABASE_URL)
    control = PostgresControlState(fx.TEST_DATABASE_URL, worker_id=fx.TEST_WORKER_ID)
    return control.create(fx.PATIENT_REF).workflow_id


@pytest.mark.integration
@needs_database
def test_two_replicas_append_one_contiguous_sequence_and_replay_it_identically() -> None:
    assert fx.TEST_DATABASE_URL
    workflow = _workflow()
    a = PostgresEventLog(fx.TEST_DATABASE_URL, synthetic=True)
    b = PostgresEventLog(fx.TEST_DATABASE_URL, synthetic=True)
    appended = [
        (a if i % 2 == 0 else b).append(
            workflow, StreamEventType.notice, {"text": str(i)}, created_at=NOW
        )
        for i in range(6)
    ]
    assert [event.seq for event in appended] == list(range(6))
    assert a.events_for(workflow) == b.events_for(workflow) == tuple(appended)
    # A cursor minted on one replica resumes on the other.
    assert b.replay(workflow, after_event_id=appended[2].event_id) == tuple(appended[3:])
    # An unknown cursor replays from the beginning, as the in-memory log does.
    assert a.replay(workflow, after_event_id="0" * 32) == tuple(appended)


@pytest.mark.integration
@needs_database
def test_concurrent_appends_from_two_replicas_never_share_or_skip_a_seq() -> None:
    assert fx.TEST_DATABASE_URL
    workflow = _workflow()
    replicas = [PostgresEventLog(fx.TEST_DATABASE_URL, synthetic=True) for _ in range(2)]

    def write(log: PostgresEventLog, tag: str) -> None:
        for i in range(10):
            log.append(workflow, StreamEventType.notice, {"text": f"{tag}{i}"}, created_at=NOW)

    threads = [threading.Thread(target=write, args=(log, str(n))) for n, log in enumerate(replicas)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [event.seq for event in replicas[0].events_for(workflow)] == list(range(20))


@pytest.mark.integration
@needs_database
def test_a_stream_opened_on_one_gateway_carries_what_another_gateway_appended() -> None:
    assert fx.TEST_DATABASE_URL
    fx.reset_database(fx.TEST_DATABASE_URL)
    settings = fx.demo_settings(
        GATEWAY_CONTROL_BACKEND="postgres", DATABASE_URL=fx.TEST_DATABASE_URL
    )

    def gateway() -> TestClient:
        control = PostgresControlState(fx.TEST_DATABASE_URL or "", worker_id=fx.TEST_WORKER_ID)
        return TestClient(create_app(settings=settings, control=control))

    with gateway() as first, gateway() as second:
        demo = fx.Demo(first)
        workflow_id = demo.seed()
        moved = demo.move(workflow_id, version=1, to_state=WorkflowState.PROVIDER_MATCHING)
        assert moved.status_code == 200, moved.text  # the append happens on the first replica
        path = f"/v1/workflows/{workflow_id}/events"
        on_first = first.get(path, headers=demo.patient).text
        on_second = second.get(path, headers=demo.patient).text
    assert "data:" in on_first and on_first == on_second
