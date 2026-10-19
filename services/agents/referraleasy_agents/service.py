"""Wiring: settings to a running service, with the policy authority attached.

This module is the only place that decides *what a process talks to*: the provider
API, the database that answers ``policy_assert``, and its peers. Everything else —
the turn, the tools, the budget — is already decided by the runtime and `handler.py`.

Two connections, and both are deliberate:

* **the provider API** (``MOCK_API_URL``) — where every retrieval lands. One
  `DirectoryApi`, retries off, token attached by the runtime.
* **the policy database** (``DATABASE_URL``) — where ``role x tool x workflow state``
  is answered. The runtime's gate is synchronous, and so is its `SqlPolicyBackend`
  seam, so the connection here is a synchronous psycopg connection used for one
  indexed ``SELECT policy_assert(...)`` per tool call. That is one blocking round
  trip inside the event loop per tool call, and it is **stated as a limitation**
  rather than hidden: moving it behind a worker thread or an async adapter changes
  the runtime's interface, which is out of this package's scope.

**No `DATABASE_URL` means no policy backend, and no policy backend means the gate
refuses every tool call** (``PolicyUnavailable``). That is the runtime's default-deny
rule applied to a deployment mistake: a service that cannot reach its authority does
not get to act on a guess.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any, Final

import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from referraleasy_agent_runtime import (
    AgentRole,
    AgentSettings,
    BudgetCaps,
    ConfigurationError,
    PolicyBackend,
    SqlPolicyBackend,
    create_app,
)
from referraleasy_agent_runtime import (
    main as runtime_main,
)
from referraleasy_agents.errors import UnknownDataError
from referraleasy_agents.handler import AgentService, RoleHandler
from referraleasy_agents.tools.call import Api, DelegationPort
from referraleasy_agents.tools.client import DirectoryApi
from referraleasy_contracts import AgentTaskResult

__all__ = [
    "A2A_BASE_URL_ENV",
    "DATABASE_URL_ENV",
    "DEFAULT_A2A_BASE_URL",
    "DEFAULT_MOCK_API_URL",
    "MOCK_API_URL_ENV",
    "HttpDelegation",
    "asgi_app",
    "build_api",
    "build_app",
    "build_service",
    "main",
]

#: Where the provider API is. Named as the rest of the repository names it.
MOCK_API_URL_ENV: Final = "MOCK_API_URL"
DEFAULT_MOCK_API_URL: Final = "http://localhost:4010"

#: The control-state database, which answers `policy_assert`. Same variable name the
#: gateway and the workers use.
DATABASE_URL_ENV: Final = "DATABASE_URL"

#: Where a peer specialist listens. ``{role}`` is replaced by the role's own value, so
#: the default matches `service_name(role)` — ``agent-referral_intake`` — which is what
#: the runtime names a deployment. One variable, because seven URLs in an environment
#: are seven chances to disagree.
A2A_BASE_URL_ENV: Final = "A2A_BASE_URL"
DEFAULT_A2A_BASE_URL: Final = "http://{role}:8080"


def _resolved(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def build_api(settings: AgentSettings, *, env: Mapping[str, str] | None = None) -> DirectoryApi:
    """The provider-API transport for this process."""
    resolved = _resolved(env)
    return DirectoryApi(
        resolved.get(MOCK_API_URL_ENV, DEFAULT_MOCK_API_URL),
        token=settings.service_token.get_secret_value(),
        caps=settings.caps,
    )


def build_policy(
    *, env: Mapping[str, str] | None = None, connect: Any = None
) -> PolicyBackend | None:
    """The `policy_assert` authority, or ``None`` when no database is configured.

    ``connect`` is a seam for a test: a callable returning anything with
    ``execute(sql, params)``. Without it, psycopg is imported lazily so a deployment
    that has no database (and therefore no tool authority) still starts and refuses
    work with a readable reason.
    """
    resolved = _resolved(env)
    dsn = resolved.get(DATABASE_URL_ENV, "").strip()
    if not dsn and connect is None:
        return None

    if connect is None:

        def connect() -> Any:
            import psycopg

            return psycopg.connect(dsn, autocommit=True, connect_timeout=5)

    connection: Any | None = None

    def execute(sql: str, params: tuple[str, str, str]) -> None:
        # One connection, opened on first use and reused: `policy_assert` is a single
        # indexed read, and reconnecting per tool call would cost more than the query.
        nonlocal connection
        if connection is None or getattr(connection, "closed", False):
            connection = connect()
        connection.execute(sql, params)

    return SqlPolicyBackend(execute)


class HttpDelegation:
    """A `DelegationPort` over HTTP, for the coordinator's `delegate_task` tool.

    The child request travels as the JSON body of ``POST /tasks`` on the target
    service, with the service token attached by the runtime's `JsonServiceClient`.
    The timeout comes from the *child's* budget, which shares the parent's absolute
    deadline (``TurnBudget.delegation``), so a delegation can never outlive the turn
    that made it — no fixed client timeout could make that guarantee.

    A URL per role, resolved from one pattern, and a `client` seam so a test can
    assert the request shape without a socket.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        caps: BudgetCaps,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._pattern = base_url
        self._token = token
        self._caps = caps
        self._client = client

    def url_for(self, role: AgentRole) -> str:
        return self._pattern.replace("{role}", role.value).rstrip("/")

    async def send(
        self,
        *,
        target_role: AgentRole,
        request: Mapping[str, Any],
        budget: Any,
    ) -> Mapping[str, Any]:
        """POST the child envelope and return the peer's JSON body."""
        from referraleasy_agent_runtime import JsonServiceClient

        client = JsonServiceClient(
            self.url_for(target_role),
            token=self._token,
            caps=self._caps,
            client=self._client,
        )
        response = await client.post_json("/tasks", request, budget=budget)
        if not response.ok:
            # A task that the peer *refused* is not this shape: the runtime's `/tasks`
            # answers 200 with a shaped envelope whose outcome says `rejected`, because
            # a refusal is a protocol answer. A non-2xx here is therefore the transport
            # or the addressing being wrong — the request was understood and rejected,
            # which is `UpstreamRejected` and not a transient failure to retry.
            from referraleasy_agent_runtime import UpstreamRejected

            raise UpstreamRejected(
                f"{target_role.value} answered {response.status_code} to a delegated task; "
                "the peer's task route answers 200 with a shaped envelope even when it "
                "refuses, so this is the hop itself.",
                status_code=response.status_code,
            )
        result = response.body.get("result")
        if request.get("payload", {}).get("execution_mode") == "workflow":
            usage = response.body.get("trace", {}).get("budget")
            if not isinstance(usage, Mapping):
                raise UnknownDataError("The peer returned no budget usage.")
            budget.account_remote(usage)
        if not isinstance(result, Mapping):
            raise UnknownDataError(f"{target_role.value} returned no task result envelope")
        try:
            projection = AgentTaskResult.model_validate(
                {
                    "outcome": result.get("outcome"),
                    "event": result.get("event"),
                    "a2a_state": result.get("a2a_state"),
                }
            )
        except ValidationError as error:
            raise UnknownDataError(
                f"{target_role.value} returned an invalid task result"
            ) from error
        if result.get("is_success") is not projection.is_success:
            raise UnknownDataError(f"{target_role.value} returned inconsistent success flags")
        return {**result, "state": projection.a2a_state.value}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def build_delegation(
    settings: AgentSettings, *, env: Mapping[str, str] | None = None
) -> DelegationPort | None:
    """The delegation port, unless this role has nothing to delegate to.

    The coordinator is the only role whose allowlist contains `delegate_task`, so the
    transport is only built for it — a port every agent holds is a port any agent can
    grow a use for.
    """
    from referraleasy_agent_runtime import allowed_tools

    if "delegate_task" not in allowed_tools(settings.role):
        return None
    resolved = _resolved(env)
    return HttpDelegation(
        base_url=resolved.get(A2A_BASE_URL_ENV, DEFAULT_A2A_BASE_URL),
        token=settings.service_token.get_secret_value(),
        caps=settings.caps,
    )


