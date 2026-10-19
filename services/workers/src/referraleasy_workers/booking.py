"""The booking API as a worker sees it (§7, §17 — one key, and no second guess).

A worker that talks to a hospital's scheduling system has exactly three jobs here: send a request
with the right key, ask what happened to *that* key, and describe the answer without rounding it off.
The views in this module are shaped around the third one, because it is the one that gets people
hurt: an unknown outcome reported as a failure causes a duplicate booking, and an unknown outcome
reported as a booking strands a patient who has no appointment.

Two rules make the shapes honest.

**Responses are answers; only the absence of a response is an exception.** A 200, a 404 for a key the
facility has never seen, a 409 slot conflict, a 504 whose envelope says the outcome is unknown, and a
502 whose body is unparseable are all answers — the method returns a view and the caller decides.
Only two things raise: a transport failure (no response at all) and a status that means "you cannot
proceed right now" (429 and a bare 5xx), both as
:class:`~referraleasy_agent_runtime.errors.UpstreamTransient`, which is what the retry curve is
allowed to act on. The difference matters for `Retry-After`: it can only be honoured by a caller that
receives an exception carrying it.

**A lookup is never issued under a new key.** :meth:`HttpBookingApi.lookup_request` takes the key it
is given and puts it in the URL; the reconciler asserts it is the attempt's key before calling
(`ensure_same_key`). There is no parameter anywhere in this module that mints one, which is the
structural half of the requirement quoted below: the dangerous call is hard to make, not
merely discouraged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from referraleasy_agent_runtime import (
    BudgetCaps,
    HttpProfile,
    UpstreamRejected,
    UpstreamTransient,
    build_http_client,
    parse_retry_after,
    service_auth_headers,
)
from referraleasy_contracts.generated.booking import BookingAttemptResult
from referraleasy_contracts.generated.common import BookingState

#: §7, quoted verbatim: "İkinci bağımsız booking key üretilmez" — a second, independent booking key is never  # noqa: RUF003
#: generated. The requirement keeps its own words so it can be found in the spec by them.
__all__ = [
    "BookingApi",
    "BookingAttemptView",
    "CancelView",
    "HttpBookingApi",
    "LookupOutcome",
    "LookupView",
    "RescheduleView",
    "UnknownOutcomeLookup",
]


class LookupOutcome(StrEnum):
    """What a `lookup_request` answered — the vocabulary of the reconciliation skill, verbatim.

    Four of these are answers the facility gives; the last two are conclusions the *reconciler*
    draws from a sequence of them, and they live here so that one vocabulary describes the whole
    reconciliation rather than two overlapping ones.
    """

    BOOKING_FOUND_UNDER_SAME_KEY = "booking_found_under_same_key"
    DEFINITIVELY_ABSENT_FOR_SAME_KEY = "definitively_absent_for_same_key"
    LOOKUP_FAILED_OR_TIMED_OUT = "lookup_failed_or_timed_out"
    BOOKING_FOUND_UNDER_A_DIFFERENT_KEY = "booking_found_under_a_different_key"
    #: Conclusions, not answers.
    LOOKUPS_DISAGREE = "lookups_disagree"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True, slots=True)
class BookingAttemptView:
    """The result of one `create_booking` attempt, as the facility reported it."""

    result: BookingAttemptResult
    idempotency_key: str
    http_status: int
    booking_request_id: str | None = None
    booking_state: str | None = None
    detail: str | None = None
    retry_after_s: float | None = None

    @property
    def is_empty(self) -> bool:
        """Never. A booking attempt always answers something; "nothing matched" is a search's word."""
        return False

    @property
    def is_definite(self) -> bool:
        """Whether the facility told us what happened, rather than leaving it open."""
        return self.result in _DEFINITE_RESULTS

    @property
    def requires_reconciliation(self) -> bool:
        """Whether the outcome is unknown and must be resolved under the same key (§7, §17).

        Both timeout flavours land here. `timeout_before_commit` asserts that nothing was committed,
        and that assertion is exactly what the reconciliation skill tells us not to trust: the request
        may have been committed a moment after contact was lost, so the answer is "we do not know"
        until a lookup against the same key says otherwise.
        """
        return self.result in _UNKNOWN_RESULTS

    @property
    def booking_state_enum(self) -> BookingState | None:
        if self.booking_state is None:
            return None
        return _booking_state(self.booking_state)


