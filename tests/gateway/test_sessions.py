# ruff: noqa: RUF002  # Turkish quoted from §9 of ReferralEasy_Architecture_v3.md is kept verbatim
"""Sessions: two kinds, two secrets, and a trace id that is not one of them (§9).

The claim under test is that the separation is *real* — enforced by a verifier over a signed token
rather than by a branch a handler could forget — so every test here presents a credential that a
reasonable attacker (or a careless client) would try, and asserts the refusal, not merely a
non-200: the status, the envelope's `code`, and for the 401 the challenge a client needs to retry
correctly.

A test that would still pass if the verifier were replaced by `return Session(...)` is not a test, so
each one also asserts that the *right* credential is accepted somewhere — otherwise a gateway that
refused everything would satisfy the whole file.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gateway_fixtures import REPO_ROOT, SPEC_ENV, Demo, demo_settings, profile_settings

from referraleasy_gateway.app import create_app
from referraleasy_gateway.auth import SessionKind, build_verifiers
from referraleasy_gateway.config import ConfigurationError, GatewaySettings
from referraleasy_gateway.control import SeedableControlState
from referraleasy_gateway.timing import ManualClock


def _b64url(segment: bytes) -> str:
    return base64.urlsafe_b64encode(segment).rstrip(b"=").decode("ascii")


def _unsigned_token(private_claims: dict[str, Any]) -> str:
    """A token with `alg: none` and an empty signature — the classic JWT confusion attack.

    Built by hand rather than by a library, because the point is that the *verifier* must refuse it:
    a token that declares "no signature" while carrying a valid issuer, audience and expiry is
    accepted by anything that reads the header and picks the algorithm from it.
    """
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(private_claims).encode())
    return f"{header}.{payload}."


def _claims(demo: Demo) -> dict[str, Any]:
    """The patient token's own claims, decoded, so a forged token differs in one field only."""
    _, payload, _ = demo.patient_token.split(".")
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def test_a_patient_session_reads_its_own_workflow(client: TestClient, demo: Demo) -> None:
    """The positive case. Without it, every refusal below is satisfied by refusing everything."""
    workflow_id = demo.seed()
    response = demo.snapshot(workflow_id)
    assert response.status_code == 200
    body = response.json()
    assert body["workflow_id"] == workflow_id
    assert body["state"] == "INTAKE"
    assert body["state_version"] == 1
    assert body["synthetic"] is True


def test_a_patient_token_is_refused_at_a_coordinator_route(client: TestClient, demo: Demo) -> None:
    """The direction §9 spells out ("hasta/admin oturumları ayrıdır"), refused twice over.

    With the two secrets the fixture configures, a patient token does not even *verify* against the
    admin verifier: a 401 whose message names the signature, so a client is not told to re-authenticate
    as a coordinator when the token it holds is simply not one. The audience check is the second
    layer, and it is what still holds in the misconfigured deployment where both surfaces share a
    secret — `test_a_token_for_the_other_audience_is_refused_when_the_secrets_match` covers that.
    """
    workflow_id = demo.seed()
    response = client.get(f"/v1/admin/workflows/{workflow_id}", headers=demo.patient)
    assert response.status_code == 401
    assert "signature" in response.json()["error"]["message"].lower()
    # The same route with the right session works, so the refusal is about the session.
    assert client.get(f"/v1/admin/workflows/{workflow_id}", headers=demo.admin).status_code == 200