def build_service(
    settings: AgentSettings,
    *,
    env: Mapping[str, str] | None = None,
    api: Api | None = None,
    policy: PolicyBackend | None = None,
    delegation: DelegationPort | None = None,
    **overrides: Any,
) -> AgentService:
    """This role's `AgentService`: settings resolved, seams replaceable.

    A caller that passes ``policy=None`` and a ``DATABASE_URL`` unset gets a service
    whose tool calls are all refused, which is the intended reading of "the authority
    is not available" — not a service that acts anyway.
    """
    resolved = _resolved(env)
    return AgentService(
        role=settings.role,
        api=api if api is not None else build_api(settings, env=resolved),
        policy=policy if policy is not None else build_policy(env=resolved),
        delegation=delegation
        if delegation is not None
        else build_delegation(settings, env=resolved),
        env=resolved,
        **overrides,
    )


def build_app(
    settings: AgentSettings, *, env: Mapping[str, str] | None = None, **overrides: Any
) -> FastAPI:
    """The ASGI app for one role, with its handler wired.

    A wiring failure is surfaced through `ConfigurationError`, not swallowed: an
    exception here means the process serves nothing, and a container that refuses to
    start is easier to diagnose than one that answers every task with a refusal.
    """
    service = build_service(settings, env=env, **overrides)
    return create_app(settings, handler=RoleHandler(service))


def asgi_app() -> FastAPI:
    """The factory an ASGI server starts: ``uvicorn ...:asgi_app --factory``.

    Handler-less, like the runtime's own: a container that boots without a database
    or a provider API still reports its role and its manifest, which is how a wiring
    mistake is seen rather than guessed at.
    """
    settings = AgentSettings.from_env()
    try:
        return build_app(settings)
    except ConfigurationError:
        return create_app(settings)


def main(
    argv: Sequence[str] | None = None,
    *,
    role: AgentRole | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Serve one role, optionally pinning it for a single-role service image.

    ``role`` is a *default*, not an override: the environment still wins, because the
    image is one image and ``AGENT_ROLE`` is how the runtime's own deployment picks a
    role. A service directory that pins a role only means its container needs one
    variable fewer to be correct.
    """
    resolved = dict(_resolved(env))
    if role is not None:
        resolved.setdefault("AGENT_ROLE", role.value)
    return runtime_main(
        argv,
        handler_factory=lambda settings: RoleHandler(build_service(settings, env=resolved)),
        env=resolved,
    )