#: Outcomes that answer the question "what happened".
_DEFINITE_RESULTS = frozenset(
    {
        BookingAttemptResult.created,
        BookingAttemptResult.duplicate_replayed,
        BookingAttemptResult.slot_conflict,
        BookingAttemptResult.rejected_by_hospital,
    }
)
#: Outcomes that leave it open. `rate_limited` and `malformed_response` are in here with the
#: timeouts: both mean a request left and no readable answer came back.
_UNKNOWN_RESULTS = frozenset(
    {
        BookingAttemptResult.timeout_before_commit,
        BookingAttemptResult.timeout_after_commit,
        BookingAttemptResult.malformed_response,
        BookingAttemptResult.rate_limited,
        BookingAttemptResult.outcome_unknown,
    }
)


@dataclass(frozen=True, slots=True)
class LookupView:
    """What one `lookup_request` answered, including an answer about a key we did not mint."""

    outcome: LookupOutcome
    idempotency_key: str
    http_status: int
    booking_request_id: str | None = None
    booking_state: str | None = None
    #: Set only for `BOOKING_FOUND_UNDER_A_DIFFERENT_KEY`: the key the facility actually answered
    #: under. Reported to a human, never adopted — adopting it would be minting a second identity.
    other_key: str | None = None
    #: The facility's own reconciliation status, when it reports one (`pending`, `escalated`, …).
    facility_reconciliation: str | None = None
    detail: str | None = None
    retry_after_s: float | None = None

    @property
    def is_empty(self) -> bool:
        """A definite "there is no booking under this key" is a *valid* answer, not a failure.

        This is the flag the retry loop must consult: asking a system that has already answered the
        same question again cannot change the answer, and burning the lookup budget on it only makes
        the escalation later.
        """
        return self.outcome is LookupOutcome.DEFINITIVELY_ABSENT_FOR_SAME_KEY

    @property
    def is_resolved(self) -> bool:
        return self.outcome in _RESOLVING_OUTCOMES

    @property
    def is_answer(self) -> bool:
        """Whether this lookup told us something, as opposed to failing to."""
        return self.outcome in _ANSWER_OUTCOMES

    @property
    def facility_gave_up(self) -> bool:
        """Whether the *facility* reports its own reconciliation as exhausted.

        Worth acting on: when the other side says it has no answer after repeated lookups, spending
        our own budget on the same question is not diligence.
        """
        return self.facility_reconciliation == "escalated"


_RESOLVING_OUTCOMES = frozenset(
    {
        LookupOutcome.BOOKING_FOUND_UNDER_SAME_KEY,
        LookupOutcome.DEFINITIVELY_ABSENT_FOR_SAME_KEY,
    }
)
_ANSWER_OUTCOMES = frozenset(
    {
        LookupOutcome.BOOKING_FOUND_UNDER_SAME_KEY,
        LookupOutcome.DEFINITIVELY_ABSENT_FOR_SAME_KEY,
        LookupOutcome.BOOKING_FOUND_UNDER_A_DIFFERENT_KEY,
    }
)


@dataclass(frozen=True, slots=True)
class CancelView:
    """The result of a best-effort cancellation (§7).

    `confirmed` is the only thing that may be narrated as "cancelled". A refusal, a timeout and a
    request that is still `CANCEL_PENDING` all leave the appointment where it was, and the workflow
    must stay in `BOOKING_UNVERIFIED` until a lookup says otherwise.
    """

    requested: bool
    confirmed: bool
    http_status: int
    booking_state: str | None = None
    detail: str | None = None

    @property
    def patient_message(self) -> str:
        if self.confirmed:
            return "The hospital has confirmed this appointment is cancelled."
        return (
            "We asked the hospital to cancel this appointment and have not had it confirmed yet. "
            "We are still checking, and we will tell you as soon as we know."
        )


