"""The outbox worker: one attempt per row, the row owns the retry, a bounce is not a booking (§7, §11).

§7 asks for an outbox worker and names five behaviours. Each has a test below, and each test fails if
its behaviour is deleted:

* **invariant #11 — a failed notification never rolls back a booking.** Structural version: the
  enqueue shares the booking's transaction and the delivery does not, so the failing write is the
  *row*, and the booking is already committed and visible on another connection by the time the
  provider refuses. Symptom version: the appointment's record is untouched, the workflow does not move
  into a failure state, and no ticket appears in the human queue because a message bounced.
* **a permanently failing notification is buried.** The row reaches `dead`, which the schema calls
  terminal, and no later pass claims it again. `attempt_count < max_attempts` in `outbox_claim` is the
  loop's stop condition, so the test asserts the queue's own rule rather than a Python flag.
* **a valid empty answer is not retried.** The provider saying "there was nothing to deliver" (and the
  provider saying "there is no such booking") are answers. They settle as `delivered` with a NULL
  `last_error` — the table's own constraint, not this package's preference — and the row is never
  claimed again.
* **`Retry-After` wins over our curve.** The configured base is a floor: `max(base, ceil(retry_after))`,
  and the pass counts how often it was raised.
* **a lease that expires mid-delivery leaves the outcome to the new owner.** `RE007` from
  `outbox_settle`, produced with a hijacking sender on a **real second connection** while this worker
  is mid-delivery, and the row is left settle-able by the new owner instead of wedged.

Two more things are asserted because they are how the first four stay true. A pass makes *exactly one*
attempt per row (a second attempt in-process would multiply the budget the row is enforcing), and
`max_batches` really bounds one unit of work.

The tests never sleep: "a later pass, after the backoff" is expressed by pushing `next_attempt_at` into
the past, exactly as the suite's `expire_lease` does for a workflow lease.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from worker_doubles import (
    SPEC_CAPS,
    ScriptedSender,
    delivered,
    delivery_failure,
    nothing_to_deliver,
)

from referraleasy_domain import OutboxClaimError, OutboxMessage, OutboxStatus, OutboxStore
from referraleasy_workers import outbox as outbox_module
from referraleasy_workers.outbox import (
    DeliveryFailure,
    DeliveryOutcome,
    HttpNotificationSender,
    OutboxWorker,
)
from referraleasy_workers.settings import WorkerSettings

#: This worker and the other one. Constants because the assertions are about *whose* claim stands.
WORKER = "w-outbox"
OTHER_WORKER = "w-hijack"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_worker(
    conn: Any, sender: Any, settings: WorkerSettings, *, worker_id: str = WORKER
) -> Any:
    return OutboxWorker(conn, worker_id=worker_id, sender=sender, settings=settings)


def run_once(worker: Any, **kwargs: Any) -> Any:
    """One drain pass. The worker is async because its sender is; the store is not."""
    return asyncio.run(worker.run_once(**kwargs))


def outbox_row(conn: Any, outbox_id: Any) -> Mapping[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM outbox WHERE id = %s", (outbox_id,))
        return cur.fetchone()


def make_due(conn: Any, outbox_id: Any) -> None:
    """Push a row's backoff into the past, so the next pass sees it as due.

    A row edit rather than a sleep, for the same reason `test_ownership.py` edits a lease: the suite
    does not wait on wall-clock time, and *how* a backoff elapses is not what is under test — that the
    next claim notices it is. The pass between the two edits is what the test is about.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE outbox SET next_attempt_at = now() - interval '1 second' WHERE id = %s",
            (outbox_id,),
        )


def message(
    *, dedupe_key: str = "notify-1", payload: Mapping[str, Any] | None = None
) -> OutboxMessage:
    """One claimed row, for the tests that drive the sender directly rather than through the store."""
    now = datetime.now(UTC)
    return OutboxMessage(
        id=uuid4(),
        dedupe_key=dedupe_key,
        kind="notify_patient",
        payload=payload if payload is not None else {"booking_request_id": "B-1"},
        status=OutboxStatus.CLAIMED,
        attempt_count=1,
        max_attempts=3,
        next_attempt_at=now,
        claimed_by=WORKER,
        claimed_until=now,
        last_error=None,
        created_at=now,
        updated_at=now,
        delivered_at=None,
        terminal_at=None,
    )


