"""Draining the outbox without taking a booking down with it (§7, §11).

The queue is a table (see `migrations/010_outbox.sql`), so the interesting decisions are not "how do
we deliver" but "who owns the retry" and "what does a failure mean". This module answers both, and
the answers are the ones that keep a notification from becoming a booking's problem.

**The row owns the retry. This module owns exactly one attempt.** `outbox.attempt_count` and
`outbox.next_attempt_at` are the durable retry state: a failure settles as `pending` with a backoff,
and after the budget is spent the row goes `dead` and is never claimable again. There is therefore no
retry loop here, and the sender's HTTP transport has its retries turned off
(:func:`~referraleasy_agent_runtime.http.build_http_client`), so "one delivery attempt" means one
request. A worker that retried in process *on top of* that would multiply the budget the database is
already enforcing — nine requests where the row says three.

**A delivery failure is not a workflow failure, and it is not reported as one.** The enqueue happened
in the booking's transaction; the delivery happens here, later, in another transaction. Nothing in
this module rolls anything back — it cannot: it never touches the booking. The failure is written to
the *row* (`last_error`) and a later attempt is scheduled. `invariant #11` reads "a failed
notification must never roll back a booking", and the structural version of that sentence is that
these two writes do not share a transaction; the tests pin both the structure (the booking row is
untouched by a failed delivery) and the symptom (the workflow never enters a failure state because a
message bounced).

**A valid "nothing to deliver" is a success.** Two of them exist and both are real answers rather
than failures: the facility saying the notification has no content, and the facility answering that
this booking does not exist (there is nothing to send a message *about*). Retrying either is
pointless, and a worker that retried them would spend the whole attempt budget on a question that
has already been answered. They settle as `delivered` with the reason recorded in `last_error`'s
cousin — the delivery detail — so the difference between "sent" and "nothing to send" survives in
the report even though both stopped the loop.

**`Retry-After` wins over our curve.** When the far side says "not before N seconds", our configured
base is only a floor: the row is pushed to `max(configured_base, ceil(retry_after))`. Honouring it
is not politeness — a provider that answered 429 with a delay is a provider whose next refusal is
already scheduled if we ignore it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Protocol

import httpx

from referraleasy_agent_runtime import (
    BudgetCaps,
    HttpProfile,
    build_http_client,
    parse_retry_after,
    service_auth_headers,
)
from referraleasy_domain import (
    DrainReport,
    OutboxClaimError,
    OutboxMessage,
    OutboxStatus,
    OutboxStore,
)
from referraleasy_workers.db import ConnectionLike
from referraleasy_workers.settings import WorkerSettings

__all__ = [
    "DeliveryFailure",
    "DeliveryOutcome",
    "DeliveryResult",
    "HttpNotificationSender",
    "OutboxRunReport",
    "OutboxSender",
    "OutboxWorker",
]


class DeliveryOutcome(StrEnum):
    """What one delivery attempt did."""

    DELIVERED = "delivered"
    #: A valid answer that there was nothing to deliver. Success — see the module docstring.
    EMPTY = "empty"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """The outcome of one delivery, with the distinction the retry decision needs.

    ``delivered`` is False for :attr:`DeliveryOutcome.EMPTY`, and that is deliberate: it is the field
    a careless caller reaches for, and :attr:`is_success` is the one that decides the settle. The two
    differ on exactly the case this module exists to keep out of the retry loop.
    """

    outcome: DeliveryOutcome = DeliveryOutcome.DELIVERED
    detail: str | None = None
    #: Set when the far side named a delay. Only meaningful for a failure, and only read there.
    retry_after_s: float | None = None

    @property
    def delivered(self) -> bool:
        return self.outcome is DeliveryOutcome.DELIVERED

    @property
    def is_empty(self) -> bool:
        return self.outcome is DeliveryOutcome.EMPTY

    @property
    def is_success(self) -> bool:
        """Whether this attempt should stop the loop for this row."""
        return self.outcome in (DeliveryOutcome.DELIVERED, DeliveryOutcome.EMPTY)


class DeliveryFailure(Exception):
    """One attempt did not work, and the row must be settled as a failure.

    ``retry_after_s`` is carried on the exception rather than in a global because it is the only way
    it reaches the settle call — and the settle call is where the backoff is actually computed.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_s: float | None = None,
        permanent: bool = False,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_s = retry_after_s
        #: A refusal that will not become different by trying again (a 4xx the sender understands).
        #: Recorded, but not used to skip the budget: `outbox_settle` owns the terminal decision, and
        #: a second "give up" path in Python would be a policy that could drift from the table's.
        self.permanent = permanent
        self.detail = dict(detail or {})


