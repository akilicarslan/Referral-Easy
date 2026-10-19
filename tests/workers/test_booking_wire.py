"""What the facility's bytes mean, over the real HTTP adapter (§7, §17).

Every other test in this directory drives the booking path through `ScriptedBookingApi`, which is the
right tool for the reconciler, the pipeline and the resume plans — and the wrong one for the adapter
itself. The double answers in the *views* (`BookingAttemptView`, `LookupView`, `CancelView`), so a
mapping inside `HttpBookingApi` that reads the wrong key, the wrong enum member or the wrong nested
path is invisible to all of them. This module drives the adapter over `httpx.MockTransport` with the
mock API's own response shapes (copied from `services/mock-api/src/modules/appointments.ts`), so the
place where a JSON body becomes a contract value is covered by something other than the code being
covered.

This module exists because of two defects the rest of the suite could not see, and it is the pin for
both.

`cancel` compared the facility's state string to `BookingState.cancelled` — a member the enum does not
have, so the ordinary "the hospital confirmed the cancellation" answer raised `AttributeError` instead
of returning a `CancelView`, on the one path that tells a patient their appointment is gone. The
inverse of the fix (`BookingState.CANCELLED`) is asserted here, together with the thing the bug
endangered: `confirmed` is what a patient is told, and nothing else may set it.

`create_booking` read `booking_request_id`/`booking_state` from `booking` and from a top-level
`detail`, and missed the third place — `error.detail`, which is where the *outcome-unknown* envelope
carries them. That is the envelope of the case reconciliation exists for, so the view reported `None`
for a facility that had just said "a booking may exist, here is its id and its state".

Offline by construction: `MockTransport` answers in-process, so no socket is opened and the default
offline profile holds (§8).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest
from worker_doubles import SPEC_CAPS

from referraleasy_agent_runtime import UpstreamTransient
from referraleasy_contracts.generated.booking import BookingAttemptResult
from referraleasy_workers.booking import CancelView, HttpBookingApi, LookupOutcome, LookupView

#: The spec's booking request id, in the shape the facility uses: the adapter puts it in the path.
BOOKING_REQUEST_ID = "B-7001"
#: The one key, minted once. Every test that sends one sends this.
KEY = "wf-7001-booking-1"

CONFIRMED_MESSAGE = "The hospital has confirmed this appointment is cancelled."
STILL_CHECKING_MESSAGE = (
    "We asked the hospital to cancel this appointment and have not had it confirmed yet. "
    "We are still checking, and we will tell you as soon as we know."
)


def response(
    status: int, body: Any = None, headers: Mapping[str, str] | None = None
) -> httpx.Response:
    """A response with a fresh body stream — a reused `Response` is already consumed."""
    return httpx.Response(
        status, json=body if body is not None else {}, headers=dict(headers or {})
    )


def adapter_with(handler: Any) -> HttpBookingApi:
    """The shipping adapter over a transport that answers in-process.

    `caps` is the spec's own budget object rather than a stand-in, because the timeout the adapter
    computes comes out of it: a test that passed a made-up cap would be asserting against a number
    the deployment does not use.
    """
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://booking.invalid/"
    )
    return HttpBookingApi("http://booking.invalid/", caps=SPEC_CAPS, client=client)


def cancel(handler: Any, *, reason: str | None = None) -> CancelView:
    return asyncio.run(
        adapter_with(handler).cancel(booking_request_id=BOOKING_REQUEST_ID, reason=reason)
    )


def lookup(handler: Any) -> LookupView:
    return asyncio.run(adapter_with(handler).lookup_request(key=KEY))


# ---------------------------------------------------------------------------
# Cancellation: the one call whose answer is read aloud to a patient
# ---------------------------------------------------------------------------


def test_a_cancelled_appointment_is_the_only_thing_narrated_as_cancelled() -> None:
    """The regression pin: `CANCELLED` on the wire is a confirmed cancellation, and it parses.

    The mock API's own success body (`appointments.ts`, the `/cancel` handler): the booking, under
    `booking.state`, in the uppercase vocabulary the contract enum uses. Before the fix this call
    raised `AttributeError` — `BookingState.cancelled` does not exist — on the one path that tells a
    patient their appointment is gone. A test that only checked `requested` would have passed
    against the bug, which is why `confirmed` and the sentence are both asserted.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/appointments/{BOOKING_REQUEST_ID}/cancel"
        return response(
            200,
            {
                "outcome": "created",
                "booking": {"booking_request_id": BOOKING_REQUEST_ID, "state": "CANCELLED"},
                "appointment": {"state": "CANCELLED"},
                "idempotent_replay": False,
            },
        )

    view = cancel(handler)

    assert view.requested is True
    assert view.confirmed is True, "the facility said CANCELLED and the adapter must read it"
    assert view.booking_state == "CANCELLED"
    assert view.http_status == 200
    assert view.patient_message == CONFIRMED_MESSAGE