@dataclass(frozen=True, slots=True)
class RescheduleView:
    """The result of a move, whose ordering rule is not negotiable (§7).

    The replacement is created and verified *first*; the original is cancelled only after that. So a
    failed move never leaves the patient with nothing, and a move that cannot be verified is reported
    as a move that cannot be verified — never as a success.
    """

    result: BookingAttemptResult
    http_status: int
    idempotency_key: str | None = None
    booking_request_id: str | None = None
    superseded_booking_request_id: str | None = None
    booking_state: str | None = None
    old_booking_cancelled: bool = False
    cancellation_deferred_reason: str | None = None
    detail: str | None = None

    @property
    def original_still_in_force(self) -> bool:
        """Whether the patient still holds the appointment they started with."""
        return not self.old_booking_cancelled

    @property
    def replacement_exists(self) -> bool:
        """Whether the facility has a booking for the *replacement* (created, or already known)."""
        return self.result in (
            BookingAttemptResult.created,
            BookingAttemptResult.duplicate_replayed,
        )

    @property
    def both_in_force(self) -> bool:
        """The uncomfortable middle: the replacement exists and the original was never cancelled.

        Not a failure of the new booking and not a safe state either — the patient may hold two
        appointments. It is reported as an open question for a person (§11 `conflicting_sources`),
        never as a completed move and never as a failed one.
        """
        return self.replacement_exists and self.original_still_in_force

    @property
    def is_verified_move(self) -> bool:
        """A move is a success only when the new booking exists *and* the old one is gone.

        Both halves are load-bearing, and the second one is the one that is easy to get wrong:
        `old_booking_cancelled` alone says nothing about the replacement, so combining it with
        "the facility gave a definite answer" would call a *refused* move a completed one whenever the
        cancellation had gone through first. That is the exact failure the module docstring warns
        about — a patient told their appointment moved while holding no appointment at all — so the
        replacement is checked explicitly rather than inferred from the answer being definite.
        """
        return self.replacement_exists and self.old_booking_cancelled

    @property
    def patient_message(self) -> str:
        if self.is_verified_move:
            return "Your appointment has been moved, and the hospital has confirmed the change."
        if self.original_still_in_force:
            return (
                "We could not complete the change to your appointment. Your existing appointment "
                "is still in place, and nothing has been cancelled."
            )
        return (
            "The change to your appointment is still being confirmed by the hospital. "
            "We will tell you when it is settled."
        )


@dataclass(frozen=True, slots=True)
class UnknownOutcomeLookup:
    """A lookup that failed to resolve anything, with the reason kept for the escalation."""

    key: str
    attempts: int
    detail: str
    retry_after_s: float | None = None
    results: tuple[LookupOutcome, ...] = field(default_factory=tuple)


class BookingApi:
    """What a worker needs from a scheduling system, stated once.

    A protocol rather than a base class, so the tests drive the reconciler and the booking path with
    a scripted double instead of a fake HTTP server, and so the HTTP adapter's shape is pinned by the
    double being written against the same protocol.
    """

    async def create_booking(
        self,
        *,
        key: str,
        payload: Mapping[str, Any],
        command_id: str | None = None,
        timeout_ms: int | None = None,
    ) -> BookingAttemptView:  # pragma: no cover - protocol
        """Send one booking request under ``key``.

        ``timeout_ms`` is the *attempt lease's* remaining time when the caller has one
        (:class:`~referraleasy_agent_runtime.retries.CallLease`): a request allowed to outlive its own
        lease would produce a timeout the attempt budget never sees, and the budget is the only thing
        that decides when an unknown outcome becomes a human's problem. ``None`` means "use the
        profile's attempt timeout".
        """
        raise NotImplementedError

    async def lookup_request(self, *, key: str) -> LookupView:  # pragma: no cover - protocol
        raise NotImplementedError

    async def cancel(
        self, *, booking_request_id: str, reason: str | None = None
    ) -> CancelView:  # pragma: no cover - protocol
        raise NotImplementedError

    async def reschedule(
        self, *, booking_request_id: str, payload: Mapping[str, Any]
    ) -> RescheduleView:  # pragma: no cover - protocol
        raise NotImplementedError