class OutboxSender(Protocol):
    """What the worker needs from a delivery channel: one attempt, no retrying of its own."""

    async def send(self, message: OutboxMessage) -> DeliveryResult:  # pragma: no cover - protocol
        raise NotImplementedError


#: The request fields the notification API accepts. Copied out of the message payload rather than
#: forwarded wholesale, so an enqueue with extra bookkeeping in it cannot turn into a 400.
_NOTIFICATION_FIELDS: Final = ("booking_request_id", "idempotency_key", "channel")


class HttpNotificationSender:
    """Delivery over HTTP, against a transport with retries off.

    The body is built from the allow-list above. When the payload names neither a booking nor a key,
    the message's own `dedupe_key` is sent as the transport idempotency header — the delivery is the
    thing that must not happen twice, and it is the only identifier left that is unique per message.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        caps: BudgetCaps,
        client: httpx.AsyncClient | None = None,
        timeout_ms: int | None = None,
    ) -> None:
        normalised = base_url.rstrip("/") + "/"
        if client is not None:
            self._client = client
            self._profile: HttpProfile | None = None
        else:
            self._client, self._profile = build_http_client(caps, base_url=normalised)
        self._token = token
        # The same budget field the booking adapter reads: one timeout for HTTP tools, so a
        # notification cannot outlive the call that the budget is counting. (`tool_timeout_ms` was a
        # typo for it, and it made this constructor raise `AttributeError` — caught by
        # `tests/workers/test_worker_commands.py`.)
        self._timeout_ms = timeout_ms or caps.http_tool_timeout_ms
        self.base_url = normalised

    @property
    def profile(self) -> HttpProfile | None:
        """The transport policy — `retries=0` when this sender built its own client (§8)."""
        return self._profile

    async def send(self, message: OutboxMessage) -> DeliveryResult:
        body = {
            field: message.payload[field]
            for field in _NOTIFICATION_FIELDS
            if field in message.payload
        }
        body.setdefault(
            "idempotency_key", message.payload.get("idempotency_key", message.dedupe_key)
        )
        try:
            response = await self._client.post(
                "notifications",
                json=body,
                headers={
                    **service_auth_headers(self._token),
                    "idempotency-key": message.dedupe_key,
                },
                timeout=self._timeout_ms / 1000,
            )
        except httpx.TransportError as error:
            raise DeliveryFailure(f"{type(error).__name__}: {error}") from error

        payload = _json_object(response)
        if response.status_code < 300:
            if payload.get("nothing_to_deliver") or payload.get("skipped"):
                return DeliveryResult(
                    outcome=DeliveryOutcome.EMPTY,
                    detail=str(payload.get("reason") or "the facility had nothing to deliver"),
                )
            return DeliveryResult(detail=str(payload.get("report") or "delivered"))

        code = _error_code(payload)
        retry_after_s = parse_retry_after(response.headers.get("Retry-After")) or _retry_after(
            payload
        )
        if response.status_code == 404 and code == "unknown_booking_request":
            # There is no such booking, so there is no message to deliver. A resolved answer: the
            # row stops here rather than spending the budget asking the same question again.
            return DeliveryResult(
                outcome=DeliveryOutcome.EMPTY,
                detail="the facility has no such booking; nothing to notify about",
            )
        detail = (
            payload.get("error", {}).get("detail")
            if isinstance(payload.get("error"), Mapping)
            else None
        )
        raise DeliveryFailure(
            f"notification delivery answered {response.status_code} ({code or 'no code'})"
            + (
                f"; booking_still_committed={detail.get('booking_still_committed')}"
                if isinstance(detail, Mapping)
                else ""
            ),
            status_code=response.status_code,
            retry_after_s=retry_after_s,
            permanent=400 <= response.status_code < 500 and response.status_code != 429,
            detail=detail if isinstance(detail, Mapping) else None,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass(frozen=True, slots=True)
class OutboxRunReport:
    """What one drain pass did, in the domain's vocabulary plus the facts only a worker knows."""

    report: DrainReport
    #: Every row this pass settled, so a caller can see the backoff that was written without
    #: re-querying. Terminal rows are included: reaching `dead` is a result, not an absence of one.
    settlements: tuple[OutboxMessage, ...] = ()
    #: Rows that ended on a valid "there was nothing to deliver", which is why they are not retried.
    #: Tracked here because the row itself cannot hold the reason: a `delivered` row must have a NULL
    #: `last_error` (`outbox_last_error_when_unhappy`), and that constraint is not one to weaken.
    empty_results: tuple[OutboxMessage, ...] = ()
    #: One line per attempt that did not send, for a log or an operator looking at a stuck queue.
    notes: tuple[str, ...] = ()
    #: How many of the failures had their backoff raised because the far side asked for a delay.
    retry_after_honoured: int = 0

    @property
    def nothing_to_do(self) -> bool:
        """Whether the queue had nothing due at all — the healthy steady state."""
        return self.report.batches == 0 and self.report.claimed == 0

    @property
    def terminal(self) -> tuple[OutboxMessage, ...]:
        """Rows that reached a terminal state in this pass (delivered *and* dead)."""
        return tuple(row for row in self.settlements if row.is_terminal)

    @property
    def buried(self) -> tuple[OutboxMessage, ...]:
        """Rows whose attempt budget ran out: the loop has stopped for them, permanently."""
        return tuple(row for row in self.settlements if row.status == OutboxStatus.DEAD)