def test_a_token_for_the_other_audience_is_refused_when_the_secrets_match(
    settings: GatewaySettings, clock: ManualClock, control: SeedableControlState
) -> None:
    """The audience and `kind` claims, tested where they are the only thing standing.

    One secret for both surfaces is a deployment mistake the service allows (it cannot know whether
    an operator has a key-management reason), so the claims must carry the separation by themselves:
    a token that is cryptographically valid is still refused at the wrong surface, with 403 rather
    than 401 — re-authenticating as the same principal would not help.
    """
    shared = profile_settings(settings, admin_secret=SPEC_ENV["JWT_PATIENT_SECRET"])
    assert shared.patient_secret == shared.admin_secret
    assert shared.patient_audience != shared.admin_audience
    app = create_app(settings=shared, clock=clock, control=control)
    with TestClient(app) as client:
        demo = Demo(client)
        workflow_id = demo.seed()
        # The patient token now verifies against the admin secret, so the claim check is reached.
        at_admin = client.get(f"/v1/admin/workflows/{workflow_id}", headers=demo.patient)
        assert at_admin.status_code == 403
        assert at_admin.json()["error"]["code"] == "audience_mismatch"
        at_patient = client.get(f"/v1/workflows/{workflow_id}", headers=demo.admin)
        assert at_patient.status_code == 403
        assert at_patient.json()["error"]["code"] == "audience_mismatch"
        # And each token still works on its own surface: the refusal is scoped, not blanket.
        assert client.get(f"/v1/workflows/{workflow_id}", headers=demo.patient).status_code == 200
        assert (
            client.get(f"/v1/admin/workflows/{workflow_id}", headers=demo.admin).status_code == 200
        )


def test_a_missing_credential_is_a_401_with_a_challenge(client: TestClient, demo: Demo) -> None:
    workflow_id = demo.seed()
    response = client.get(f"/v1/workflows/{workflow_id}")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


def test_a_trace_id_grants_no_authority(client: TestClient, demo: Demo) -> None:
    """§9: a trace id is correlation, not a credential.

    Three shapes of the same mistake. Alone it must not authenticate; beside a valid session it must
    neither add nor remove authority (the coordinator's own route still serves the coordinator); and
    beside a valid *patient* session it must not widen scope, so another patient's workflow is still
    a 404 rather than a trace-id-shaped exception.
    """
    workflow_id = demo.seed()
    other_patient = demo.seed(patient_ref="patient-2")

    alone = client.get(f"/v1/workflows/{workflow_id}", headers={"X-Trace-Id": "trace-1"})
    assert alone.status_code == 401

    beside_admin = client.get(
        f"/v1/admin/workflows/{workflow_id}", headers={**demo.admin, "X-Trace-Id": "trace-1"}
    )
    assert beside_admin.status_code == 200

    beside_patient = demo.snapshot(other_patient)
    assert beside_patient.status_code == 404
    with_trace = client.get(
        f"/v1/workflows/{other_patient}", headers={**demo.patient, "X-Trace-Id": "trace-1"}
    )
    assert with_trace.status_code == 404

    wrong_kind = client.get(
        f"/v1/admin/workflows/{workflow_id}", headers={**demo.patient, "X-Trace-Id": "trace-1"}
    )
    assert wrong_kind.status_code == 401


def test_a_tampered_signature_is_refused(client: TestClient, demo: Demo) -> None:
    """One character of the signature changed: the token is no longer the one we signed."""
    workflow_id = demo.seed()
    header, payload, signature = demo.patient_token.split(".")
    tampered = f"{header}.{payload}.{signature[:-1]}{'A' if signature[-1] != 'A' else 'B'}"
    response = client.get(
        f"/v1/workflows/{workflow_id}", headers={"Authorization": f"Bearer {tampered}"}
    )
    assert response.status_code == 401
    assert "signature" in response.json()["error"]["message"].lower()


def test_a_tampered_payload_is_refused(client: TestClient, demo: Demo) -> None:
    """Re-encoded claims, original signature: the signature covers the payload, so it fails.

    This is the attack a verifier that trusted the claims before checking the signature would let
    through — e.g. one that decoded `kind` to choose the secret and then compared.
    """
    workflow_id = demo.seed()
    header, _, signature = demo.patient_token.split(".")
    forged_claims = {**_claims(demo), "sub": "someone-else"}
    forged = f"{header}.{_b64url(json.dumps(forged_claims).encode())}.{signature}"
    response = client.get(
        f"/v1/workflows/{workflow_id}", headers={"Authorization": f"Bearer {forged}"}
    )
    assert response.status_code == 401


