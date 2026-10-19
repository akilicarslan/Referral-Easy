"""The mock provider API, reached through the runtime's retry-free transport.

Every retrieval a specialist performs lands here. The interesting part is what
this class does *not* do:

* **It does not retry.** One httpx client, built by the runtime's
  `build_http_client`, with `retries=0`. A retry here would be a second retry
  owner: the turn's budget counts an attempt, the tool counts one, and now two
  transports disagree about how many times the upstream was asked. Retrying is
  `run_with_retries`'s job, in the budget's sight.
* **It does not decide anything.** A 4xx becomes :class:`UpstreamRejected`
  (terminal — the request was understood and refused) and a 5xx or 429 becomes
  :class:`UpstreamTransient` (retryable). What to *do* with either is the calling
  agent's decision, and the state machine's after that.
* **It does not read a response into a loose dict and hope.** Handlers validate
  answers against the published contract models, so a mock API that drifts from
  its contract fails a test rather than quietly informing a patient.

The POST path goes through the runtime's own :class:`JsonServiceClient` — the
idempotency key travels in the body (`createBookingRequest` accepts
`idempotency_key` as well as the header), so no custom header path is needed and
the runtime keeps ownership of the write transport.

**A finding, stated rather than hidden.** `JsonServiceClient` supports POST only,
and it raises on a 5xx *without* the response body — so the mock API's
`504 booking_outcome_unknown` is distinguishable only by status code, not by its
`detail.reconcile_with` hint. `create_booking` therefore treats any 5xx or
transport failure on the write path as "the write may have committed", which is
the conservative reading. A richer signal would need the body to survive the
exception; that is a runtime change, not one this service may make.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from referraleasy_agent_runtime import (
    BudgetCaps,
    CallLease,
    HttpProfile,
    JsonServiceClient,
    TurnBudget,
    UpstreamRejected,
    UpstreamTransient,
    build_http_client,
    parse_retry_after,
    service_auth_headers,
)

__all__ = ["DirectoryApi"]


def _without_none(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Query parameters, minus the ones the caller left unset.

    A missing filter and a filter set to `None` are different questions, and the
    mock API treats an empty string as "absent" while treating `plan_id=` as given.
    Dropping `None` here means a handler never has to remember which of the two it
    meant.
    """
    if not params:
        return {}
    return {key: value for key, value in params.items() if value is not None}


class DirectoryApi:
    """GET and POST against the provider API, token attached, retries off."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        caps: BudgetCaps,
        trace_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._trace_id = trace_id
        self._caps = caps
        #: The transport's own policy, when this object built it — `None` when the
        #: caller supplied a client, because a profile describes a client's retry and
        #: redirect settings and this object did not make those choices.
        self.profile: HttpProfile | None
        normalised = f"{self.base_url}/"
        if client is None:
            self._httpx, self.profile = build_http_client(caps, base_url=normalised)
            self._owns_transport = True
        else:
            self._httpx = client
            self.profile = None
            self._owns_transport = False
        # The runtime's client for the write path, sharing this transport, so that
        # one turn has one connection pool and one retry policy (zero).
        self._poster = JsonServiceClient(normalised, token=token, caps=caps, client=self._httpx)

    # ── transport ────────────────────────────────────────────────────────────────────────────

    def _timeout(self, *, budget: TurnBudget | None, lease: CallLease | None) -> float:
        """Seconds, from the lease and what remains of the turn.

        Mirrors `JsonServiceClient._resolve_timeout`: a call near the end of a turn
        gets the milliseconds that remain rather than a fresh nominal timeout,
        because the deadline is shared downward. Kept in one small method so the
        GET path cannot drift from the POST path.
        """
        nominal = lease.timeout_ms if lease is not None else self._caps.http_tool_timeout_ms
        if budget is not None:
            nominal = min(nominal, max(budget.remaining_ms(), 1))
        return nominal / 1000

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        budget: TurnBudget | None = None,
        lease: CallLease | None = None,
    ) -> dict[str, Any]:
        """GET ``path``, mapping transport faults the way the runtime does.

        429 and 5xx are transient (the upstream asked us to come back); any other
        non-2xx is rejected (terminal); a body that is not a JSON object is
        rejected, because a route that answers with something else has not answered
        the question that was asked.
        """
        try:
            response = await self._httpx.get(
                path.lstrip("/"),
                params=_without_none(params),
                headers=service_auth_headers(self._token, trace_id=self._trace_id),
                timeout=self._timeout(budget=budget, lease=lease),
            )
        except httpx.TransportError as error:
            raise UpstreamTransient(f"GET {path}: {error!r}") from error

        retry_after_s = parse_retry_after(response.headers.get("Retry-After"))
        if response.status_code == 429 or response.status_code >= 500:
            raise UpstreamTransient(
                f"GET {path} returned {response.status_code}",
                status_code=response.status_code,
                retry_after_s=retry_after_s,
            )
        if not 200 <= response.status_code < 300:
            raise UpstreamRejected(
                f"GET {path} returned {response.status_code}", status_code=response.status_code
            )
        return _json_object(response, path)

    async def post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        budget: TurnBudget | None = None,
        lease: CallLease | None = None,
    ) -> dict[str, Any]:
        """POST ``payload`` through the runtime's client. Writes included."""
        response = await self._poster.post_json(
            path, payload, trace_id=self._trace_id, budget=budget, lease=lease
        )
        return dict(response.body)

    async def aclose(self) -> None:
        """Close the transport, when this instance built it."""
        if self._owns_transport:
            await self._httpx.aclose()


def _json_object(response: httpx.Response, path: str) -> dict[str, Any]:
    """The response body, as a JSON object, or a terminal protocol failure."""
    try:
        body = response.json()
    except ValueError as error:
        raise UpstreamRejected(
            f"GET {path} returned a body that is not JSON", status_code=response.status_code
        ) from error
    if not isinstance(body, dict):
        raise UpstreamRejected(
            f"GET {path} returned a {type(body).__name__}, not an object",
            status_code=response.status_code,
        )
    return body
