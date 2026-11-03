"""The `SecretProvider` interface and its three implementations."""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import SecretStr

__all__ = [
    "FIXTURE_PROFILES",
    "EnvSecretProvider",
    "FileSecretProvider",
    "GcpSecretManagerProvider",
    "SecretConfigurationError",
    "SecretName",
    "SecretNotFound",
    "SecretProvider",
    "SecretVersions",
    "from_profile",
]


class SecretName(StrEnum):
    """Every secret the platform reads. A secret with no name here is a secret nobody rotates."""

    JWT_PATIENT_SECRET = "JWT_PATIENT_SECRET"
    JWT_ADMIN_SECRET = "JWT_ADMIN_SECRET"
    A2A_SERVICE_TOKEN = "A2A_SERVICE_TOKEN"
    #: WS1: the ActorContext signing key (gateway only).
    ACTOR_CONTEXT_SIGNING_KEY = "ACTOR_CONTEXT_SIGNING_KEY"
    OPENROUTER_API_KEY = "OPENROUTER_API_KEY"
    LANGFUSE_SECRET_KEY = "LANGFUSE_SECRET_KEY"
    DATABASE_PASSWORD = "DATABASE_PASSWORD"
    #: WS5 S4: the HMAC key telemetry pseudonyms are computed under.
    TELEMETRY_PSEUDONYM_KEY = "TELEMETRY_PSEUDONYM_KEY"
    #: WS8: the seed the online eval sampler draws with, so a sample cannot be predicted.
    EVAL_SAMPLE_SEED = "EVAL_SAMPLE_SEED"


#: The profiles in which environment-variable secrets are acceptable. Same set as the gateway's
#: fixture profiles (`referraleasy_gateway.config.FIXTURE_PROFILES`); `ci` is an alias for `demo`.
FIXTURE_PROFILES: Final = frozenset({"demo", "test"})
_PROFILE_ALIASES: Final = {"ci": "demo"}
_PROFILES: Final = frozenset({"production"}) | FIXTURE_PROFILES

#: Where compose `secrets:` and a Cloud Run volume mount put files, unless `SECRETS_DIR` says
#: otherwise.
DEFAULT_SECRETS_DIR: Final = Path("/run/secrets")

#: How long a file read is reused. Short enough that a rotated volume is picked up within a minute,
#: long enough that a hot path does not stat a file per request.
DEFAULT_TTL_SECONDS: Final = 60.0

_PREVIOUS_SUFFIX: Final = ".previous"


class SecretConfigurationError(RuntimeError):
    """The environment does not describe a usable secret source. Raised at startup."""


class SecretNotFound(LookupError):
    """A named secret is not present in the configured source. The message names it, never a value."""


@dataclass(frozen=True, slots=True)
class SecretVersions:
    """The current value and, during a rotation's overlap, the one it replaced."""

    current: SecretStr
    previous: SecretStr | None = None

    def all(self) -> tuple[SecretStr, ...]:
        """Every value a verifier should accept, current first."""
        return (self.current,) if self.previous is None else (self.current, self.previous)


class SecretProvider(ABC):
    """Read a named secret. Implementations never log, print or `repr` a value."""

    #: Short, loggable description of where secrets come from (`file:/run/secrets`), never a value.
    source: str = ""

    def get(self, name: SecretName) -> SecretStr:
        return self.get_versions(name).current

    @abstractmethod
    def get_versions(self, name: SecretName) -> SecretVersions: ...

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.source})"


def _non_blank(name: SecretName, raw: str | None, *, where: str) -> SecretStr | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        raise SecretNotFound(f"{name} is present in {where} but blank; a blank secret is no secret")
    return SecretStr(value)