def booking_is_visible(conn: Any, request_id: Any) -> Mapping[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM booking_request WHERE id = %s", (request_id,))
        return cur.fetchone()


class WatchingSender:
    """A sender that records what it was handed and what it could see, then fails.

    It records rather than asserts: `OutboxWorker._attempt` catches *every* exception a sender raises,
    so an `assert` in here would be silently converted into "the delivery failed" and a test would pass
    while proving nothing. The recorded facts are asserted after the pass, in the test's own code.
    """

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure or DeliveryFailure("the provider refused", status_code=502)
        self.seen: dict[str, Any] = {}

    async def send(self, outbox_message: OutboxMessage) -> Any:
        self.seen["dedupe_key"] = outbox_message.dedupe_key
        self.seen["payload"] = dict(outbox_message.payload)
        raise self.failure


# ---------------------------------------------------------------------------
# One attempt per pass, and the row owns the retry
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a_pass_makes_exactly_one_attempt_per_row_and_the_backoff_doubles(
    db: Any, seed: Any, sender: ScriptedSender, settings: WorkerSettings
) -> None:
    """The retry state is the row's: one attempt per pass, an exponential backoff between passes.

    Three failures are scripted and each pass makes exactly one request — not three. That is the
    difference between the row's budget and a worker-side retry loop: three passes with a doubling
    backoff spend three requests and three attempts, while a loop would spend nine requests for three
    attempts, and the row's `attempt_count` would be a fiction.

    The backoff is read off the row as `next_attempt_at - updated_at`, which the settle computes from
    one `now()`: attempt 1 is pushed `base` seconds, attempt 2 twice that. Every timestamp asserted
    here is the database's, so a Python-side backoff that disagreed with `outbox_settle`'s would show
    up as a difference rather than as a passing test.
    """
    outbox_id = seed.outbox(dedupe_key="notify-1", max_attempts=3)
    db.commit()
    base = settings.outbox_backoff_base_seconds
    sender.scripted(
        delivery_failure(status=500), delivery_failure(status=500), delivery_failure(status=500)
    )

    for attempt in (1, 2):
        report = run_once(make_worker(db, sender, settings))
        db.commit()

        assert report.report.claimed == 1
        assert sender.deliveries == attempt, "one request per pass, whatever the row has left"
        row = outbox_row(db, outbox_id)
        assert row["attempt_count"] == attempt
        assert row["status"] == OutboxStatus.PENDING
        assert row["last_error"] is not None
        assert row["next_attempt_at"] - row["updated_at"] == timedelta(
            seconds=base * 2 ** (attempt - 1)
        ), "the backoff doubles per attempt, starting at the configured base"

        make_due(db, outbox_id)
        db.commit()

    report = run_once(make_worker(db, sender, settings))
    db.commit()

    assert sender.deliveries == 3, "the budget is spent, and it was spent one request at a time"
    assert report.report.dead == 1


@pytest.mark.integration
def test_a_permanently_failing_notification_is_buried_and_never_attempted_again(
    db: Any, seed: Any, sender: ScriptedSender, settings: WorkerSettings
) -> None:
    """`dead` is terminal, and the queue is what makes it terminal.

    Two attempts against a budget of two, the second one failing, and the row stops. The next pass
    claims nothing at all — `outbox_claim` excludes spent rows in its own `WHERE`, so "never retried" is
    a property of the claim rather than a flag this worker remembers. `terminal_at` is set, and
    `next_attempt_at` is left exactly where the claim found it: a buried row has no next time, and a
    worker that wrote one would leave a queue that always has something due in it.
    """
    outbox_id = seed.outbox(dedupe_key="notify-1", max_attempts=2)
    db.commit()
    sender.scripted(delivery_failure(status=503), delivery_failure(status=503))

    first = run_once(make_worker(db, sender, settings))
    db.commit()
    assert first.report.retried == 1
    assert outbox_row(db, outbox_id)["status"] == OutboxStatus.PENDING

    make_due(db, outbox_id)
    db.commit()
    due_at_claim = outbox_row(db, outbox_id)["next_attempt_at"]

    second = run_once(make_worker(db, sender, settings))
    db.commit()

    row = outbox_row(db, outbox_id)
    assert second.report.dead == 1
    assert row["status"] == OutboxStatus.DEAD
    assert row["attempt_count"] == row["max_attempts"] == 2
    assert row["terminal_at"] is not None
    assert row["last_error"] is not None
    assert row["next_attempt_at"] == due_at_claim, (
        "death leaves the schedule where the claim found it"
    )

    after = run_once(make_worker(db, sender, settings))
    db.commit()

    assert after.nothing_to_do is True, "an exhausted row is never even a candidate"
    assert sender.deliveries == 2, "no third request, ever"
    assert after.report.dead == 0
    assert after.settlements == ()

    buried = OutboxMessage.from_row(row)
    assert buried.is_terminal is True
    assert buried.attempts_remaining == 0


@pytest.mark.integration
def test_a_valid_empty_result_is_delivered_and_never_retried(
    db: Any, seed: Any, settings: WorkerSettings
) -> None:
    """§7: "nothing to deliver" is a success, and the row may not be asked again.

    Charging the budget for a question the provider has already answered is the failure mode: a worker
    that read `EMPTY` as a failure would retry until the row was buried, and an operator would then have
    to work out that nothing had ever been wrong. The row settles `delivered` with a NULL `last_error` —
    `outbox_last_error_when_unhappy` refuses anything else — and the reason survives in the pass report
    instead, because a `delivered` row cannot carry one.
    """
    sender = ScriptedSender([nothing_to_deliver("the hospital has no patient contact on file")])
    outbox_id = seed.outbox(dedupe_key="notify-1")
    db.commit()

    report = run_once(make_worker(db, sender, settings))
    db.commit()

    row = outbox_row(db, outbox_id)
    assert row["status"] == OutboxStatus.DELIVERED
    assert row["last_error"] is None, "the table refuses a last_error on a delivered row"
    assert row["delivered_at"] is not None
    assert row["terminal_at"] is not None
    assert report.report.delivered == 1
    assert report.report.retried == 0
    assert [empty.id for empty in report.empty_results] == [row["id"]]
    assert any("nothing to deliver" in note for note in report.notes), (
        "the reason survives in the report"
    )
    assert [terminal.id for terminal in report.terminal] == [row["id"]]

    make_due(db, outbox_id)
    db.commit()
    after = run_once(make_worker(db, sender, settings))
    db.commit()

    assert after.nothing_to_do is True
    assert sender.deliveries == 1, "an answered question is not asked again"


@pytest.mark.integration
def test_a_pass_is_bounded_by_the_batch_ceiling(
    db: Any, seed: Any, settings: WorkerSettings
) -> None:
    """`max_batches` bounds one unit of work, which is what lets one process share itself.

    Five rows, a batch of one, a ceiling of two: two claims, two deliveries, three rows still due. A
    ceiling that only stopped *after* the next batch — or one the store ignored — would take the whole
    queue, holding its row locks for as long as the deliveries took.
    """
    sender = ScriptedSender([delivered() for _ in range(5)])
    for index in range(5):
        seed.outbox(dedupe_key=f"notify-{index}")
    db.commit()

    report = run_once(make_worker(db, sender, settings), limit=1, max_batches=2)
    db.commit()

    assert report.report.batches == 2
    assert report.report.claimed == 2
    assert sender.deliveries == 2

    rest = run_once(make_worker(db, sender, settings))
    db.commit()
    assert rest.report.claimed == 3, "the rest of the queue was still there to be worked"


@pytest.mark.integration
def test_an_empty_queue_is_the_healthy_steady_state(
    db: Any, sender: ScriptedSender, settings: WorkerSettings
) -> None:
    """Nothing due is a normal answer, not an error: the poll loop must be allowed to find nothing."""
    report = run_once(make_worker(db, sender, settings))

    assert report.nothing_to_do is True
    assert report.report.batches == 0
    assert report.report.claimed == 0
    assert report.report.settled == 0
    assert report.settlements == () and report.notes == ()
    assert sender.deliveries == 0


# ---------------------------------------------------------------------------
# Retry-After (§7: the far side's delay wins over ours)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    ("retry_after_s", "expected_delay_s", "honoured"),
    [
        (90.0, 90, True),  # the provider is the authority for its own window
        (5.5, 6, True),  # rounded up: half a second short of the window is still inside it
        (5.0, 5, False),  # equal to our base: nothing to raise
        (2.5, 5, False),  # ours is longer; the row keeps our backoff
    ],
)
def test_retry_after_is_a_floor_under_our_curve(
    db: Any,
    seed: Any,
    settings: WorkerSettings,
    retry_after_s: float,
    expected_delay_s: int,
    honoured: bool,
) -> None:
    """`max(configured_base, ceil(retry_after))`, read off the row's own columns.

    Both directions are pinned, because the wrong half is the easy bug: a worker that always used the
    provider's number would shorten its own backoff when the provider asked for less than the base, and
    one that always used its own would ignore a 429 that named a window. `retry_after_honoured` counts
    the raises, so it is 1 exactly when the far side moved the delay — the report and the row have to
    agree about which happened.
    """
    sender = ScriptedSender([delivery_failure(status=429, retry_after_s=retry_after_s)])
    outbox_id = seed.outbox(dedupe_key="notify-1")
    db.commit()

    report = run_once(make_worker(db, sender, settings))
    db.commit()

    row = outbox_row(db, outbox_id)
    delay = row["next_attempt_at"] - row["updated_at"]
    assert delay == timedelta(seconds=expected_delay_s)
    assert report.retry_after_honoured == (1 if honoured else 0)
    assert row["status"] == OutboxStatus.PENDING
    assert row["attempt_count"] == 1