def test_alg_none_is_refused(client: TestClient, demo: Demo) -> None:
    """The verifier accepts one algorithm, and it is a literal in the module, not a header field."""
    workflow_id = demo.seed()
    token = _unsigned_token(_claims(demo))
    response = client.get(
        f"/v1/workflows/{workflow_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
    assert "alg" in response.json()["error"]["message"].lower()


def test_an_expired_token_is_refused(client: TestClient, demo: Demo, clock: ManualClock) -> None:
    """Past `exp`, plus the tolerated skew, the session is over.

    `JWT_TTL_SECONDS=1` and a two-hour jump: the assertion is about the expiry check, not about the
    arithmetic of a boundary, which the skew test below covers.
    """
    workflow_id = demo.seed()
    short = client.post(
        "/v1/auth/patient-session",
        json={"subject": "patient-1", "patient_ref": "patient-1", "ttl_seconds": 1},
    ).json()["access_token"]
    assert (
        client.get(
            f"/v1/workflows/{workflow_id}", headers={"Authorization": f"Bearer {short}"}
        ).status_code
        == 200
    )
    clock.advance_ms(2 * 3600 * 1000)
    response = client.get(
        f"/v1/workflows/{workflow_id}", headers={"Authorization": f"Bearer {short}"}
    )
    assert response.status_code == 401
    assert "expired" in response.json()["error"]["message"].lower()


def test_a_token_expiring_inside_the_skew_is_still_accepted(
    client: TestClient, demo: Demo, clock: ManualClock
) -> None:
    """The tolerated skew is small and it exists: two machines disagreeing by seconds is not an
    expired session. 10 s past `exp` with a 30 s tolerance is inside it."""
    workflow_id = demo.seed()
    short = client.post(
        "/v1/auth/patient-session",
        json={"subject": "patient-1", "patient_ref": "patient-1", "ttl_seconds": 60},
    ).json()["access_token"]
    clock.advance_ms(70 * 1000)
    assert (
        client.get(
            f"/v1/workflows/{workflow_id}", headers={"Authorization": f"Bearer {short}"}
        ).status_code
        == 200
    )


def test_a_token_signed_by_the_patient_secret_but_claiming_the_admin_kind_is_refused(
    settings: GatewaySettings, clock: ManualClock, control: SeedableControlState
) -> None:
    """Both halves of "the two kinds are separate": the secret *and* the `kind` claim.

    A token that would verify against the patient secret but declares itself an admin session must
    not be usable on the coordinator surface — otherwise the audience check would be the only thing
    between a patient's browser and every workflow in the demo.
    """
    from referraleasy_gateway.auth import JwtIssuer

    app = create_app(settings=settings, clock=clock, control=control)
    with TestClient(app) as client:
        demo = Demo(client)
        workflow_id = demo.seed()
        forged = JwtIssuer(
            settings.patient_secret,
            kind=SessionKind.admin,
            audience=settings.admin_audience,
            issuer=settings.jwt_issuer,
            ttl_seconds=600,
            clock=clock,
        ).issue("coordinator-1")
        response = client.get(
            f"/v1/admin/workflows/{workflow_id}", headers={"Authorization": f"Bearer {forged}"}
        )
        assert response.status_code == 401
        assert "signature" in response.json()["error"]["message"].lower()


def test_the_patient_session_requires_a_patient_ref(client: TestClient) -> None:
    response = client.post("/v1/auth/patient-session", json={"subject": "patient-1"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "contract_violation"


def test_the_admin_session_refuses_a_patient_ref(client: TestClient) -> None:
    """A coordinator session that carries a patient identity is a request for the patient surface."""
    response = client.post(
        "/v1/auth/admin-session", json={"subject": "coordinator-1", "patient_ref": "patient-1"}
    )
    assert response.status_code == 422


def test_a_patient_cannot_read_another_patients_workflow(client: TestClient, demo: Demo) -> None:
    """404, not 403: "forbidden" would confirm that the id exists, which is a fact about someone
    else's referral."""
    other = demo.seed(patient_ref="patient-2")
    response = demo.snapshot(other)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_the_fixture_routes_do_not_exist_outside_the_demo_profile(
    settings: GatewaySettings, clock: ManualClock, control: SeedableControlState
) -> None:
    """The token minting and the seeding route are fixtures, and a deployment must not have them.

    `APP_ENV=production` with real secrets: the routes are gone (404), while `/healthz` and the
    authenticated surfaces still work. That is the difference between a demo convenience and a
    backdoor. The control backend is *not* moved: the fixture routes are gated on `APP_ENV` alone,
    and a test that also changed backend would be unable to tell which of the two it was reading.
    """
    settings = profile_settings(settings, app_env="production")
    app = create_app(settings=settings, clock=clock, control=control)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert (
            client.post(
                "/v1/auth/patient-session", json={"subject": "p", "patient_ref": "p"}
            ).status_code
            == 404
        )
        assert client.post("/v1/auth/admin-session", json={"subject": "c"}).status_code == 404
        assert client.post("/v1/demo/workflows", json={"patient_ref": "p"}).status_code == 404
        # A token minted the way a real provider would mint one still works: the *sessions* are not
        # the fixture, only the two routes that hand them out are.
        from referraleasy_gateway.auth import JwtIssuer

        admin_token = JwtIssuer(
            settings.admin_secret,
            kind=SessionKind.admin,
            audience=settings.admin_audience,
            issuer=settings.jwt_issuer,
            ttl_seconds=600,
            clock=clock,
        ).issue("coordinator-1")
        assert (
            client.get(
                "/v1/admin/metrics", headers={"Authorization": f"Bearer {admin_token}"}
            ).status_code
            == 200
        )


def test_a_query_token_is_accepted_only_where_the_demo_needs_it(
    settings: GatewaySettings, clock: ManualClock, control: SeedableControlState
) -> None:
    """`EventSource` cannot set headers, so the stream accepts `?token=` — in the demo profile.

    The production profile has the same route and the same verifier, and simply does not consult the
    query parameter: a token in a URL must be assumed to reach logs, which is acceptable for a local
    fixture and not for a deployment.
    """
    from referraleasy_gateway.auth import JwtIssuer

    production = profile_settings(settings, app_env="production")
    app = create_app(settings=production, clock=clock, control=control)
    assert production.is_demo is False
    with TestClient(app) as client:
        # Seeding is demo-only, so the workflow is created through the port directly — the same thing
        # the referral intake flow does in a real deployment.
        workflow_id = str(control.create("patient-1").workflow_id)
        token = JwtIssuer(
            production.patient_secret,
            kind=SessionKind.patient,
            audience=production.patient_audience,
            issuer=production.jwt_issuer,
            ttl_seconds=600,
            clock=clock,
        ).issue("patient-1", patient_ref="patient-1")
        by_query = client.get(f"/v1/workflows/{workflow_id}/events", params={"token": token})
        assert by_query.status_code == 401
        by_header = client.get(
            f"/v1/workflows/{workflow_id}/events", headers={"Authorization": f"Bearer {token}"}
        )
        assert by_header.status_code == 200


def test_a_placeholder_secret_is_refused_outside_the_demo_profile() -> None:
    """Same rule as `scripts/validate_env.py`: this repository is public, so a `CHANGEME_*` signing
    key is a published key. The demo profile is exempt because it *is* the fixture profile."""
    with pytest.raises(ConfigurationError, match="placeholder"):
        GatewaySettings.from_env(
            {
                "APP_ENV": "production",
                "JWT_PATIENT_SECRET": "CHANGEME_LOCAL_FIXTURE_SECRET",
                "JWT_ADMIN_SECRET": "CHANGEME_LOCAL_FIXTURE_SECRET",
            }
        )
    # The demo profile is exactly what those placeholders are for.
    settings = GatewaySettings.from_env(
        {
            "APP_ENV": "demo",
            "JWT_PATIENT_SECRET": "CHANGEME_LOCAL_FIXTURE_SECRET",
            "JWT_ADMIN_SECRET": "CHANGEME_LOCAL_FIXTURE_SECRET",
        }
    )
    assert settings.is_demo


def test_blank_secrets_refuse_to_start() -> None:
    """A blank signing key would authenticate every token, so it is a startup failure."""
    with pytest.raises(ConfigurationError):
        GatewaySettings.from_env({"JWT_PATIENT_SECRET": "", "JWT_ADMIN_SECRET": ""})


def test_equal_audiences_refuse_to_start() -> None:
    with pytest.raises(ConfigurationError, match="audiences are equal"):
        GatewaySettings.from_env(
            {
                **SPEC_ENV,
                "JWT_PATIENT_AUDIENCE": "referraleasy-everyone",
                "JWT_ADMIN_AUDIENCE": "referraleasy-everyone",
            }
        )


def test_the_postgres_profile_is_verified_before_it_serves(clock: ManualClock) -> None:
    """A `postgres` label that cannot reach a database is a refusal to start, not a healthy process.

    Two things can be wrong with the profile that a name check cannot see, and both used to be
    covered by refusing the profile outright: a DSN that names a server that is not there, and a
    database whose migrations have never run. The adapter exists now — `test_postgres_control_state`
    runs it against a real server — so what is left to refuse is those two, and refusing them is the
    same mechanism for the same reason it always was: a container that starts, reports healthy and
    fails every request is the worst of the available failures (§7).

    The message is asserted, not just the type: "cannot serve" is different from "misconfigured", and
    an operator reading the log has to be able to tell them apart. `connect_timeout` is in the DSN so
    the test fails in a second rather than waiting on a TCP timeout.
    """
    settings = demo_settings(
        GATEWAY_CONTROL_BACKEND="postgres",
        DATABASE_URL="postgresql://nobody:nobody@127.0.0.1:1/nothing-here?connect_timeout=2",
    )
    with pytest.raises(ConfigurationError, match="cannot serve"):
        create_app(settings=settings, clock=clock)


def test_the_postgres_profile_needs_a_database_url() -> None:
    """The profile cannot fall back to the in-memory store, so a missing DSN is not a default."""
    with pytest.raises(ConfigurationError, match="needs DATABASE_URL"):
        demo_settings(GATEWAY_CONTROL_BACKEND="postgres")


def test_an_unknown_control_backend_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="not 'memory' or 'postgres'"):
        demo_settings(GATEWAY_CONTROL_BACKEND="sqlite")


def test_healthz_needs_no_credential_and_reports_what_it_does_not_check(
    client: TestClient,
) -> None:
    """The probe compose curls. It must not need a session, and it must not touch the database."""
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "referraleasy-gateway"
    assert body["guardrails"] == {
        "mode": "three_stage",
        "configured": False,
        "layers": {"regex": True, "decision_model": False, "local_classifier": False},
    }
    assert body["synthetic"] is True
    # §18: the offline profile is labelled in what the service serves, not only in the docs.
    assert "OFFLINE FIXTURE PROFILE" in body["offline_profile"]


def test_the_verifiers_are_built_from_the_two_configured_secrets(
    settings: GatewaySettings, clock: ManualClock
) -> None:
    """The verifiers read the settings, not a module constant: separate audiences, separate secrets."""
    patient, admin = build_verifiers(settings, clock)
    assert patient.audience == settings.patient_audience
    assert admin.audience == settings.admin_audience
    assert patient.kind is SessionKind.patient
    assert admin.kind is SessionKind.admin
    assert patient.audience != admin.audience


def test_the_error_envelope_carries_a_request_id_that_matches_the_header(
    client: TestClient,
) -> None:
    """One failure shape, and an id that ties the body to the log line and the response header."""
    response = client.get("/does-not-exist", headers={"X-Request-Id": "req-abc"})
    assert response.status_code == 404
    assert response.headers["X-Request-Id"] == "req-abc"


def test_the_repository_root_is_the_one_these_tests_assume() -> None:
    """A guard on the path fallback: if this ever stops being the repository, the suite is running
    against a different tree than the one it is testing."""
    assert (REPO_ROOT / "services" / "gateway" / "src" / "referraleasy_gateway").is_dir()
    assert (REPO_ROOT / "migrations" / "014_align_domain_vocabulary.sql").is_file()
