# fixture passwords, not credentials, and S105/S106 are not enabled here to say so
"""`GatewaySettings.describe()` must be loggable, and that is a property to be tested not asserted.

`describe()`'s docstring claims "no secret is included, by construction rather than by review". That
claim is only true while every field it emits is checked, and it is exactly the kind of claim that
goes stale when a new field is added by someone who reads the docstring and believes it.

The case that prompted this file: a URL's host was derived with `Path(url).name`, which for
`http://user:secret@host:9200` returns `secret@host:9200`. Every shortcut that looks safe here is
not, so the assertion is on the *output* rather than on the method.
"""

from __future__ import annotations

import pytest

from referraleasy_gateway.config import GatewaySettings


def settings(**env: str) -> GatewaySettings:
    """Settings from *environment variable* names, like `demo_settings`.

    `from_env`'s `**overrides` are dataclass field names, so passing `OPENSEARCH_URL=` there is a
    `TypeError` — which is how this file's first version failed. The distinction is worth keeping in
    mind here because the thing under test is the env contract itself.
    """
    return GatewaySettings.from_env(
        {
            "APP_ENV": "test",
            "JWT_PATIENT_SECRET": "fixture-patient-secret-0123456789abcdef",
            "JWT_ADMIN_SECRET": "fixture-admin-secret-0123456789abcdef",
            **env,
        }
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://opensearch:9200",
        "https://opensearch.example:9200/",
        "http://user:sup3rs3cret@opensearch:9200",
        "http://user@opensearch:9200/prefix",
    ],
)
def test_describe_never_emits_credentials_from_a_url(url: str) -> None:
    """The property, checked over the shapes a deployment actually uses."""
    described = settings(OPENSEARCH_URL=url).describe()

    rendered = repr(described)
    assert "sup3rs3cret" not in rendered
    assert "user" not in rendered.replace("opensearch", ""), (
        f"userinfo reached describe(): {rendered}"
    )


def test_describe_names_the_telemetry_host_when_there_is_one() -> None:
    """The useful part survives: an operator still learns which cluster is being read."""
    assert settings(OPENSEARCH_URL="http://opensearch:9200").describe()["telemetry_host"] == (
        "opensearch:9200"
    )
    # A URL with credentials still reports the host, and only the host.
    assert (
        settings(OPENSEARCH_URL="http://u:p@host:9200").describe()["telemetry_host"] == "host:9200"
    )


def test_describe_reports_no_host_when_none_is_configured() -> None:
    """Absent is `None`, not an empty string, so a log line does not read as "configured, blank"."""
    assert settings().describe()["telemetry_host"] is None
    assert settings(OPENSEARCH_URL="not a url at all").describe()["telemetry_host"] is None