# ---------------------------------------------------------------------------
# Two workers, one row (§7: `RE007`)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a_lease_that_expires_mid_delivery_leaves_the_outcome_to_the_new_owner(
    db: Any, db2: Any, seed: Any, settings: WorkerSettings
) -> None:
    """The `RE007` case, staged while this worker is inside the sender — on a **real second connection**.

    The row's lease lapses during the delivery: the sender commits this worker's own claim (so the row
    is visible at all), pushes `claimed_until` into the past on `db2`, and lets the other worker claim
    it. By the time this worker settles, the row is not its to settle, and `outbox_settle` refuses with
    `RE007`. The pass counts it as `lost` and touches nothing.

    The settle runs inside a savepoint (`OutboxWorker.run_once`), which is why the refusal does not
    poison the pass's transaction: a lost lease is reported *and* recovered from, rather than reported
    by a worker that then cannot run its next statement. Finally the new owner settles the row itself —
    `lost` is a handover, not a wedge.
    """
    outbox_id = seed.outbox(dedupe_key="notify-1", max_attempts=5)
    db.commit()
    stolen: dict[str, Any] = {}

    class HijackingSender:
        async def send(self, outbox_message: OutboxMessage) -> Any:
            with db.cursor() as cur:
                cur.execute("SELECT claimed_by FROM outbox WHERE id = %s", (outbox_message.id,))
                stolen["claimed_by_this_worker"] = cur.fetchone()["claimed_by"]
            db.commit()  # the claim becomes visible, and its row lock is released with it
            with db2.cursor() as cur:
                cur.execute(
                    "UPDATE outbox SET claimed_until = now() - interval '1 second' WHERE id = %s",
                    (outbox_message.id,),
                )
            claimed = OutboxStore(db2).claim(OTHER_WORKER, limit=10)
            db2.commit()
            stolen["taken"] = [str(row.id) for row in claimed]
            stolen["claimed_by_other"] = [row.claimed_by for row in claimed]
            raise DeliveryFailure("the provider refused mid-delivery", status_code=502)

    report = run_once(make_worker(db, HijackingSender(), settings))
    db.commit()

    assert stolen["claimed_by_this_worker"] == WORKER
    assert stolen["taken"] == [str(outbox_id)]
    assert stolen["claimed_by_other"] == [OTHER_WORKER]

    assert report.report.lost == 1
    assert report.report.retried == 0 and report.report.delivered == 0
    assert report.settlements == (), "a lost lease records nothing, not even a failure"
    assert any("lease lost" in note for note in report.notes)

    row = outbox_row(db2, outbox_id)
    assert row["claimed_by"] == OTHER_WORKER
    assert row["status"] == OutboxStatus.CLAIMED
    assert row["last_error"] is None, "this worker's failure text was never written"
    assert row["attempt_count"] == 2, "a claim counts as an attempt; the schema says so"

    settled = OutboxStore(db2).settle(outbox_id, OTHER_WORKER, ok=True)
    db2.commit()
    assert settled.status == OutboxStatus.DELIVERED
    assert settled.delivered_at is not None


