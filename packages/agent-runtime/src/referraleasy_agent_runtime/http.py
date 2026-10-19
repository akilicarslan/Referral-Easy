"""Transport: one HTTP client, retries set to zero, timeouts derived from the turn.

The outbound path is where budgets leak. httpx defaults are harmless, but every
convenience wrapper layered on top of it (a client library's retry, an SDK's
"resilient" transport, a polling loop) multiplies the attempts the budget already
counts. So the runtime builds *the* client, sets ``retries=0`` explicitly rather
than relying on a default, and hands that same client to the A2A SDK — whose
``ClientConfig`` accepts an ``httpx_client``, which is how an SDK retry policy is
disabled rather than merely discouraged.

Two other deliberate choices:

* **No redirects.** A redirect could move a bearer token to another host.
* **Per-request timeout from the lease.** A call near the end of a turn gets the
  milliseconds that remain, not a fresh 1.5 s — the deadline is shared downward,
  including into the socket.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from referraleasy_agent_runtime.auth import service_auth_headers
from referraleasy_agent_runtime.budget import BudgetCaps, CallLease, TurnBudget
from referraleasy_agent_runtime.errors import UpstreamRejected, UpstreamTransient
from referraleasy_agent_runtime.retries import NO_RETRIES, parse_retry_after

if TYPE_CHECKING:
    from a2a.client import ClientConfig

__all__ = [
    "HttpProfile",
    "JsonServiceClient",
    "ServiceResponse",
    "a2a_client_config",
    "build_http_client",
]

#: Longest connect wait, regardless of the tool budget: DNS or a TCP handshake that takes a
#: second and a half is a dead host, and waiting longer only burns the turn's remainder.
MAX_CONNECT_TIMEOUT_MS: Final = 1000


@dataclass(frozen=True, slots=True)
class HttpProfile:
    """The transport policy, recorded so a test can assert the retries are off."""

    retries: int
    connect_timeout_ms: int
    read_timeout_ms: int
    follow_redirects: bool

    def as_trace_entry(self) -> dict[str, object]:
        return {
            "retries": self.retries,
            "connect_timeout_ms": self.connect_timeout_ms,
            "read_timeout_ms": self.read_timeout_ms,
            "follow_redirects": self.follow_redirects,
        }


def build_http_client(
    caps: BudgetCaps,
    *,
    base_url: str | None = None,
) -> tuple[httpx.AsyncClient, HttpProfile]:
    """Build the shared async client, with transport retries explicitly disabled.

    Returns the client and its profile, so the "one retry owner" claim is
    inspectable at runtime and asserted in ``tests/agent_runtime/test_retries.py``.
    """
    profile = HttpProfile(
        retries=NO_RETRIES,
        connect_timeout_ms=min(MAX_CONNECT_TIMEOUT_MS, caps.http_tool_timeout_ms),
        read_timeout_ms=caps.http_tool_timeout_ms,
        follow_redirects=False,
    )
    client = httpx.AsyncClient(
        # httpx rejects an explicit `base_url=None`, and `""` is its own spelling for
        # "no base URL" — it is literally the library's default value — so the optional
        # argument can be forwarded unconditionally instead of splatted conditionally.
        base_url=base_url or "",
        timeout=httpx.Timeout(
            connect=profile.connect_timeout_ms / 1000,
            read=profile.read_timeout_ms / 1000,
            write=profile.read_timeout_ms / 1000,
            pool=profile.read_timeout_ms / 1000,
        ),
        # The explicit non-default: this is the line that stops a retry from
        # multiplying the attempt budget. See retries.py.
        transport=httpx.AsyncHTTPTransport(retries=profile.retries),
        follow_redirects=profile.follow_redirects,
    )
    return client, profile


def a2a_client_config(client: httpx.AsyncClient) -> ClientConfig:
    """The A2A SDK client config, built around our retry-free client.

    ``polling=False`` on purpose: a polling loop is an unbounded retry wearing a
    different hat. A caller that wants to poll asks again under a new turn, where
    the budget can see it.
    """
    from a2a.client import ClientConfig

    return ClientConfig(streaming=True, polling=False, httpx_client=client)


@dataclass(frozen=True, slots=True)
class ServiceResponse:
    """A peer's reply, plus the pacing it asked for."""

    status_code: int
    body: dict[str, Any]
    retry_after_s: float | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class JsonServiceClient:
    """A small JSON client for A2A-shaped hops: token attached, retries off.

    Deliberately not a protocol implementation. It exists so that the two things
    every A2A call must get right — attach the bearer token, size the timeout from
    the turn that remains — are done in one place instead of in seven agents.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str,
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
        """The transport policy, when this client built its own transport."""
        return self._profile

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        trace_id: str | None = None,
        budget: TurnBudget | None = None,
        lease: CallLease | None = None,
        timeout_ms: int | None = None,
    ) -> ServiceResponse:
        """POST ``payload``, raising :class:`UpstreamTransient` on a retryable status.

        The timeout is taken, in order of preference, from an explicit
        ``timeout_ms``, the call's ``lease``, or the turn budget's remainder —
        never from a fixed default while a turn is running.
        """
        timeout = self._resolve_timeout(budget=budget, lease=lease, timeout_ms=timeout_ms)
        now = datetime.now(UTC)
        try:
            response = await self._client.post(
                f"{self.base_url}{path.lstrip('/')}",
                json=dict(payload),
                headers=service_auth_headers(self._token, trace_id=trace_id),
                timeout=timeout,
            )
        except httpx.TransportError as error:
            raise UpstreamTransient(f"{path}: {error!r}") from error

        retry_after_s = parse_retry_after(response.headers.get("Retry-After"), now=now)
        if response.status_code == 429 or response.status_code >= 500:
            raise UpstreamTransient(
                f"{path} returned {response.status_code}",
                status_code=response.status_code,
                retry_after_s=retry_after_s,
            )
        if not 200 <= response.status_code < 300:
            raise UpstreamRejected(
                f"{path} returned {response.status_code}", status_code=response.status_code
            )
        return ServiceResponse(
            status_code=response.status_code, body=_json_body(response), retry_after_s=retry_after_s
        )

    def _resolve_timeout(
        self,
        *,
        budget: TurnBudget | None,
        lease: CallLease | None,
        timeout_ms: int | None,
    ) -> float:
        if timeout_ms is not None:
            return timeout_ms / 1000
        nominal = lease.timeout_ms if lease is not None else self._caps.http_tool_timeout_ms
        if budget is not None:
            nominal = min(nominal, max(budget.remaining_ms(), 1))
        return nominal / 1000

    async def aclose(self) -> None:
        await self._client.aclose()


def _json_body(response: httpx.Response) -> dict[str, Any]:
    try:
        parsed: Any = response.json()
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {"value": parsed}
