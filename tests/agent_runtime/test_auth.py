"""Service auth: the token is the credential, and nothing else is.

Three attacks are modelled directly, because each one is a plausible "small
shortcut" that a later change could reintroduce:

1. **"The trace id is already in the log, so let it through."** Refused structurally —
   ``verify`` has no parameter that could carry a trace id, and a test asserts the
   signature so a future keyword cannot be added quietly.
2. **"The body says who the user is."** Refused by dropping every identity claim, and
   *recording* the drop, because a payload that names its own ``user_id`` is either a
   bug or an attack and both deserve to be visible.
3. **"Let me leave the token empty in dev."** Refused at construction: a blank token
   authenticates everyone, so it is a startup failure rather than a wildcard.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from referraleasy_agent_runtime.auth import (
    AUTH_SCHEME,
    AUTH_TOKEN_ENV,
    TRACE_ID_HEADER,
    UNTRUSTED_IDENTITY_KEYS,
    BearerTokenVerifier,
    ServicePrincipal,
    drop_untrusted_identity,
    service_auth_dependency,
    service_auth_headers,
)
from referraleasy_agent_runtime.errors import ConfigurationError, ServiceAuthError

TOKEN = "a-shared-service-token"
OTHER_TOKEN = "a-different-service-token"


def _verifier() -> BearerTokenVerifier:
    return BearerTokenVerifier(TOKEN, audience="referraleasy-a2a", realm="referraleasy")


def test_the_right_token_yields_a_principal() -> None:
    principal = _verifier().verify(f"{AUTH_SCHEME} {TOKEN}")

    assert isinstance(principal, ServicePrincipal)
    assert principal.subject == "a2a-service"
    assert principal.audience == "referraleasy-a2a"


def test_a_missing_token_is_rejected() -> None:
    with pytest.raises(ServiceAuthError) as raised:
        _verifier().verify(None)

    assert raised.value.www_authenticate == 'Bearer realm="referraleasy"'
    assert raised.value.to_result().a2a_state.value == "auth-required"


def test_a_blank_header_is_rejected_like_a_missing_one() -> None:
    for header in ("", "   ", "\t"):
        with pytest.raises(ServiceAuthError):
            _verifier().verify(header)


def test_a_wrong_token_is_rejected() -> None:
    with pytest.raises(ServiceAuthError, match="invalid service token"):
        _verifier().verify(f"{AUTH_SCHEME} {OTHER_TOKEN}")


def test_a_near_miss_token_is_rejected() -> None:
    """A prefix, a suffix or a different case must not authenticate."""
    near_misses = (TOKEN[:-1], TOKEN + "x", TOKEN.upper(), TOKEN.replace("-", "_"))

    for candidate in near_misses:
        with pytest.raises(ServiceAuthError):
            _verifier().verify(f"{AUTH_SCHEME} {candidate}")


def test_surrounding_whitespace_is_a_transport_artefact_not_a_bypass() -> None:
    """HTTP trims header values; the *token* still has to match in full.

    Stripping cannot widen access: every character of the configured token is still
    compared, so a padded correct token authenticates anyone who already had it.
    """
    assert _verifier().verify(f"{AUTH_SCHEME}   {TOKEN}  ").subject == "a2a-service"


def test_the_scheme_must_be_bearer() -> None:
    for header in (TOKEN, f"Basic {TOKEN}", f"Token {TOKEN}", f"bearer{TOKEN}"):
        with pytest.raises(ServiceAuthError, match="Bearer"):
            _verifier().verify(header)


def test_a_bearer_header_with_no_credential_is_rejected() -> None:
    with pytest.raises(ServiceAuthError):
        _verifier().verify("Bearer    ")


def test_the_principal_carries_a_fingerprint_not_the_token() -> None:
    verifier = _verifier()
    principal = verifier.verify(f"{AUTH_SCHEME} {TOKEN}")

    entry = principal.as_trace_entry()
    assert principal.token_fingerprint == verifier.fingerprint
    assert len(principal.token_fingerprint) == 12
    assert TOKEN not in "".join(entry.values())
    assert TOKEN not in repr(principal)


def test_a_blank_configured_token_refuses_to_start() -> None:
    """An empty token would authenticate every caller."""
    for blank in ("", "   "):
        with pytest.raises(ConfigurationError, match=AUTH_TOKEN_ENV):
            BearerTokenVerifier(blank)


def test_the_verifier_cannot_be_built_from_an_environment_without_a_token() -> None:
    with pytest.raises(ConfigurationError, match=AUTH_TOKEN_ENV):
        BearerTokenVerifier.from_env({})

    assert BearerTokenVerifier.from_env({AUTH_TOKEN_ENV: TOKEN}).fingerprint


def test_verify_takes_the_authorization_header_and_nothing_else() -> None:
    """Structural, not documentary: no `trace_id`, no `payload`, no `**kwargs`."""
    parameters = list(inspect.signature(BearerTokenVerifier.verify).parameters)

    assert parameters == ["self", "authorization"], (
        "a trace id (or anything else a caller controls) must not be able to reach the "
        f"verification path; found {parameters}"
    )

    for name in ("trace_id", "payload", "body", "headers", "user_id"):
        assert name not in parameters


def test_a_trace_id_alone_does_not_authenticate() -> None:
    """The header is correlation; presenting it without a token is still a 401."""
    with pytest.raises(ServiceAuthError):
        _verifier().verify(None)

    headers = service_auth_headers(TOKEN, trace_id="trace-1")
    assert headers[TRACE_ID_HEADER] == "trace-1"
    assert headers["Authorization"] == f"{AUTH_SCHEME} {TOKEN}"

    without_trace = service_auth_headers(TOKEN)
    assert TRACE_ID_HEADER not in without_trace
    assert without_trace == {"Authorization": f"{AUTH_SCHEME} {TOKEN}"}


def test_the_outbound_headers_are_the_token_and_the_correlation_id() -> None:
    headers = service_auth_headers(f"  {TOKEN}  ", trace_id="trace-9")

    assert headers == {"Authorization": f"Bearer {TOKEN}", "X-Trace-Id": "trace-9"}
    assert set(headers) == {"Authorization", TRACE_ID_HEADER}


def test_a_payload_user_id_is_dropped_and_reported() -> None:
    payload = {
        "message": "book the appointment",
        "workflow_id": "wf-1",
        "user_id": "an-attacker-chosen-id",
        "patient_id": "someone-elses-record",
        "role": "scheduling",
    }

    trusted, dropped = drop_untrusted_identity(payload)

    assert "user_id" not in trusted
    assert "patient_id" not in trusted
    assert "role" not in trusted
    assert trusted == {"message": "book the appointment", "workflow_id": "wf-1"}

    assert dropped == {
        "user_id": "an-attacker-chosen-id",
        "patient_id": "someone-elses-record",
        "role": "scheduling",
    }


def test_every_identity_key_the_runtime_knows_is_dropped() -> None:
    payload = {key: "claimed" for key in UNTRUSTED_IDENTITY_KEYS}
    payload["message"] = "keep me"

    trusted, dropped = drop_untrusted_identity(payload)

    assert trusted == {"message": "keep me"}
    assert set(dropped) == set(UNTRUSTED_IDENTITY_KEYS)


def test_dropping_identity_does_not_mutate_the_caller_payload() -> None:
    payload = {"user_id": "x", "message": "y"}

    drop_untrusted_identity(payload)

    assert payload == {"user_id": "x", "message": "y"}


def test_a_payload_with_no_identity_claims_reports_an_empty_drop() -> None:
    trusted, dropped = drop_untrusted_identity({"message": "hello"})

    assert trusted == {"message": "hello"}
    assert dropped == {}


def _protected_app() -> FastAPI:
    app = FastAPI()
    require_principal = service_auth_dependency(_verifier())

    # The dependency must be the *default value*: this module uses postponed
    # annotations, so FastAPI resolves the annotation against module globals and an
    # `Annotated[..., Depends(local)]` would become a required query parameter.
    @app.get("/tasks")
    def tasks(
        principal: ServicePrincipal = Depends(require_principal),  # noqa: B008
    ) -> dict[str, str]:
        return principal.as_trace_entry()

    return app


def test_the_dependency_rejects_a_call_with_no_bearer_token() -> None:
    with TestClient(_protected_app()) as client:
        response = client.get("/tasks")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="referraleasy"'


def test_the_dependency_rejects_an_incorrect_bearer_token() -> None:
    with TestClient(_protected_app()) as client:
        response = client.get("/tasks", headers={"Authorization": f"Bearer {OTHER_TOKEN}"})

    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers


def test_the_dependency_rejects_a_non_bearer_scheme() -> None:
    with TestClient(_protected_app()) as client:
        response = client.get("/tasks", headers={"Authorization": f"Basic {TOKEN}"})

    assert response.status_code == 401


def test_the_dependency_passes_the_verified_principal_to_the_handler() -> None:
    with TestClient(_protected_app()) as client:
        response = client.get("/tasks", headers={"Authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 200
    assert response.json()["subject"] == "a2a-service"
    assert TOKEN not in response.text


def test_the_dependency_ignores_an_untrusted_subject_header() -> None:
    """A caller may *declare* who it is; the token decides who it is."""
    with TestClient(_protected_app()) as client:
        response = client.get(
            "/tasks",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "X-Service-Subject": "admin",
                "X-Trace-Id": "trace-1",
            },
        )

    assert response.json()["subject"] == "a2a-service"