@pytest.mark.integration
def test_a_worker_that_died_mid_delivery_does_not_wedge_the_row(
    db: Any, db2: Any, seed: Any
) -> None:
    """A claim is a lease, not a lock held forever: a crashed worker's row becomes claimable again.

    No sender and no pass here — just two claims and a settle, so the assertion is about the row's own
    rule. While the lease is live the other worker gets nothing (that is what stops a double delivery);
    once it has passed, the same claim succeeds and the dead worker's late settle is refused with
    `RE007`. Without this, one process dying mid-delivery would park a patient's notification for good.
    """
    outbox_id = seed.outbox(dedupe_key="notify-1", max_attempts=5)
    db.commit()

    mine = OutboxStore(db).claim(WORKER, limit=10, lease_seconds=60)
    db.commit()
    assert [str(row.id) for row in mine] == [str(outbox_id)]

    assert OutboxStore(db2).claim(OTHER_WORKER, limit=10) == [], "a live lease is respected"

    with db2.cursor() as cur:
        cur.execute(
            "UPDATE outbox SET claimed_until = now() - interval '1 second' WHERE id = %s",
            (outbox_id,),
        )
    theirs = OutboxStore(db2).claim(OTHER_WORKER, limit=10)
    db2.commit()
    assert [str(row.id) for row in theirs] == [str(outbox_id)]
    assert theirs[0].attempt_count == 2

    with pytest.raises(OutboxClaimError):
        OutboxStore(db).settle(outbox_id, WORKER, ok=True)

    assert outbox_row(db2, outbox_id)["status"] == OutboxStatus.CLAIMED
    assert (
        OutboxStore(db2).settle(outbox_id, OTHER_WORKER, ok=True).status == OutboxStatus.DELIVERED
    )
    db2.commit()