class OutboxWorker:
    """Claims, delivers and settles — one attempt per row per pass, with the row owning the retry."""

    def __init__(
        self,
        conn: ConnectionLike,
        *,
        worker_id: str,
        sender: OutboxSender,
        settings: WorkerSettings,
        store: OutboxStore | None = None,
    ) -> None:
        self._conn = conn
        self._store = store or OutboxStore(conn)
        self.worker_id = worker_id
        self.sender = sender
        self.settings = settings

    async def run_once(
        self,
        *,
        limit: int | None = None,
        lease_seconds: int | None = None,
        max_batches: int | None = None,
    ) -> OutboxRunReport:
        """Drain until the queue is empty, the batch ceiling is reached, or a batch comes back empty.

        Everything claimed in a batch is settled before the caller's transaction is committed, so the
        batch's row locks (taken by `outbox_claim`'s `FOR UPDATE SKIP LOCKED`) are held across the
        deliveries. That is the *first* guard against two workers delivering the same row — a second
        claim skips locked rows rather than blocking on them. The lease (`claimed_until`) is the
        second, and it is the one that matters when a claim is committed separately and the worker
        then dies: the row becomes claimable again when the lease runs out, and a settle from the
        dead worker is refused (`RE007`) rather than accepted.
        """
        settings = self.settings
        ceiling = settings.outbox_max_batches if max_batches is None else max_batches
        # `None` is the documented "until the queue is empty" — and it is the *default* — so the
        # non-positive test has to come second, or a worker running its default configuration dies on
        # its first pass (`test_an_empty_queue_is_the_healthy_steady_state` is what caught that).
        if ceiling is not None and ceiling <= 0:
            ceiling = None

        batches = claimed = delivered = retried = dead = lost = honoured = 0
        settlements: list[OutboxMessage] = []
        empties: list[OutboxMessage] = []
        notes: list[str] = []

        while ceiling is None or batches < ceiling:
            batch = self._store.claim(
                self.worker_id,
                limit=limit or settings.outbox_claim_limit,
                lease_seconds=lease_seconds or settings.outbox_lease_seconds,
            )
            if not batch:
                break
            batches += 1

            for message in batch:
                claimed += 1
                attempt = await self._attempt(message)
                backoff_base, did_honour = self._backoff_for(attempt)
                if did_honour:
                    honoured += 1
                try:
                    # A savepoint when the caller already has a transaction open: a refused settle
                    # (RE007) poisons the enclosing transaction otherwise, and the pass could then
                    # neither report the lost lease nor continue past it.
                    with self._conn.transaction():
                        settled = self._store.settle(
                            message.id,
                            self.worker_id,
                            ok=attempt.ok,
                            error=attempt.error,
                            backoff_base=backoff_base,
                            backoff_cap=settings.outbox_backoff_cap_seconds,
                        )
                except OutboxClaimError:
                    # The lease expired while the sender was working, so the row belongs to whoever
                    # holds it now and its outcome is theirs to record. Counted, not retried: a
                    # worker that re-claimed here would be the second worker this guard exists to
                    # prevent.
                    lost += 1
                    notes.append(
                        f"{message.id}: lease lost during delivery; outcome left to the new owner"
                    )
                    continue

                settlements.append(settled)
                if attempt.empty:
                    empties.append(settled)
                    notes.append(f"{message.id}: nothing to deliver ({attempt.detail})")
                elif attempt.error:
                    notes.append(f"{message.id}: {attempt.error}")
                if settled.status == OutboxStatus.DELIVERED:
                    delivered += 1
                elif settled.status == OutboxStatus.DEAD:
                    dead += 1
                else:
                    retried += 1

        return OutboxRunReport(
            report=DrainReport(
                batches=batches,
                claimed=claimed,
                delivered=delivered,
                retried=retried,
                dead=dead,
                lost=lost,
            ),
            settlements=tuple(settlements),
            empty_results=tuple(empties),
            notes=tuple(notes),
            retry_after_honoured=honoured,
        )

    async def _attempt(self, message: OutboxMessage) -> _Attempt:
        """Exactly one delivery attempt. No loop, no second try — the row is the retry owner."""
        try:
            result = await self.sender.send(message)
        except DeliveryFailure as failure:
            return _Attempt(
                ok=False,
                error=_failure_text(failure),
                retry_after_s=failure.retry_after_s,
            )
        except Exception as error:  # a sender that raised anything else is still a failed attempt
            return _Attempt(ok=False, error=f"{type(error).__name__}: {error}")
        return _Attempt(
            ok=result.is_success,
            error=None if result.is_success else (result.detail or "delivery refused"),
            detail=result.detail,
            retry_after_s=result.retry_after_s,
            empty=result.is_empty,
        )

    def _backoff_for(self, attempt: _Attempt) -> tuple[int, bool]:
        """The base seconds for this settle, with `Retry-After` as a floor rather than a suggestion."""
        configured = self.settings.outbox_backoff_base_seconds
        if attempt.retry_after_s is None or attempt.retry_after_s <= configured:
            return configured, False
        # Round up: half a second short of the provider's window is still inside it.
        return max(configured, math.ceil(attempt.retry_after_s)), True