#: The one error code the mock booking API uses for "a booking MAY exist; reconcile with the same
#: key". Recognised by code rather than by status, because the status is shared with a definite 5xx.
_OUTCOME_UNKNOWN_CODE = "booking_outcome_unknown"


class HttpBookingApi(BookingApi):
    """The HTTP adapter, over a client whose transport retries are off (§8).

    The client is built by :func:`~referraleasy_agent_runtime.http.build_http_client`, so
    `httpx.AsyncHTTPTransport(retries=0)` is the transport's policy and `Retry-After` is handled by
    the runtime's curve rather than by httpx's. The profile is exposed so a test can assert that,
    and so an operator can answer "how many times did one attempt actually hit the network" without
    reading code.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        caps: BudgetCaps,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._caps = caps
        self._token = token
        normalised = base_url.rstrip("/") + "/"
        if client is not None:
            self._client = client
            self._profile: HttpProfile | None = None
        else:
            self._client, self._profile = build_http_client(caps, base_url=normalised)
        self.base_url = normalised

    @property
    def profile(self) -> HttpProfile | None:
        """The transport policy, when this adapter built its own transport."""
        return self._profile

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_ms: int | None = None,
    ) -> tuple[int, dict[str, Any], float | None]:
        """One HTTP exchange, returning the answer whatever it says.

        Only a missing response raises. A status that means "not now" — 429, or a 5xx that is not the
        outcome-unknown envelope — also raises :class:`UpstreamTransient`, because that is the signal
        the retry curve acts on and the only way a `Retry-After` reaches it.
        """
        timeout = (timeout_ms or self._caps.http_tool_timeout_ms) / 1000
        try:
            response = await self._client.request(
                method,
                path,
                json=None if json_body is None else dict(json_body),
                params=params,
                headers={**service_auth_headers(self._token), **(headers or {})},
                timeout=timeout,
            )
        except httpx.TransportError as error:
            raise UpstreamTransient(f"{path}: {error!r}") from error

        body = _json_object(response)
        retry_after_s = parse_retry_after(response.headers.get("Retry-After"))
        code = _error_code(body)
        if response.status_code == 429:
            raise UpstreamTransient(
                f"{path} returned 429", status_code=429, retry_after_s=retry_after_s
            )
        if response.status_code >= 500 and code != _OUTCOME_UNKNOWN_CODE:
            raise UpstreamTransient(
                f"{path} returned {response.status_code}",
                status_code=response.status_code,
                retry_after_s=retry_after_s,
            )
        return response.status_code, body, retry_after_s

    async def create_booking(
        self,
        *,
        key: str,
        payload: Mapping[str, Any],
        command_id: str | None = None,
        timeout_ms: int | None = None,
    ) -> BookingAttemptView:
        """Send one booking request under ``key``.

        The key travels in the `idempotency-key` header *and* in the body, because the mock accepts
        either and a caller that sent only one of them would look correct right up until the day the
        other path is exercised.

        `booking_request_id` and `booking_state` are read from three places, and the third one is the
        one that matters: the outcome-unknown envelope puts both inside `error.detail`, which is the
        answer a reconciler has to work from. Reading only `booking`/`detail` there reported `None`
        for a facility that had just said "a booking may exist, here is its id and its state" — the
        state the resumed run needs in order to describe what it found.
        """
        body_in = dict(payload)
        body_in.setdefault("idempotency_key", key)
        if command_id is not None:
            body_in.setdefault("command_id", command_id)
        status, body, retry_after_s = await self._request(
            "POST",
            "appointments",
            json_body=body_in,
            headers={"idempotency-key": key},
            # A booking write is the one call that must not be retried by a hidden transport: it
            # gets the attempt's own timeout — the caller's lease when it has one — and the attempt
            # budget is spent once, visibly.
            timeout_ms=timeout_ms or self._caps.attempt_timeout_ms,
        )
        return BookingAttemptView(
            result=_attempt_result(body, fallback=_result_for_status(status)),
            idempotency_key=key,
            http_status=status,
            booking_request_id=_nested(body, "booking", "booking_request_id")
            or _nested(body, "detail", "booking_request_id")
            or _nested(body, "error", "detail", "booking_request_id"),
            booking_state=_nested(body, "booking", "state")
            or _nested(body, "detail", "booking_state")
            or _nested(body, "error", "detail", "booking_state"),
            detail=_detail(body),
            retry_after_s=retry_after_s,
        )

    async def lookup_request(self, *, key: str) -> LookupView:
        """Ask what the facility has for ``key``. Never a different key — see the module docstring."""
        status, body, retry_after_s = await self._request(
            "GET", "appointments/lookup_request", params={"idempotency_key": key}
        )
        if status == 404 and _error_code(body) == "unknown_idempotency_key":
            # The facility's own system answering, for this key: never created. A source that would
            # have the booking if it existed has said that it does not.
            return LookupView(
                outcome=LookupOutcome.DEFINITIVELY_ABSENT_FOR_SAME_KEY,
                idempotency_key=key,
                http_status=status,
                detail=_detail(body) or "no booking was ever created with this key",
                retry_after_s=retry_after_s,
            )
        if status != 200:
            return LookupView(
                outcome=LookupOutcome.LOOKUP_FAILED_OR_TIMED_OUT,
                idempotency_key=key,
                http_status=status,
                detail=_detail(body) or f"lookup answered {status}",
                retry_after_s=retry_after_s,
            )

        answered_key = _nested(body, "booking", "idempotency_key")
        answer = _nested(body, "lookup", "outcome")
        state = _nested(body, "booking", "state")
        facility = _nested(body, "reconciliation", "status")
        if answer == "created" and answered_key is not None and answered_key != key:
            return LookupView(
                outcome=LookupOutcome.BOOKING_FOUND_UNDER_A_DIFFERENT_KEY,
                idempotency_key=key,
                other_key=answered_key,
                http_status=status,
                booking_request_id=_nested(body, "booking", "booking_request_id"),
                booking_state=state,
                facility_reconciliation=facility,
                detail="the facility answered under a key this workflow did not mint",
                retry_after_s=retry_after_s,
            )
        if answer == "created":
            return LookupView(
                outcome=LookupOutcome.BOOKING_FOUND_UNDER_SAME_KEY,
                idempotency_key=key,
                http_status=status,
                booking_request_id=_nested(body, "booking", "booking_request_id"),
                booking_state=state,
                facility_reconciliation=facility,
                detail="the facility reports the booking exists",
                retry_after_s=retry_after_s,
            )
        if answer == "not_booked":
            return LookupView(
                outcome=LookupOutcome.DEFINITIVELY_ABSENT_FOR_SAME_KEY,
                idempotency_key=key,
                http_status=status,
                booking_request_id=_nested(body, "booking", "booking_request_id"),
                facility_reconciliation=facility,
                detail="the facility reports no booking under this key",
                retry_after_s=retry_after_s,
            )
        return LookupView(
            outcome=LookupOutcome.LOOKUP_FAILED_OR_TIMED_OUT,
            idempotency_key=key,
            http_status=status,
            booking_request_id=_nested(body, "booking", "booking_request_id"),
            booking_state=state,
            facility_reconciliation=facility,
            detail=f"the facility could not answer ({answer or 'no answer'})",
            retry_after_s=retry_after_s,
        )

    async def cancel(self, *, booking_request_id: str, reason: str | None = None) -> CancelView:
        """Ask the facility to cancel, and report only what came back (§7)."""
        payload: dict[str, Any] = {}
        if reason is not None:
            payload["reason"] = reason
        try:
            status, body, _ = await self._request(
                "POST", f"appointments/{booking_request_id}/cancel", json_body=payload
            )
        except UpstreamRejected as refused:
            return CancelView(
                requested=True,
                confirmed=False,
                http_status=refused.status_code,
                detail=str(refused),
            )
        except UpstreamTransient as transient:
            return CancelView(
                requested=True,
                confirmed=False,
                http_status=transient.status_code or 0,
                detail=str(transient),
            )
        state = _nested(body, "booking", "state") or _nested(body, "appointment", "state")
        # Parsed through `_booking_state`, which is the one place the facility's string becomes a
        # contract value — and compared to `CANCELLED`, which is the member the enum actually has.
        # The spelling here was `BookingState.cancelled`: a member that does not exist, so this line
        # raised `AttributeError` on the wire instead of answering, in a path no double reaches.
        return CancelView(
            requested=True,
            confirmed=state is not None and _booking_state(state) is BookingState.CANCELLED,
            http_status=status,
            booking_state=state,
            detail=_detail(body),
        )

    async def reschedule(
        self, *, booking_request_id: str, payload: Mapping[str, Any]
    ) -> RescheduleView:
        """Move an appointment. The new booking is created and verified before the old is cancelled."""
        status, body, _ = await self._request(
            "POST", f"appointments/{booking_request_id}/reschedule", json_body=dict(payload)
        )
        return RescheduleView(
            result=_attempt_result(body, fallback=_result_for_status(status)),
            http_status=status,
            idempotency_key=_nested(body, "booking", "idempotency_key"),
            booking_request_id=_nested(body, "booking", "booking_request_id"),
            superseded_booking_request_id=_nested(body, "superseded_booking", "booking_request_id"),
            booking_state=_nested(body, "booking", "state"),
            old_booking_cancelled=bool(body.get("old_booking_cancelled")),
            cancellation_deferred_reason=body.get("cancellation_deferred_reason"),
            detail=_detail(body),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        parsed: Any = response.json()
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _error_code(body: Mapping[str, Any]) -> str | None:
    error = body.get("error")
    if isinstance(error, Mapping):
        code = error.get("code")
        return None if code is None else str(code)
    return None


def _detail(body: Mapping[str, Any]) -> str | None:
    error = body.get("error")
    if isinstance(error, Mapping):
        fault = error.get("fault_type")
        if fault is not None:
            return str(fault)
        message = error.get("message")
        return None if message is None else str(message)
    return None


def _nested(body: Mapping[str, Any], *path: str) -> str | None:
    current: Any = body
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return None if current is None else str(current)


def _attempt_result(
    body: Mapping[str, Any], *, fallback: BookingAttemptResult
) -> BookingAttemptResult:
    """The outcome a response reports, or `outcome_unknown` when it reports one we cannot read.

    A result token this worker does not know is not a reason to guess: an unreadable answer is
    precisely the case the unknown-outcome path exists for, and it is the safe direction to fail in.
    """
    token = body.get("outcome") or _nested(body, "detail", "attempt_result")
    if token is None:
        return fallback
    try:
        return BookingAttemptResult(str(token))
    except ValueError:
        return BookingAttemptResult.outcome_unknown


def _result_for_status(status: int) -> BookingAttemptResult:
    """The outcome implied by a bare status, when the body says nothing usable."""
    if status == 504:
        return BookingAttemptResult.outcome_unknown
    if status == 429:
        return BookingAttemptResult.rate_limited
    if status >= 500:
        return BookingAttemptResult.outcome_unknown
    return BookingAttemptResult.outcome_unknown


def _booking_state(value: str) -> BookingState | None:
    try:
        return BookingState(value)
    except ValueError:
        return None
