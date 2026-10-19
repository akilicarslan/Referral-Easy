"""The model factory: one construction, and a record of which model it actually built.

Two things are being defended here. First, the construction is *the spike-tested
one* — ``LiteLlm(model="openrouter/<vendor>/<model>")`` — and a test asserts the
real SDK object was built with exactly that string, so a refactor that changes how
LiteLlm is called cannot pass. Second, the factory returns the resolved identity
beside the model, because a per-agent override that is not visible in the release
manifest is an override nobody can audit.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from referraleasy_agent_runtime.errors import ConfigurationError
from referraleasy_agent_runtime.model import (
    AGENT_MODEL_OVERRIDES_ENV,
    AGENT_MODEL_PARAMS_ENV,
    DEFAULT_MODEL_PROVIDER,
    OPENROUTER_API_KEY_ENV,
    SUPPORTED_PROVIDERS,
    AgentModelOverride,
    ModelRef,
    ModelSource,
    build_model,
    parse_overrides,
    resolve_model,
)

SPIKE_MODEL_ID = "anthropic/claude-haiku-5.5"


def test_the_environment_resolves_to_the_proven_pair(env: dict[str, str]) -> None:
    ref = resolve_model(env, role="scheduling")

    assert ref.provider == "openrouter"
    assert ref.provider in SUPPORTED_PROVIDERS
    assert ref.model_id == SPIKE_MODEL_ID
    assert ref.litellm_model == f"openrouter/{SPIKE_MODEL_ID}"
    assert ref.role == "scheduling"
    assert ref.source is ModelSource.ENVIRONMENT


def test_the_default_provider_is_openrouter_when_unset(env: dict[str, str]) -> None:
    env.pop("AGENT_MODEL_PROVIDER")

    assert resolve_model(env).provider == DEFAULT_MODEL_PROVIDER


def test_the_provider_prefix_is_applied_exactly_once() -> None:
    """`AGENT_MODEL_ID` may be written either way; the model string must not double it."""
    bare = ModelRef(provider="openrouter", model_id=SPIKE_MODEL_ID, params={})
    prefixed = ModelRef(provider="openrouter", model_id=f"openrouter/{SPIKE_MODEL_ID}", params={})

    assert bare.litellm_model == prefixed.litellm_model == f"openrouter/{SPIKE_MODEL_ID}"


def test_an_unsupported_provider_fails_loudly(env: dict[str, str]) -> None:
    env["AGENT_MODEL_PROVIDER"] = "vertex"

    with pytest.raises(ConfigurationError, match="vertex"):
        resolve_model(env)


def test_a_missing_model_id_fails_at_startup_not_mid_turn(env: dict[str, str]) -> None:
    env.pop("AGENT_MODEL_ID")

    with pytest.raises(ConfigurationError, match="AGENT_MODEL_ID"):
        resolve_model(env)


def test_a_per_agent_override_wins_and_is_recorded(env: dict[str, str]) -> None:
    env[AGENT_MODEL_OVERRIDES_ENV] = json.dumps(
        {"scheduling": {"model_id": "anthropic/claude-sonnet-5", "params": {"temperature": 0}}}
    )

    overridden = resolve_model(env, role="scheduling")
    untouched = resolve_model(env, role="referral_intake")

    assert overridden.model_id == "anthropic/claude-sonnet-5"
    assert overridden.source is ModelSource.AGENT_OVERRIDE
    assert overridden.params["temperature"] == 0

    assert untouched.model_id == SPIKE_MODEL_ID
    assert untouched.source is ModelSource.ENVIRONMENT

    entry = overridden.manifest_entry()
    assert entry["component"] == "scheduling"
    assert entry["model_id"] == "anthropic/claude-sonnet-5"
    assert entry["overridden"] is True
    assert entry["source"] == "agent_override"
    assert untouched.manifest_entry()["overridden"] is False


def test_an_override_can_name_its_own_provider(env: dict[str, str]) -> None:
    overrides = parse_overrides(
        json.dumps({"scheduling": {"model_id": SPIKE_MODEL_ID, "provider": "openrouter"}})
    )

    assert resolve_model(env, role="scheduling", overrides=overrides).provider == "openrouter"


def test_an_override_inherits_the_environment_parameters(env: dict[str, str]) -> None:
    """An override that sets nothing but the model id must not silently drop the params."""
    env[AGENT_MODEL_PARAMS_ENV] = json.dumps({"temperature": 0.2, "top_p": 0.9})
    env[AGENT_MODEL_OVERRIDES_ENV] = json.dumps({"scheduling": {"model_id": "vendor/model"}})

    ref = resolve_model(env, role="scheduling")

    assert ref.params == {"temperature": 0.2, "top_p": 0.9}


def test_a_malformed_override_is_a_configuration_error(env: dict[str, str]) -> None:
    env[AGENT_MODEL_OVERRIDES_ENV] = json.dumps({"scheduling": {"model": "vendor/model"}})

    with pytest.raises(ConfigurationError, match="scheduling"):
        parse_overrides(env[AGENT_MODEL_OVERRIDES_ENV])

    with pytest.raises(ConfigurationError, match=AGENT_MODEL_OVERRIDES_ENV):
        parse_overrides("not json")


def test_an_absent_override_map_is_empty_not_an_error(env: dict[str, str]) -> None:
    assert dict(parse_overrides("")) == {}
    assert dict(parse_overrides("   ")) == {}
    assert resolve_model(env, role="scheduling").source is ModelSource.ENVIRONMENT


def test_the_override_model_is_typed_and_closed() -> None:
    override = AgentModelOverride(model_id="vendor/model")

    assert override.params == {}
    assert override.provider is None

    # `extra="forbid"`: a credential that has no business in a manifest is rejected
    # rather than silently recorded.
    with pytest.raises(ValidationError):
        AgentModelOverride(model_id="vendor/model", api_key="sk-should-not-be-here")


def test_a_missing_key_fails_before_any_model_is_constructed(env: dict[str, str]) -> None:
    env.pop(OPENROUTER_API_KEY_ENV)

    with pytest.raises(ConfigurationError, match=OPENROUTER_API_KEY_ENV):
        build_model(env, role="scheduling")


def test_the_built_model_is_the_spike_tested_construction(env: dict[str, str]) -> None:
    """The load-bearing assertion: the *real* LiteLlm object, built the proven way."""
    model, ref = build_model(env, role="care_coordinator")

    assert model is not None
    assert ref.litellm_model == f"openrouter/{SPIKE_MODEL_ID}"
    assert model.model == f"openrouter/{SPIKE_MODEL_ID}", "LiteLlm must receive the qualified id"
    assert type(model).__name__ == "LiteLlm"


def test_the_secret_never_reaches_the_reference_or_the_manifest(env: dict[str, str]) -> None:
    secret = "sk-or-v1-not-a-real-credential"
    env[OPENROUTER_API_KEY_ENV] = secret

    _, ref = build_model(env, role="scheduling")

    assert secret not in json.dumps(ref.manifest_entry())
    assert secret not in repr(ref)
    assert secret not in repr(resolve_model(env, role="scheduling"))


@pytest.mark.parametrize(
    "role",
    [
        "care_coordinator",
        "referral_intake",
        "coverage_network",
        "provider_matching",
        "referral_readiness",
        "scheduling",
        "recovery_handoff",
    ],
)
def test_all_seven_roles_resolve_the_same_model_by_default(role: str, env: dict[str, str]) -> None:
    """One image, seven roles: the role changes the prompt and the tools, not the model."""
    ref = resolve_model(env, role=role)

    assert ref.litellm_model == f"openrouter/{SPIKE_MODEL_ID}"
    assert ref.manifest_entry()["component"] == role