# ---------------------------------------------------------------------------
# Invariant #11: a bounced notification is not a bounced booking
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a_failed_notification_never_rolls_back_the_booking(
    db: Any, db2: Any, seed: Any, settings: WorkerSettings
) -> None:
    """Invariant #11, as structure and as symptom.

    The enqueue is written in the booking's transaction and the delivery happens later, elsewhere: the
    sender below reads the booking through the *other* connection and finds it committed and visible
    while this worker's own transaction holds nothing but the outbox row. A failing delivery can
    therefore only roll back the outbox write — there is nothing else in its transaction to lose — and
    that separation is the whole mechanism behind "a failed notification must never roll back a
    booking".

    The symptom is asserted too, because the structure could be right and the workflow still wrong: the
    appointment request stands with its facility reference, the approval stays spent, the workflow does
    not move, and no ticket reaches the human queue. A 502 from the notification provider is not a
    reason for a person to be asked about this patient.
    """
    workflow, approval_id, _payload_hash = seed.in_flight()
    request_id = seed.booking_request(workflow.id, approval_id=approval_id, external_ref="B-1")
    outbox_id = seed.outbox(
        dedupe_key="notify-1", payload={"booking_request_id": "B-1", "channel": "email"}
    )
    before_booking = booking_is_visible(db, request_id)
    db.commit()

    class Watching(WatchingSender):
        async def send(self, outbox_message: OutboxMessage) -> Any:
            # What the *other* connection can see while this delivery is in flight: the queued row,
            # still `pending` — this worker's claim is uncommitted, which is the design (a batch is
            # settled before its transaction commits) — and the booking, long since committed.
            self.seen["row_on_another_connection"] = dict(outbox_row(db2, outbox_message.id))
            self.seen["booking_on_another_connection"] = booking_is_visible(db2, request_id)
            return await super().send(outbox_message)

    watcher = Watching(
        DeliveryFailure(
            "notification delivery answered 502 (notification_undeliverable); "
            "booking_still_committed=True — the appointment is unaffected",
            status_code=502,
        )
    )
    report = run_once(make_worker(db, watcher, settings))
    db.commit()

    assert watcher.seen["dedupe_key"] == "notify-1"
    assert watcher.seen["payload"] == {"booking_request_id": "B-1", "channel": "email"}
    assert watcher.seen["row_on_another_connection"]["status"] == OutboxStatus.PENDING
    assert watcher.seen["row_on_another_connection"]["attempt_count"] == 0
    assert watcher.seen["booking_on_another_connection"] is not None, "the booking committed first"
    assert watcher.seen["booking_on_another_connection"]["external_ref"] == "B-1"

    assert report.report.retried == 1
    assert report.report.delivered == 0
    assert any("booking_still_committed" in note for note in report.notes), (
        "the note reaches the row"
    )

    after = outbox_row(db, outbox_id)
    assert after["status"] == OutboxStatus.PENDING
    assert after["attempt_count"] == 1
    assert after["last_error"] is not None
    assert str(after["id"]) == str(outbox_id)

    with db2.cursor() as cur:
        cur.execute("SELECT consumed_at, consumed_by FROM approval WHERE id = %s", (approval_id,))
        approval = cur.fetchone()
        cur.execute(
            "SELECT state, state_version, human_owner_id FROM workflow WHERE id = %s",
            (workflow.id,),
        )
        current = cur.fetchone()
        cur.execute(
            "SELECT count(*) AS tickets FROM handoff WHERE workflow_id = %s", (workflow.id,)
        )
        tickets = cur.fetchone()["tickets"]

    assert tickets == 0, "a bounced message is not a reason to ask a human"
    assert approval["consumed_at"] is not None, "the booking's spend was not undone"
    assert current["state"] == "BOOKING_IN_FLIGHT"
    assert current["state_version"] == workflow.state_version
    assert current["human_owner_id"] is None
    assert booking_is_visible(db2, request_id) == before_booking, "the booking row is untouched"


