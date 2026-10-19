"""Everything a running agent resolves from its environment, in one object.

One image serves seven agents, so the environment *is* the deployment: ``AGENT_ROLE``
picks the agent, the budget caps are global, the model comes from the factory, and
the service token authenticates the A2A hop. :meth:`AgentSettings.from_env` is the
single place any of that is read, which is what lets ``main --check`` answer "what
would this container actually do?" before it serves a single request.

The settings object is also where the release-manifest entry comes from
(:meth:`AgentSettings.manifest_entry`): the role, the resolved model — including a
per-agent override, which the spec requires to be recorded rather than hidden in
the environment — and the budget caps the process will enforce.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx
from pydantic import SecretStr

from referraleasy_agent_runtime.auth import AUTH_TOKEN_ENV, BearerTokenVerifier
from referraleasy_agent_runtime.budget import BudgetCaps
from referraleasy_agent_runtime.errors import ConfigurationError
from referraleasy_agent_runtime.http import HttpProfile, build_http_client
from referraleasy_agent_runtime.model import (
    OPENROUTER_API_KEY_ENV,
    AgentModelOverride,
    ModelRef,
    resolve_model,
)
from referraleasy_agent_runtime.roles import AgentRole, role_from_env, service_name

__all__ = [
    "AGENT_PORT_ENV",
    "APP_ENV_ENV",
    "DEFAULT_AGENT_PORT",
    "AgentSettings",
    "resolve_port",
]

APP_ENV_ENV: Final = "APP_ENV"
AGENT_PORT_ENV: Final = "AGENT_PORT"
#: Compose sets `PORT` for every service; honour it so the image needs no special case.
PORT_ENV: Final = "PORT"
DEFAULT_AGENT_PORT: Final = 8080


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """The resolved configuration of one agent process."""

    role: AgentRole
    caps: BudgetCaps
    model: ModelRef
    service_token: SecretStr
    app_env: str = "local"

    @property
    def service_name(self) -> str:
        return service_name(self.role)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        role: AgentRole | str | None = None,
        overrides: Mapping[str, AgentModelOverride] | None = None,
    ) -> AgentSettings:
        """Resolve and validate everything the process needs, or fail loudly.

        A deployment mistake must surface here — as a named missing variable — and
        not as a 401 in the middle of a patient's booking.
        """
        resolved: Mapping[str, str] = env if env is not None else os.environ
        agent_role: AgentRole
        if isinstance(role, AgentRole):
            agent_role = role
        elif role is None:
            agent_role = role_from_env(resolved)
        else:
            agent_role = AgentRole(role)
        token = resolved.get(AUTH_TOKEN_ENV, "").strip()
        if not token:
            raise ConfigurationError(
                f"{AUTH_TOKEN_ENV} is not set. It authenticates service-to-service A2A calls."
            )
        model_ref = resolve_model(resolved, role=agent_role.value, overrides=overrides)
        if (
            model_ref.provider == "openrouter"
            and not resolved.get(OPENROUTER_API_KEY_ENV, "").strip()
        ):
            raise ConfigurationError(
                f"{OPENROUTER_API_KEY_ENV} is not set, so the openrouter/{model_ref.model_id} "
                "path cannot be exercised (ADR-0002)."
            )
        return cls(
            role=agent_role,
            caps=BudgetCaps.from_env(resolved),
            model=model_ref,
            service_token=SecretStr(token),
            app_env=resolved.get(APP_ENV_ENV, "local").strip() or "local",
        )

    def verifier(
        self,
        *,
        audience: str = "referraleasy-a2a",
        realm: str = "referraleasy",
        subject: str = "a2a-service",
    ) -> BearerTokenVerifier:
        """The transport-level verifier for inbound A2A calls."""
        return BearerTokenVerifier(
            self.service_token.get_secret_value(),
            audience=audience,
            realm=realm,
            subject=subject,
        )

    def http_client(self) -> tuple[httpx.AsyncClient, HttpProfile]:
        """The shared, retry-free client every outbound call in this process uses."""
        return build_http_client(self.caps)

    def manifest_entry(self) -> dict[str, Any]:
        """The release-manifest record for this role.

        Contains no credential. ``model`` carries the per-agent override flag, which
        is the point of recording it here rather than trusting the environment.
        """
        return {
            "component": self.service_name,
            "role": self.role.value,
            "app_env": self.app_env,
            "model": self.model.manifest_entry(),
            "budget": {
                "attempt_timeout_ms": self.caps.attempt_timeout_ms,
                "max_attempts": self.caps.max_attempts,
                "turn_deadline_ms": self.caps.turn_deadline_ms,
                "max_delegations_per_turn": self.caps.max_delegations,
                "max_model_calls_per_turn": self.caps.max_model_calls,
                "max_tool_calls_per_turn": self.caps.max_tool_calls,
                "http_tool_timeout_ms": self.caps.http_tool_timeout_ms,
                "fallback_timeout_ms": self.caps.fallback_timeout_ms,
                "retry_owner": "agent-runtime",
                "transport_retries": 0,
            },
        }


def resolve_port(env: Mapping[str, str] | None = None, *, default: int = DEFAULT_AGENT_PORT) -> int:
    """The port to serve on: ``AGENT_PORT``, else compose's ``PORT``, else 8080."""
    resolved: Mapping[str, str] = env if env is not None else os.environ
    for name in (AGENT_PORT_ENV, PORT_ENV):
        raw = resolved.get(name, "").strip()
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigurationError(f"{name} must be an integer port, got {raw!r}") from exc
    return default
