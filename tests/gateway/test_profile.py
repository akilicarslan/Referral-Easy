"""WS1 S1 (ADR-0043): the deployment profile has no default, and the fixture profile stays on a laptop.

Release blocker B1 was a default: `APP_ENV` fell back to `demo`, and `demo` registers routes that mint
admin sessions for anyone who asks. These tests pin the three halves of the fix — a missing or unknown
profile refuses to start, the fixture profile refuses to start anywhere that looks deployed, and every
answer names the profile that produced it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from gateway_fixtures import SPEC_ENV

from referraleasy_gateway.app import create_app
from referraleasy_gateway.config import ConfigurationError, GatewaySettings

SECRETS = {key: value for key, value in SPEC_ENV.items() if key != "APP_ENV"}


def test_missing_app_env_refuses_start() -> None:
    with pytest.raises(ConfigurationError, match="APP_ENV is not set"):
        GatewaySettings.from_env({})
    # Real secrets do not change the answer: the profile is required in its own right.
    with pytest.raises(ConfigurationError, match="APP_ENV is not set"):
        GatewaySettings.from_env(SECRETS)


@pytest.mark.parametrize("value", ["staging", "prod", "DEMO-ish", " "])
def test_unknown_app_env_refuses_start(value: str) -> None:
    with pytest.raises(ConfigurationError):
        GatewaySettings.from_env({**SECRETS, "APP_ENV": value})


def test_ci_is_an_alias_for_the_demo_profile() -> None:
    settings = GatewaySettings.from_env({**SECRETS, "APP_ENV": "ci"})
    assert settings.app_env == "demo"
    assert settings.is_demo


def test_a_settings_object_built_by_hand_is_production() -> None:
    """The dataclass default is the safe profile, so omission never registers fixture routes."""
    settings = GatewaySettings(patient_secret="p" * 40, admin_secret="a" * 40)
    assert settings.app_env == "production"
    assert settings.is_demo is False


@pytest.mark.parametrize(
    ("marker", "reason"),
    [
        ({"K_SERVICE": "gateway"}, "K_SERVICE is set"),
        ({"CLOUD_RUN_JOB": "migrate"}, "CLOUD_RUN_JOB is set"),
        ({"SECRET_PROVIDER": "gcp"}, "SECRET_PROVIDER=gcp"),
        ({"GATEWAY_URL": "https://referraleasy.example.org"}, "public https address"),
        ({"SYNTHETIC": "false"}, "SYNTHETIC is not true"),
    ],
)
@pytest.mark.parametrize("profile", ["demo", "test"])
def test_fixture_profile_refuses_to_start_when_deployed(
    profile: str, marker: dict[str, str], reason: str
) -> None:
    """Negative: `APP_ENV=demo` on Cloud Run is exactly the misconfiguration B1 describes."""
    with pytest.raises(ConfigurationError, match=reason):
        GatewaySettings.from_env({**SECRETS, "APP_ENV": profile, **marker})


@pytest.mark.parametrize(
    "url", ["http://localhost:8080", "https://localhost:8443", "https://app.localhost", ""]
)
def test_fixture_profile_accepts_local_urls(url: str) -> None:
    settings = GatewaySettings.from_env({**SPEC_ENV, "GATEWAY_URL": url})
    assert settings.is_demo


def test_production_may_run_where_the_fixture_profile_may_not() -> None:
    settings = GatewaySettings.from_env(
        {**SECRETS, "APP_ENV": "production", "K_SERVICE": "gateway", "SECRET_PROVIDER": "gcp"}
    )
    assert settings.is_demo is False


def test_production_has_no_fixture_routes() -> None:
    settings = GatewaySettings.from_env({**SECRETS, "APP_ENV": "production"})
    with TestClient(create_app(settings=settings)) as client:
        assert client.post("/v1/auth/admin-session", json={"subject": "x"}).status_code == 404
        assert (
            client.post(
                "/v1/auth/patient-session", json={"subject": "p", "patient_ref": "p"}
            ).status_code
            == 404
        )


@pytest.mark.parametrize(("profile", "fixture_auth"), [("demo", True), ("production", False)])
def test_healthz_and_header_report_the_profile(profile: str, fixture_auth: bool) -> None:
    settings = GatewaySettings.from_env({**SECRETS, "APP_ENV": profile})
    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.headers["X-ReferralEasy-Profile"] == profile
        assert response.json()["profile"] == {
            "name": profile,
            "fixture_auth": fixture_auth,
            "synthetic": True,
        }
        # Errors carry it too: the header is set by middleware, not by each route.
        missing = client.get("/v1/admin/metrics")
        assert missing.status_code == 401
        assert missing.headers["X-ReferralEasy-Profile"] == profile