# ---------------------------------------------------------------------------
# The HTTP sender's classification: what the retry decision is made of
# ---------------------------------------------------------------------------


def fake_response(
    status: int, body: Any = None, headers: Mapping[str, str] | None = None
) -> httpx.Response:
    """A response with a fresh body stream. Built per call: a reused `Response` is already consumed."""
    return httpx.Response(
        status, json=body if body is not None else {}, headers=dict(headers or {})
    )


def real_sender(
    handler: Any, *, base_url: str = "http://notify.invalid/"
) -> HttpNotificationSender:
    """The shipping sender over a mock transport. The shapes below are the mock API's own."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base_url)
    return HttpNotificationSender(base_url, caps=SPEC_CAPS, client=client)


def send(sender: HttpNotificationSender, outbox_message: OutboxMessage | None = None) -> Any:
    return asyncio.run(sender.send(outbox_message or message()))


def test_a_provider_refusal_is_a_failure_carrying_the_delay_and_the_booking_line() -> None:
    """The 502 the mock API actually returns, and the 429 it returns for a rate limit.

    The sender has to hand the worker three things: that this attempt failed, how long the far side
    wants to be left alone, and the `booking_still_committed` note — invariant #11's own wording,
    travelling on the failure so it reaches `outbox.last_error`. A 4xx the sender can recognise as final
    is marked `permanent`: recorded, not acted on, because `outbox_settle` owns the give-up decision and
    a second policy in Python could only drift from the table's.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen.append(body.get("booking_request_id", ""))
        if body.get("booking_request_id") == "rate-limited":
            # Header and body disagree on purpose: the transport-level answer wins.
            return fake_response(
                429,
                {
                    "error": {
                        "code": "rate_limit_429",
                        "message": "slow down",
                        "retry_after_seconds": 30,
                    }
                },
                {"Retry-After": "45"},
            )
        if body.get("booking_request_id") == "rejected":
            return fake_response(
                400, {"error": {"code": "unknown_channel", "message": "no channel"}}
            )
        return fake_response(
            502,
            {
                "error": {
                    "code": "notification_undeliverable",
                    "message": "The notification could not be delivered; the appointment is unaffected",
                    "retry_after_seconds": None,
                    "detail": {
                        "booking_request_id": "B-1",
                        "booking_state": "confirmed",
                        "booking_still_committed": True,
                    },
                }
            },
        )

    sender = real_sender(handler)

    with pytest.raises(DeliveryFailure) as refused:
        send(sender, message(payload={"booking_request_id": "B-1"}))
    assert refused.value.status_code == 502
    assert refused.value.retry_after_s is None
    assert refused.value.permanent is False
    assert "booking_still_committed=True" in str(refused.value)

    with pytest.raises(DeliveryFailure) as limited:
        send(sender, message(payload={"booking_request_id": "rate-limited"}))
    assert limited.value.status_code == 429
    assert limited.value.retry_after_s == 45.0
    assert limited.value.permanent is False, "a rate limit is the one 4xx worth trying again"

    with pytest.raises(DeliveryFailure) as bad:
        send(sender, message(payload={"booking_request_id": "rejected"}))
    assert bad.value.status_code == 400
    assert bad.value.permanent is True
    assert bad.value.retry_after_s is None

    assert seen == ["B-1", "rate-limited", "rejected"]
    asyncio.run(sender.aclose())