def test_a_cancellation_still_pending_is_not_a_cancelled_appointment() -> None:
    """`CANCEL_PENDING` is a real state on the wire, and it is the dangerous near-miss.

    The mock API writes `CANCEL_PENDING` between the request and the facility's confirmation
    (`service.ts:773`), so this is exactly what a cancellation whose confirmation failed looks like.
    Reporting it as cancelled would tell a patient they have no appointment while the slot is still
    held in their name; the adapter reports it as requested-but-unconfirmed, keeps the spelling it
    was given, and the workflow stays in `BOOKING_UNVERIFIED` until a lookup says otherwise.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return response(
            200,
            {
                "outcome": "created",
                "booking": {"booking_request_id": BOOKING_REQUEST_ID, "state": "CANCEL_PENDING"},
                "appointment": {"state": "CANCEL_PENDING"},
                "idempotent_replay": False,
            },
        )

    view = cancel(handler)

    assert view.requested is True
    assert view.confirmed is False
    assert view.booking_state == "CANCEL_PENDING", "the answer is kept, not rounded to a boolean"
    assert view.patient_message == STILL_CHECKING_MESSAGE


def test_a_state_this_worker_cannot_parse_is_not_read_as_cancelled() -> None:
    """An unreadable state is reported verbatim and resolves to "not confirmed".

    A facility that spells its own vocabulary (lowercase here) is the case a defensive
    `str.lower() == "cancelled"` would swallow — and swallowing it is what makes a *newer* state, one
    this worker was never taught, indistinguishable from the one state that licenses the sentence
    "your appointment is cancelled". The raw string travels on the view so a person reading the
    escalation can see what actually came back.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return response(
            200,
            {
                "booking": {"booking_request_id": BOOKING_REQUEST_ID, "state": "cancelled"},
                "outcome": "created",
            },
        )

    view = cancel(handler)

    assert view.confirmed is False
    assert view.booking_state == "cancelled"
    assert view.patient_message == STILL_CHECKING_MESSAGE


def test_a_refusal_is_an_answer_and_never_a_confirmed_cancellation() -> None:
    """The mock API's 409 `not_cancellable`, in its own error envelope.

    A refusal is a response, not a transport failure: the adapter returns a view (`requested=True`,
    `confirmed=False`) rather than raising, and the `fault_type`/message reaches the caller so the
    escalation can name what the facility said. Raising here would turn "the hospital says this
    booking cannot be cancelled" into a retry the retry curve owns — and the curve would spend the
    budget re-asking a question that was already answered.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return response(
            409,
            {
                "error": {
                    "code": "not_cancellable",
                    "message": "A booking in state CANCELLED cannot be cancelled",
                    "request_id": "req-1",
                    "fault_type": None,
                    "scenario_id": None,
                    "retry_after_seconds": None,
                    "detail": {"state": "CANCELLED"},
                }
            },
        )

    view = cancel(handler, reason="patient_requested")

    assert view.requested is True
    assert view.confirmed is False
    assert view.http_status == 409
    assert view.detail == "A booking in state CANCELLED cannot be cancelled"
    assert view.patient_message == STILL_CHECKING_MESSAGE


def test_a_transport_failure_leaves_the_cancellation_unconfirmed_with_no_status() -> None:
    """No response at all is the one case that raises inside `_request` — and it is caught here.

    The cancel path is best-effort by design: the caller asked the facility and cannot know the
    answer, so it returns `requested=True, confirmed=False` with status `0` rather than propagating
    an exception that would abort a workflow mid-recovery. `0` is not a real status and reads as
    "never answered", which is the truth.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    view = cancel(handler)

    assert view.requested is True
    assert view.confirmed is False
    assert view.http_status == 0
    assert view.booking_state is None
    assert "ConnectError" in (view.detail or "")


# ---------------------------------------------------------------------------
# Lookup: the same key, or an answer about somebody else's
# ---------------------------------------------------------------------------


def test_a_key_the_facility_has_never_seen_is_a_definite_answer_about_this_key() -> None:
    """404 `unknown_idempotency_key` is the facility's own system saying "never created".

    It is not a failed lookup: a source that would have the booking if it existed has said that it
    does not, which is what lets the reconciler stop asking. The lookup is issued under the key it
    was given — the query string carries that key and nothing else.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/appointments/lookup_request"
        # The whole query string, not just a membership check: one parameter, and it is ours. A
        # second key anywhere on this URL is the mistake the module docstring of `booking.py` forbids.
        assert dict(request.url.params) == {"idempotency_key": KEY}
        return response(
            404, {"error": {"code": "unknown_idempotency_key", "message": f"No booking for {KEY}"}}
        )

    view = lookup(handler)

    assert view.outcome is LookupOutcome.DEFINITIVELY_ABSENT_FOR_SAME_KEY
    assert view.idempotency_key == KEY
    assert view.http_status == 404


def test_a_booking_answered_under_another_key_is_named_as_another_key() -> None:
    """The "same key" claim is checked against the body, not assumed from the request.

    `lookup_request` is sent under one key and the facility may answer about a different one — a
    duplicate booking made by somebody else, a migration, a bug on their side. Reporting that as a
    booking *under our key* would let the reconciler conclude the patient's appointment exists while
    the key it is tracking has nothing, so the other key is carried on the view.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return response(
            200,
            {
                "outcome": "created",
                "lookup": {"outcome": "created"},
                "booking": {"idempotency_key": "somebody-elses-key", "state": "CONFIRMED"},
                "reconciliation": {"status": "resolved"},
            },
        )

    view = lookup(handler)

    assert view.outcome is LookupOutcome.BOOKING_FOUND_UNDER_A_DIFFERENT_KEY
    assert view.idempotency_key == KEY, "the lookup's own key is what the caller asked about"
    assert view.other_key == "somebody-elses-key"


