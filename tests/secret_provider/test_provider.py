"""WS5 S7 (ADR-0047): one SecretProvider interface, file-mounted in production, env only in fixtures.

The two failure shapes these tests exist for: a production process that quietly reads secrets from
its environment (where every crash dump and `env` listing shows them), and a rotation that either
is never picked up or invalidates everything signed a second before it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from referraleasy_secrets import (
    EnvSecretProvider,
    FileSecretProvider,
    SecretConfigurationError,
    SecretName,
    SecretNotFound,
    from_profile,
)

NAME = SecretName.JWT_PATIENT_SECRET


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _mount(directory: Path, name: SecretName, value: str, *, previous: str | None = None) -> None:
    (directory / name.value).write_text(value + "\n", encoding="utf-8")
    if previous is not None:
        (directory / f"{name.value}.previous").write_text(previous, encoding="utf-8")


@pytest.mark.parametrize("profile", ["production"])
def test_env_provider_refused_outside_demo(profile: str) -> None:
    with pytest.raises(SecretConfigurationError, match="not a fixture profile"):
        EnvSecretProvider({NAME.value: "x" * 40}, profile=profile)
    # And through the factory: asking for env explicitly in production is refused, not honoured.
    with pytest.raises(SecretConfigurationError, match="not a fixture profile"):
        from_profile({"APP_ENV": profile, "SECRET_PROVIDER": "env"})


@pytest.mark.parametrize("env", [{}, {"APP_ENV": ""}, {"APP_ENV": "  "}])
def test_unset_app_env_is_not_demo(env: dict[str, str]) -> None:
    """Negative: an unset profile must not fall back to the env provider (B1's shape, for secrets)."""
    with pytest.raises(SecretConfigurationError, match="APP_ENV is not set"):
        from_profile({**env, NAME.value: "x" * 40})


def test_unknown_profile_and_provider_are_refused() -> None:
    with pytest.raises(SecretConfigurationError, match="not a known profile"):
        from_profile({"APP_ENV": "staging"})
    with pytest.raises(SecretConfigurationError, match="not file, env or gcp"):
        from_profile({"APP_ENV": "demo", "SECRET_PROVIDER": "vault"})


@pytest.mark.parametrize(("profile", "kind"), [("demo", "env"), ("ci", "env"), ("test", "env")])
def test_fixture_profiles_default_to_env(profile: str, kind: str) -> None:
    provider = from_profile({"APP_ENV": profile, NAME.value: "demo-secret"})
    assert provider.source.startswith(kind)
    assert provider.get(NAME).get_secret_value() == "demo-secret"


def test_production_defaults_to_files(tmp_path: Path) -> None:
    _mount(tmp_path, NAME, "from-a-file")
    provider = from_profile({"APP_ENV": "production", "SECRETS_DIR": str(tmp_path)})
    assert isinstance(provider, FileSecretProvider)
    assert provider.get(NAME).get_secret_value() == "from-a-file"


def test_production_ignores_a_secret_left_in_the_environment(tmp_path: Path) -> None:
    """The environment is not a fallback: a production process with no mount fails, loudly."""
    provider = from_profile(
        {"APP_ENV": "production", "SECRETS_DIR": str(tmp_path), NAME.value: "leaked-into-env"}
    )
    with pytest.raises(SecretNotFound, match=NAME.value):
        provider.get(NAME)


def test_file_provider_rotation_reads_new_version(tmp_path: Path) -> None:
    clock = _Clock()
    provider = FileSecretProvider(tmp_path, ttl_seconds=60, clock=clock)
    _mount(tmp_path, NAME, "v1")
    assert provider.get(NAME).get_secret_value() == "v1"

    _mount(tmp_path, NAME, "v2", previous="v1")
    clock.now = 59.0
    assert provider.get(NAME).get_secret_value() == "v1", "a read inside the TTL is cached"

    clock.now = 60.0
    assert provider.get(NAME).get_secret_value() == "v2", "a rotation is seen without a restart"


def test_previous_version_is_accepted_during_the_overlap(tmp_path: Path) -> None:
    _mount(tmp_path, NAME, "v2", previous="v1")
    versions = FileSecretProvider(tmp_path).get_versions(NAME)
    assert [v.get_secret_value() for v in versions.all()] == ["v2", "v1"]
    assert versions.current.get_secret_value() == "v2"

    (tmp_path / f"{NAME.value}.previous").unlink()
    after = FileSecretProvider(tmp_path).get_versions(NAME)
    assert after.previous is None
    assert [v.get_secret_value() for v in after.all()] == ["v2"]


def test_env_provider_previous_version() -> None:
    provider = EnvSecretProvider({NAME.value: "v2", f"{NAME.value}_PREVIOUS": "v1"}, profile="demo")
    assert [v.get_secret_value() for v in provider.get_versions(NAME).all()] == ["v2", "v1"]


@pytest.mark.parametrize("value", ["", "   \n"])
def test_a_blank_secret_is_refused(tmp_path: Path, value: str) -> None:
    (tmp_path / NAME.value).write_text(value, encoding="utf-8")
    with pytest.raises(SecretNotFound, match="blank"):
        FileSecretProvider(tmp_path).get(NAME)
    with pytest.raises(SecretNotFound, match="blank"):
        EnvSecretProvider({NAME.value: value}, profile="test").get(NAME)


def test_a_secret_never_appears_in_repr_str_or_json(tmp_path: Path) -> None:
    _mount(tmp_path, NAME, "super-secret-value", previous="older-secret-value")
    provider = FileSecretProvider(tmp_path)
    versions = provider.get_versions(NAME)
    rendered = " ".join(
        [
            repr(provider),
            repr(versions),
            str(versions.current),
            json.dumps({"s": str(versions.current)}),
        ]
    )
    assert "super-secret-value" not in rendered
    assert "older-secret-value" not in rendered


def test_missing_secret_names_the_secret_not_a_value(tmp_path: Path) -> None:
    with pytest.raises(SecretNotFound) as raised:
        FileSecretProvider(tmp_path).get(SecretName.A2A_SERVICE_TOKEN)
    assert "A2A_SERVICE_TOKEN" in str(raised.value)


def test_every_platform_secret_has_a_name() -> None:
    """The enum is the inventory the rotation runbook walks; adding a secret means adding it here."""
    assert {name.value for name in SecretName} == {
        "JWT_PATIENT_SECRET",
        "JWT_ADMIN_SECRET",
        "A2A_SERVICE_TOKEN",
        "ACTOR_CONTEXT_SIGNING_KEY",
        "OPENROUTER_API_KEY",
        "LANGFUSE_SECRET_KEY",
        "DATABASE_PASSWORD",
        "TELEMETRY_PSEUDONYM_KEY",
        "EVAL_SAMPLE_SEED",
    }