@dataclass(frozen=True, slots=True)
class _Attempt:
    """One delivery attempt, flattened to exactly what the settle and the report need."""

    ok: bool
    #: What goes into `outbox.last_error`. None on success: the table refuses a `last_error` on a
    #: `delivered` row, and that constraint is the reason a delivered row cannot be re-read as a
    #: failure.
    error: str | None = None
    #: A human-readable reason for the pass report, kept even when the attempt succeeded.
    detail: str | None = None
    retry_after_s: float | None = None
    #: This was a valid "there was nothing to deliver" rather than a send.
    empty: bool = False


def _failure_text(failure: DeliveryFailure) -> str:
    text = str(failure)
    if failure.permanent:
        text += " (refused; will not improve on its own)"
    return text


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        parsed: Any = response.json()
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _error_code(payload: Mapping[str, Any]) -> str | None:
    error = payload.get("error")
    if isinstance(error, Mapping):
        code = error.get("code")
        return None if code is None else str(code)
    return None


def _retry_after(payload: Mapping[str, Any]) -> float | None:
    """The delay the facility asked for, from the envelope when there is no header."""
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return None
    seconds = error.get("retry_after_seconds")
    if isinstance(seconds, (int, float)) and seconds > 0:
        return float(seconds)
    return None


def settled_at(row: OutboxMessage) -> datetime:
    """When this row stopped, read from whichever terminal column applies."""
    if row.delivered_at is not None:
        return row.delivered_at
    if row.terminal_at is not None:
        return row.terminal_at
    return row.updated_at
