"""The model factory: one constructor, one proven provider path.

ADR-0002 settled this. The OpenRouter path was spike-tested in exactly the
construction used below — ``LiteLlm(model="openrouter/<vendor>/<model>")`` — for
tool calling, structured output, streaming and cancellation. Nothing here
special-cases the provider beyond that prefix: ADK delegates to LiteLLM, LiteLLM
owns the ``openrouter/`` provider prefix, and LiteLLM reads ``OPENROUTER_API_KEY``
itself, so this module never touches the credential.

Why the factory returns more than a model
-----------------------------------------
The spec requires a per-agent model override to be **recorded in the release
manifest**, not hidden in an environment variable. A factory that returned only a
model would make that impossible: the caller would have no way to learn which
model it actually got. So :func:`build_model` returns ``(model, ref)`` and
:meth:`ModelRef.manifest_entry` is the recordable form — provider, model id,
parameters and whether the value came from an override.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from referraleasy_agent_runtime.errors import ConfigurationError

if TYPE_CHECKING:
    # Imported for typing only: importing google.adk costs ~1s and drags in the
    # whole model stack, so it is loaded lazily inside `build_model`.
    from google.adk.models.lite_llm import LiteLlm

__all__ = [
    "AGENT_MODEL_ID_ENV",
    "AGENT_MODEL_OVERRIDES_ENV",
    "AGENT_MODEL_PARAMS_ENV",
    "AGENT_MODEL_PROVIDER_ENV",
    "DEFAULT_MODEL_PROVIDER",
    "OPENROUTER_API_KEY_ENV",
    "SUPPORTED_PROVIDERS",
    "AgentModelOverride",
    "ModelRef",
    "ModelSource",
    "build_model",
    "parse_overrides",
    "resolve_model",
]

AGENT_MODEL_PROVIDER_ENV: Final = "AGENT_MODEL_PROVIDER"
AGENT_MODEL_ID_ENV: Final = "AGENT_MODEL_ID"
AGENT_MODEL_PARAMS_ENV: Final = "AGENT_MODEL_PARAMS"
#: JSON object: {"<role>": {"model_id": "...", "provider": "...", "params": {...}}}
AGENT_MODEL_OVERRIDES_ENV: Final = "AGENT_MODEL_OVERRIDES"
OPENROUTER_API_KEY_ENV: Final = "OPENROUTER_API_KEY"

DEFAULT_MODEL_PROVIDER: Final = "openrouter"

#: Providers this factory can construct. Anything else fails loudly at startup:
#: guessing a provider path would present an unverified construction as working.
SUPPORTED_PROVIDERS: Final[frozenset[str]] = frozenset({"openrouter"})

#: LiteLLM's provider prefix, and the reason `AGENT_MODEL_ID` stays a bare model id.
_LITELLM_PREFIX: Final[Mapping[str, str]] = MappingProxyType({"openrouter": "openrouter/"})


class ModelSource(StrEnum):
    """Where the resolved model came from — a manifest must be able to tell."""

    ENVIRONMENT = "environment"
    AGENT_OVERRIDE = "agent_override"


class AgentModelOverride(BaseModel):
    """A per-agent model override, as recorded in a release manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    provider: str | None = None
    params: Mapping[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelRef:
    """The resolved model identity, safe to log and safe to record.

    Contains no credential: the key is LiteLLM's to read from the environment.
    """

    provider: str
    model_id: str
    params: Mapping[str, Any]
    role: str | None = None
    source: ModelSource = ModelSource.ENVIRONMENT

    @property
    def litellm_model(self) -> str:
        """The string handed to ``LiteLlm`` — provider-qualified exactly once.

        ``AGENT_MODEL_ID`` is stored bare (``anthropic/claude-haiku-5.5``); the
        prefix is added here, and a value that already carries it is left alone so
        the two spellings cannot produce ``openrouter/openrouter/...``.
        """
        prefix = _LITELLM_PREFIX[self.provider]
        return self.model_id if self.model_id.startswith(prefix) else f"{prefix}{self.model_id}"

    def manifest_entry(self) -> dict[str, Any]:
        """The recordable form: what the release manifest stores for this component.

        A per-agent override must be visible in the manifest, so the entry states
        both the resolved triple and the fact that it is an override.
        """
        return {
            "component": self.role or "agent-runtime-default",
            "provider": self.provider,
            "model_id": self.model_id,
            "params": dict(self.params),
            "source": self.source.value,
            "overridden": self.source is ModelSource.AGENT_OVERRIDE,
        }


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigurationError(
            f"{name} is not set. It is the model id the agents run on (see ADR-0002)."
        )
    return value


def _provider(env: Mapping[str, str], *, override: str | None = None) -> str:
    provider = (
        override or env.get(AGENT_MODEL_PROVIDER_ENV, "").strip() or DEFAULT_MODEL_PROVIDER
    ).lower()
    if provider not in SUPPORTED_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_PROVIDERS))
        raise ConfigurationError(
            f"model provider {provider!r} is not implemented by this factory "
            f"(supported: {supported}; see ADR-0002 for OpenRouter and ADR-0009 for the "
            "Vertex path, which lands in a later phase)."
        )
    return provider