@pytest.mark.parametrize(
    ("status", "body", "expected_detail"),
    [
        (
            404,
            {"error": {"code": "unknown_booking_request", "message": "no such booking"}},
            "the facility has no such booking",
        ),
        (
            200,
            {"nothing_to_deliver": True, "reason": "the hospital has no patient contact on file"},
            "no patient contact on file",
        ),
    ],
)
def test_an_answer_with_nothing_to_send_is_empty_rather_than_failed(
    status: int, body: Mapping[str, Any], expected_detail: str
) -> None:
    """Both "there was nothing to deliver" answers are `EMPTY`, and `EMPTY` is a success.

    A 404 for a booking that does not exist is not a failure to deliver: there is no message, so there
    is nothing to retry, and a worker that retried it would spend the whole budget on a question the
    facility has already answered definitively. The distinction a caller must not get wrong is
    `is_success` (True here) versus `delivered` (False): the field a careless caller reaches for is the
    wrong one on exactly this answer.
    """
    sender = real_sender(lambda request: fake_response(status, body))

    result = send(sender)
    asyncio.run(sender.aclose())

    assert result.outcome is DeliveryOutcome.EMPTY
    assert result.is_success is True
    assert result.delivered is False
    assert result.detail is not None and expected_detail in result.detail


def test_the_request_body_is_an_allow_list_and_carries_the_dedupe_key() -> None:
    """Only the fields the notification API publishes are forwarded, and the delivery is keyed.

    An enqueue carries its own bookkeeping — a workflow reference, an attempt counter, whatever a
    caller added — and forwarding it wholesale would eventually be a 400 for a field the API never
    agreed to. The idempotency key is the *message's*, not the payload's: the delivery is the thing that
    must not happen twice, and the row's key is the only identifier unique per message whether or not
    the payload mentions a booking.
    """
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode())
        return fake_response(200, {"booking_request_id": "B-1", "booking_still_committed": True})

    sender = real_sender(handler)
    result = send(
        sender,
        message(
            dedupe_key="notify-B-1-email",
            payload={
                "booking_request_id": "B-1",
                "idempotency_key": "idem-1",
                "channel": "email",
                "workflow_id": "must-not-travel",
                "attempt_count": 3,
            },
        ),
    )
    asyncio.run(sender.aclose())

    assert result.outcome is DeliveryOutcome.DELIVERED
    assert captured["path"] == "/notifications"
    assert captured["body"] == {
        "booking_request_id": "B-1",
        "idempotency_key": "idem-1",
        "channel": "email",
    }
    assert "must-not-travel" not in json.dumps(captured["body"])
    assert captured["headers"]["idempotency-key"] == "notify-B-1-email"


def test_the_allow_list_is_exactly_the_fields_the_sender_names() -> None:
    """A guard on the constant the three tests above depend on.

    Each of them asserts a body shape built from it, so a field added here without being added to the
    API — or the reverse — is the drift this pins: the list is short, explicit, and equal to what the
    notification contract accepts.
    """
    assert tuple(outbox_module._NOTIFICATION_FIELDS) == (
        "booking_request_id",
        "idempotency_key",
        "channel",
    )
