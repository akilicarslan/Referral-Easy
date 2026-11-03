"""Secrets behind one interface (WS5 S7, ADR-0047).

Every secret the platform holds has a `SecretName`, and every reader asks a `SecretProvider` for it.
The production path is **files**: compose `secrets:` and Cloud Run Secret Manager *volume* mounts
both present a secret as a file, so the code that reads one is the same on a laptop and in GCP, and
a volume read picks up a rotated version without a redeploy. Plain environment variables are the
demo's fixture path and are refused anywhere else.

Two properties hold for every provider:

* **A secret is a `SecretStr`.** Its `repr`, `str` and JSON form are masked; reading the value is an
  explicit `get_secret_value()` at the one place that needs it.
* **Rotation has an overlap.** `get_versions(name)` returns the current value and, when one is
  mounted, the previous one. A verifier accepts both while a signer uses only the current one, so a
  rotation never invalidates a token that was valid a second ago.

`from_profile()` chooses the provider from `APP_ENV` and `SECRET_PROVIDER` and fails closed: an
unset profile is not the demo.
"""

from referraleasy_secrets.provider import (
    FIXTURE_PROFILES,
    EnvSecretProvider,
    FileSecretProvider,
    GcpSecretManagerProvider,
    SecretConfigurationError,
    SecretName,
    SecretNotFound,
    SecretProvider,
    SecretVersions,
    from_profile,
)

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