class FileSecretProvider(SecretProvider):
    """One file per secret: `<dir>/<NAME>`, and `<dir>/<NAME>.previous` during a rotation.

    This is the production provider. Reads are cached for `ttl_seconds` so a rotation that replaces
    the file is seen within that window without a restart.
    """

    def __init__(
        self,
        directory: Path = DEFAULT_SECRETS_DIR,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.directory = Path(directory)
        self.source = f"file:{self.directory}"
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[SecretName, tuple[float, SecretVersions]] = {}

    def get_versions(self, name: SecretName) -> SecretVersions:
        now = self._clock()
        cached = self._cache.get(name)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        current = _non_blank(name, self._read(name.value), where=self.source)
        if current is None:
            raise SecretNotFound(f"{name} is not mounted at {self.directory / name.value}")
        previous = _non_blank(
            name, self._read(name.value + _PREVIOUS_SUFFIX), where=f"{self.source} (previous)"
        )
        versions = SecretVersions(current=current, previous=previous)
        self._cache[name] = (now, versions)
        return versions

    def _read(self, filename: str) -> str | None:
        path = self.directory / filename
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None


class EnvSecretProvider(SecretProvider):
    """Secrets from environment variables. The demo's fixture path, and refused anywhere else.

    `<NAME>_PREVIOUS` is the previous version, so rotation can be rehearsed in the demo as well.
    """

    def __init__(self, env: Mapping[str, str], *, profile: str) -> None:
        if profile not in FIXTURE_PROFILES:
            raise SecretConfigurationError(
                f"environment-variable secrets are a fixture convenience and APP_ENV={profile} is "
                "not a fixture profile; mount secrets as files (SECRET_PROVIDER=file)"
            )
        self._env = env
        self.source = f"env:{profile}"

    def get_versions(self, name: SecretName) -> SecretVersions:
        current = _non_blank(name, self._env.get(name.value), where="the environment")
        if current is None:
            raise SecretNotFound(f"{name} is not set in the environment")
        previous = _non_blank(
            name, self._env.get(f"{name.value}_PREVIOUS"), where="the environment (previous)"
        )
        return SecretVersions(current=current, previous=previous)


class GcpSecretManagerProvider(SecretProvider):
    """Secret Manager over its API. A sketch: production mounts volumes and uses `FileSecretProvider`.

    Kept for jobs that cannot mount a volume. Secret ids are the `SecretName` values in lower-kebab
    case; the project comes from configuration (`GOOGLE_CLOUD_PROJECT`), never from this repository.
    """

    def __init__(self, project: str) -> None:
        if not project.strip():
            raise SecretConfigurationError("GcpSecretManagerProvider needs GOOGLE_CLOUD_PROJECT")
        try:
            from google.cloud import secretmanager  # type: ignore[import-untyped,import-not-found,unused-ignore]
        except ImportError as error:  # pragma: no cover - optional extra
            raise SecretConfigurationError(
                "SECRET_PROVIDER=gcp needs the `gcp` extra (google-cloud-secret-manager)"
            ) from error
        self._client = secretmanager.SecretManagerServiceClient()
        self._project = project.strip()
        self.source = "gcp-secret-manager"

    def get_versions(self, name: SecretName) -> SecretVersions:  # pragma: no cover - needs GCP
        secret_id = name.value.lower().replace("_", "-")
        base = f"projects/{self._project}/secrets/{secret_id}/versions"
        current = self._access(f"{base}/latest")
        if current is None:
            raise SecretNotFound(f"{name} has no enabled version in Secret Manager")
        return SecretVersions(current=current)

    def _access(self, version: str) -> SecretStr | None:  # pragma: no cover - needs GCP
        try:
            response = self._client.access_secret_version(name=version)
        except Exception:
            return None
        return SecretStr(response.payload.data.decode("utf-8").strip())


def _profile(env: Mapping[str, str]) -> str:
    raw = env.get("APP_ENV", "").strip().lower()
    if not raw:
        raise SecretConfigurationError(
            "APP_ENV is not set; the secret source depends on the profile and an unset profile is "
            "not the demo (WS1 S1, WS5 S7)"
        )
    profile = _PROFILE_ALIASES.get(raw, raw)
    if profile not in _PROFILES:
        raise SecretConfigurationError(f"APP_ENV={raw!r} is not a known profile")
    return profile


def from_profile(env: Mapping[str, str] | None = None) -> SecretProvider:
    """The provider this process should use. Fails closed.

    `SECRET_PROVIDER` chooses explicitly (`file`, `env`, `gcp`). Without it, a fixture profile reads
    the environment and production reads files from `SECRETS_DIR` (default `/run/secrets`).
    """
    source = os.environ if env is None else env
    profile = _profile(source)
    explicit = source.get("SECRET_PROVIDER", "").strip().lower()
    kind = explicit or ("env" if profile in FIXTURE_PROFILES else "file")
    if kind == "env":
        return EnvSecretProvider(source, profile=profile)
    if kind == "file":
        directory = source.get("SECRETS_DIR", "").strip()
        return FileSecretProvider(Path(directory) if directory else DEFAULT_SECRETS_DIR)
    if kind == "gcp":
        return GcpSecretManagerProvider(source.get("GOOGLE_CLOUD_PROJECT", ""))
    raise SecretConfigurationError(f"SECRET_PROVIDER={explicit!r} is not file, env or gcp")