def _json_object(raw: str, *, name: str) -> Mapping[str, Any]:
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{name} must be a JSON object, got {raw!r}") from exc
    if not isinstance(parsed, dict):
        raise ConfigurationError(f"{name} must be a JSON object, got {type(parsed).__name__}")
    return MappingProxyType({str(key): value for key, value in parsed.items()})


def _env_params(env: Mapping[str, str]) -> Mapping[str, Any]:
    raw = env.get(AGENT_MODEL_PARAMS_ENV, "").strip()
    if not raw:
        return MappingProxyType({})
    return _json_object(raw, name=AGENT_MODEL_PARAMS_ENV)


def parse_overrides(raw: str) -> Mapping[str, AgentModelOverride]:
    """Parse the ``AGENT_MODEL_OVERRIDES`` JSON escape hatch into typed overrides."""
    if not raw.strip():
        return MappingProxyType({})
    payload = _json_object(raw, name=AGENT_MODEL_OVERRIDES_ENV)
    overrides: dict[str, AgentModelOverride] = {}
    for role, value in payload.items():
        try:
            overrides[role] = AgentModelOverride.model_validate(value)
        except ValidationError as exc:
            raise ConfigurationError(
                f"{AGENT_MODEL_OVERRIDES_ENV}[{role!r}] is not a valid model override: {exc}"
            ) from exc
    return MappingProxyType(overrides)


def resolve_model(
    env: Mapping[str, str],
    *,
    role: str | None = None,
    overrides: Mapping[str, AgentModelOverride] | None = None,
) -> ModelRef:
    """Resolve ``(provider, model_id, params)`` for ``role`` without importing ADK.

    Resolution is deliberately pure: it reads the environment, applies an
    override if one names this role, and returns a value. Tests, release manifests
    and ``main() --check`` all use it without constructing a model.
    """
    candidate = (
        overrides
        if overrides is not None
        else parse_overrides(env.get(AGENT_MODEL_OVERRIDES_ENV, ""))
    )
    override = candidate.get(role) if role is not None else None
    if override is not None:
        return ModelRef(
            provider=_provider(env, override=override.provider),
            model_id=override.model_id.strip(),
            params=MappingProxyType({**_env_params(env), **dict(override.params)}),
            role=role,
            source=ModelSource.AGENT_OVERRIDE,
        )
    return ModelRef(
        provider=_provider(env),
        model_id=_required(env, AGENT_MODEL_ID_ENV),
        params=_env_params(env),
        role=role,
        source=ModelSource.ENVIRONMENT,
    )


def build_model(
    env: Mapping[str, str],
    *,
    role: str | None = None,
    overrides: Mapping[str, AgentModelOverride] | None = None,
) -> tuple[LiteLlm, ModelRef]:
    """Build the ADK model for ``role``, and return the recordable reference beside it.

    The credential is checked for *presence* and never read further: LiteLLM reads
    ``OPENROUTER_API_KEY`` itself, and a missing key fails here at startup rather
    than as a confusing 401 inside a turn.
    """
    ref = resolve_model(env, role=role, overrides=overrides)
    if ref.provider == "openrouter" and not env.get(OPENROUTER_API_KEY_ENV, "").strip():
        raise ConfigurationError(
            f"{OPENROUTER_API_KEY_ENV} is not set. LiteLLM reads it directly for the "
            f"openrouter/{ref.model_id} path (ADR-0002)."
        )

    from google.adk.models.lite_llm import LiteLlm  # imported here on purpose: see TYPE_CHECKING

    return LiteLlm(model=ref.litellm_model, **dict(ref.params)), ref
