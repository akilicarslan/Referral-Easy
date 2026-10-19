"""Service-to-service authentication for A2A calls.

Every A2A call carries ``Authorization: Bearer <A2A_SERVICE_TOKEN>``. The token is
verified **at the transport**, before any body is read, and the resulting
principal is the only identity the runtime will use.

What this module refuses to do
------------------------------
* **Trust a payload.** A ``user_id`` (or ``patient_id``, ``role``, ``scopes``) in a
  request body is a *claim by the caller about itself*, and the caller is exactly
  the party we are authenticating. Those keys are dropped, and the drop is
  recorded so it shows up in the audit trail rather than vanishing.
* **Treat a trace id as authority.** ``X-Trace-Id`` is correlation, and it is
  caller-supplied. :meth:`BearerTokenVerifier.verify` takes the authorization
  header and nothing else — a signature-level fact, pinned by a test — so no
  handler can be tempted to accept a trace id in place of a credential.
* **Run with a blank token.** A blank configured token would authenticate
  everyone; it is a startup failure, not a wildcard.

The comparison is constant-time and the principal carries a fingerprint of the
token rather than the token, so a principal can be logged.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final

from fastapi import Header

from referraleasy_agent_runtime.errors import ConfigurationError, ServiceAuthError

__all__ = [
    "AUTH_SCHEME",
    "AUTH_TOKEN_ENV",
    "SERVICE_SUBJECT_HEADER",
    "TRACE_ID_HEADER",
    "UNTRUSTED_IDENTITY_KEYS",
    "BearerTokenVerifier",
    "ServicePrincipal",
    "drop_untrusted_identity",
    "service_auth_dependency",
    "service_auth_headers",
]

#: Environment variable holding the shared service token (in `.env`; never logged).
AUTH_TOKEN_ENV: Final = "A2A_SERVICE_TOKEN"

AUTH_SCHEME: Final = "Bearer"

#: Correlation only. Never an authority.
TRACE_ID_HEADER: Final = "X-Trace-Id"

#: Optional caller-declared subject, for logs. The token, not this, authenticates.
SERVICE_SUBJECT_HEADER: Final = "X-Service-Subject"

#: Payload keys that describe *identity or authority*. A caller-supplied self-description is
#: not evidence, so these are stripped before anything downstream can read them.
UNTRUSTED_IDENTITY_KEYS: frozenset[str] = frozenset(
    {
        "user_id",
        "patient_id",
        "actor_id",
        "role",
        "roles",
        "scopes",
        "is_admin",
        "is_operator",
    }
)

_FINGERPRINT_LENGTH: Final = 12


@dataclass(frozen=True, slots=True)
class ServicePrincipal:
    """Who called, as established by the transport.

    ``subject`` comes from a **verified** token, never from a header the caller
    chose; the header only selects which token to present, and the token decides
    who that is.
    """

    subject: str
    audience: str
    token_fingerprint: str

    def as_trace_entry(self) -> dict[str, str]:
        """A loggable form. The token itself is never included."""
        return {
            "subject": self.subject,
            "audience": self.audience,
            "token_fingerprint": self.token_fingerprint,
        }


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:_FINGERPRINT_LENGTH]


class BearerTokenVerifier:
    """Verifies the shared service token. Construct once, share it.

    Not a JWT verifier and deliberately not extensible into one: the demo's A2A
    hop is authenticated by a shared secret, and the patient's own identity is
    established by the gateway's signed context, not here.
    """

    def __init__(
        self,
        token: str,
        *,
        audience: str = "referraleasy-a2a",
        realm: str = "referraleasy",
        subject: str = "a2a-service",
    ) -> None:
        if not token.strip():
            raise ConfigurationError(
                f"{AUTH_TOKEN_ENV} is empty. A blank service token would authenticate every "
                "caller, so the runtime refuses to start rather than serve an open A2A endpoint."
            )
        self._token = token.strip().encode("utf-8")
        self.audience = audience
        self.realm = realm
        self.subject = subject
        self.fingerprint = _fingerprint(token.strip())

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        audience: str = "referraleasy-a2a",
        realm: str = "referraleasy",
        subject: str = "a2a-service",
    ) -> BearerTokenVerifier:
        """Build a verifier from ``env``, failing loudly when the token is absent."""
        token = env.get(AUTH_TOKEN_ENV, "").strip()
        if not token:
            raise ConfigurationError(
                f"{AUTH_TOKEN_ENV} is not set. It authenticates service-to-service A2A calls."
            )
        return cls(token, audience=audience, realm=realm, subject=subject)

    @property
    def challenge(self) -> str:
        """The ``WWW-Authenticate`` value a 401 must carry."""
        return f'{AUTH_SCHEME} realm="{self.realm}"'

    def verify(self, authorization: str | None) -> ServicePrincipal:
        """Return the principal, or raise :class:`ServiceAuthError`.

        Takes the authorization header and nothing else. There is no ``trace_id``
        parameter and no ``payload`` parameter, which is what makes "a trace id
        never grants authority" structural rather than a review comment.
        """
        if authorization is None or not authorization.strip():
            raise ServiceAuthError(
                "missing Authorization header: A2A calls are authenticated at the transport",
                www_authenticate=self.challenge,
            )
        scheme, _, credential = authorization.strip().partition(" ")
        if scheme.lower() != AUTH_SCHEME.lower() or not credential.strip():
            raise ServiceAuthError(
                f"expected a {AUTH_SCHEME} credential in the Authorization header",
                www_authenticate=self.challenge,
            )
        candidate = credential.strip().encode("utf-8")
        if not hmac.compare_digest(candidate, self._token):
            raise ServiceAuthError("invalid service token", www_authenticate=self.challenge)
        return ServicePrincipal(
            subject=self.subject,
            audience=self.audience,
            token_fingerprint=self.fingerprint,
        )

    __call__ = verify


def drop_untrusted_identity(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a payload into (trusted body, dropped identity claims).

    The second element is not a discard bin: it is what a handler should log or
    attach to the audit trail, because an identity claim that does not come from
    the transport is either a bug or an attack, and both are worth seeing.
    """
    trusted = {key: value for key, value in payload.items() if key not in UNTRUSTED_IDENTITY_KEYS}
    dropped = {key: value for key, value in payload.items() if key in UNTRUSTED_IDENTITY_KEYS}
    return trusted, dropped


def service_auth_headers(token: str, *, trace_id: str | None = None) -> dict[str, str]:
    """Headers for an outbound A2A call: the token, plus correlation when known."""
    headers = {"Authorization": f"{AUTH_SCHEME} {token.strip()}"}
    if trace_id:
        headers[TRACE_ID_HEADER] = trace_id
    return headers


def service_auth_dependency(
    verifier: BearerTokenVerifier,
) -> Callable[..., ServicePrincipal]:
    """A FastAPI dependency that authenticates the caller at the transport.

    Returns 401 with a ``WWW-Authenticate`` challenge. The endpoint receives a
    :class:`ServicePrincipal`, so no handler ever sees a raw header and no handler
    can decide for itself whether the caller is authentic.
    """
    from fastapi import HTTPException

    def require_service_principal(
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> ServicePrincipal:
        try:
            return verifier.verify(authorization)
        except ServiceAuthError as error:
            raise HTTPException(
                status_code=401,
                detail=str(error),
                headers={"WWW-Authenticate": error.www_authenticate},
            ) from error

    return require_service_principal