def test_a_lookup_that_cannot_answer_says_so_instead_of_reporting_absence() -> None:
    """The facility's non-answer is not an absence — the distinction the whole recovery turns on.

    `lookups_disagree` is the facility reporting it cannot resolve the question; a `200` whose
    `lookup.outcome` this worker does not recognise is the same thing from our side. Both must land
    on `LOOKUP_FAILED_OR_TIMED_OUT`, because `DEFINITIVELY_ABSENT_FOR_SAME_KEY` leads to a second
    booking attempt and an unanswered question must never lead there.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return response(
            200,
            {
                "outcome": "created",
                "lookup": {"outcome": "lookups_disagree"},
                "booking": {"state": "PENDING_REVIEW"},
            },
        )

    view = lookup(handler)

    assert view.outcome is LookupOutcome.LOOKUP_FAILED_OR_TIMED_OUT
    assert view.booking_state == "PENDING_REVIEW"
    assert "lookups_disagree" in (view.detail or "")


# ---------------------------------------------------------------------------
# Create: the key travels twice, and a bare 5xx is the retry signal
# ---------------------------------------------------------------------------


def test_the_create_request_carries_the_key_in_the_header_and_the_body() -> None:
    """Both places, because the mock accepts either and only one of them is a real contract.

    The mock API reads `idempotency-key` from the header *or* the body, so a sender that set only one
    would look correct until the day the other path is exercised. The `command_id` travels in the
    body too: it is what the dedupe table is keyed on, and a second replica has to hash the same
    request to dedupe onto the first one's row.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response(
            201,
            {
                "outcome": "created",
                "booking": {
                    "booking_request_id": BOOKING_REQUEST_ID,
                    "idempotency_key": KEY,
                    "state": "CONFIRMED",
                },
            },
        )

    view = asyncio.run(
        adapter_with(handler).create_booking(
            key=KEY, payload={"slot_id": "S-1", "hospital_id": "H-1"}, command_id="cmd-1"
        )
    )

    assert view.result is BookingAttemptResult.created
    assert view.idempotency_key == KEY
    assert view.booking_request_id == BOOKING_REQUEST_ID
    (request,) = seen
    assert request.headers["idempotency-key"] == KEY
    sent = json.loads(request.content.decode())
    assert sent["idempotency_key"] == KEY
    assert sent["command_id"] == "cmd-1"
    assert sent["slot_id"] == "S-1"


def test_a_bare_5xx_is_the_one_answer_the_retry_curve_owns() -> None:
    """The classification that decides whether a transport failure is retried or reported.

    A status with no `booking_outcome_unknown` envelope is "not now" — `UpstreamTransient`, carrying
    the status and the `Retry-After` — because that exception is the only signal the runtime's retry
    curve acts on (§8). The unknown-outcome envelope, by contrast, is an *answer*: it returns a view
    whose result is `outcome_unknown`, which the pipeline turns into a lookup under the same key
    rather than a retry. If the two were the same, a possibly-committed write would be re-sent.
    """

    def transient(request: httpx.Request) -> httpx.Response:
        return response(503, {"error": {"code": "upstream_unavailable", "message": "try later"}})

    def unknown(request: httpx.Request) -> httpx.Response:
        return response(
            504,
            {
                "error": {
                    "code": "booking_outcome_unknown",
                    "message": "A booking MAY exist: reconcile with the same idempotency key",
                    "fault_type": "timeout_after_commit",
                    "retry_after_seconds": None,
                    "detail": {
                        "attempt_result": "timeout_after_commit",
                        "booking_request_id": BOOKING_REQUEST_ID,
                        "booking_state": "CONFIRMED",
                        "booking_exists": True,
                    },
                }
            },
            # The facility's pacing arrives as a header, not in the body: this is the value the
            # retry curve honours, so it is the one the view must carry.
            {"Retry-After": "30"},
        )

    send = adapter_with(transient).create_booking
    with pytest.raises(UpstreamTransient) as raised:
        asyncio.run(send(key=KEY, payload={"slot_id": "S-1"}))
    assert raised.value.status_code == 503

    view = asyncio.run(adapter_with(unknown).create_booking(key=KEY, payload={"slot_id": "S-1"}))
    assert view.result is BookingAttemptResult.outcome_unknown
    assert view.http_status == 504
    assert view.booking_state == "CONFIRMED", "the facility named the state inside `error.detail`"
    assert view.booking_request_id == BOOKING_REQUEST_ID, "and the booking it is talking about"
    assert view.retry_after_s == 30.0, "the facility's own pacing survives, it is not invented"
