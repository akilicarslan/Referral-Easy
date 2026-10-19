# ruff: noqa: RUF002  # Turkish quoted from §9 of ReferralEasy_Architecture_v3.md is kept verbatim
"""Sessions: patient and admin, verified for real, separate by construction (§9).

§9's sentence is short and load-bearing: *"Hasta/admin oturumları ve servis kimlikleri ayrıdır"* —
patient sessions, admin sessions and service identities are separate. And it adds what that
separation is worth: *"Lokal servis token/JWT fixture'ı gerçek doğrulama testleriyle çalışır"* — the
local fixture tokens run through real verification, not a bypass.

So this module does the whole job and nothing else:

* **HS256 only.** The header's `alg` must be exactly `HS256`; `none`, `RS256`, a missing `alg` and a
  lower-cased `hs256` are all refused. This is the one line that stops the classic JWT
  algorithm-confusion attack, and it is a comparison against a literal rather than a lookup in a
  table of configured algorithms.
* **The signature is checked before any claim is read**, over the exact bytes the sender signed
  (`header.payload`), and compared with `hmac.compare_digest`.
* **`iss`, `aud`, `exp`, `iat` and `nbf` are all enforced.** `aud` is what makes a patient session
  unable to satisfy an admin route; `kind` is a second, independent assertion of the same fact, so a
  token that somehow carried the patient audience but claimed to be an admin session is still
  refused. A future-instant `iat` is refused as well: a token from a clock running ahead is either a
  misconfiguration or a forgery, and neither should be honoured for the length of its lifetime.
* **A trace id is not a credential.** `X-Trace-Id` is correlation only. The dependency functions
  below take the `Authorization` header (and, for the stream, an explicit token parameter) and
  nothing else — there is no code path where a trace id can reach the verifier, which is the
  structural version of "Trace ID yetki vermez". `tests/gateway/test_sessions.py` pins it.

**Why not PyJWT.** The shared virtualenv has no JWT library, and adding a dependency is a repository
decision rather than a service's. Verification here is HMAC-SHA256 through `hmac`/`hashlib` from the
standard library: no crypto is invented, only a serialisation format is parsed. `decode_segment` is
the single place that parses, and it is exercised by the algorithm-confusion, tampering and expiry
tests. Swapping in a vetted library later means replacing `JwtVerifier.verify`'s body; the
`Session` it returns, and every caller, stay as they are.

**What these tokens are, honestly.** They are local fixtures: signed with a shared secret from the
environment, minted by `JwtIssuer` for the demo profile. §9 puts the Cloud Run profile's
audience-checked service identity behind an adapter; that adapter would replace `JwtVerifier` without
changing a route.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Final

from fastapi import Header, Query

from referraleasy_gateway.config import GatewaySettings
from referraleasy_gateway.errors import (
    DEFAULT_CHALLENGE,
    AudienceMismatchError,
    UnauthenticatedError,
)
from referraleasy_gateway.timing import Clock

__all__ = [
    "AUTH_SCHEME",
    "TRACE_ID_HEADER",
    "JwtIssuer",
    "JwtVerifier",
    "Session",
    "SessionKind",
    "admin_session_dependency",
    "build_verifiers",
    "patient_session_dependency",
]

AUTH_SCHEME: Final = "Bearer"
REALM: Final = "referraleasy"

#: Correlation only, never authority. The name is the one the agent runtime uses for the same
#: purpose, so a trace id travels under one header across the platform.
TRACE_ID_HEADER: Final = "X-Trace-Id"

#: The only algorithm this verifier accepts. A literal, not a configured list.
_SUPPORTED_ALG: Final = "HS256"

_FINGERPRINT_LENGTH: Final = 12

#: Claims a token must carry. Checked once, in `_validate_claims`.
_REQUIRED_CLAIMS: Final[tuple[str, ...]] = ("iss", "aud", "exp", "sub", "kind")


class SessionKind(StrEnum):
    """Which surface a session may be used on. §9: the two never mix."""

    patient = "patient"
    admin = "admin"


@dataclass(frozen=True, slots=True)
class Session:
    """Who is calling, as established by the transport.

    `token_fingerprint` rather than the token: a fingerprint can be logged, a bearer token cannot.
    A `patient_ref` is carried by patient sessions and is the only patient identity any handler
    uses — a `patient_ref` in a request body is a claim by the caller about itself and is ignored.
    """

    subject: str
    kind: SessionKind
    audience: str
    patient_ref: str | None
    issued_at: datetime
    expires_at: datetime
    token_fingerprint: str

    def as_trace_entry(self) -> dict[str, str]:
        return {
            "subject": self.subject,
            "kind": self.kind.value,
            "audience": self.audience,
            "token_fingerprint": self.token_fingerprint,
        }


# ---------------------------------------------------------------------------
# Encoding primitives
# ---------------------------------------------------------------------------


def _b64url_decode(segment: str, *, what: str) -> bytes:
    """Decode one JWT segment, strictly.

    Padding is restored because JWT strips it; anything that is not valid base64url for this
    alphabet is a malformed token rather than something to guess at.
    """
    if not segment:
        raise UnauthenticatedError(f"the token's {what} is empty", challenge=_challenge())
    padded = segment + "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise UnauthenticatedError(
            f"the token's {what} is not valid base64url", detail={"segment": what}
        ) from exc


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _json_object(raw: bytes, *, what: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UnauthenticatedError(f"the token's {what} is not JSON") from exc
    if not isinstance(value, dict):
        raise UnauthenticatedError(f"the token's {what} is not a JSON object")
    return value


def _challenge() -> str:
    """The challenge a 401 carries. One declaration, in `errors`, so no raise site can omit it."""
    return DEFAULT_CHALLENGE


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:_FINGERPRINT_LENGTH]


def _numeric_date(claims: Mapping[str, Any], name: str) -> datetime | None:
    value = claims.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnauthenticatedError(f"the token's {name} is not a numeric date")
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise UnauthenticatedError(f"the token's {name} is not a usable instant") from exc


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class JwtVerifier:
    """Verifies tokens for exactly one (audience, kind) pair.

    One instance per kind, built together in :func:`build_verifiers`. A single verifier that took the
    expected audience as an argument to `verify()` would put the choice at the call site, and the
    call site is where a mistake becomes a security bug.
    """

    def __init__(
        self,
        secret: str,
        *,
        kind: SessionKind,
        audience: str,
        issuer: str,
        leeway_seconds: int,
        clock: Clock,
    ) -> None:
        if not secret.strip():
            raise ValueError(f"the {kind.value} signing secret is empty; refusing to verify tokens")
        self._secret = secret.strip().encode("utf-8")
        self.kind = kind
        self.audience = audience
        self.issuer = issuer
        self.leeway = timedelta(seconds=leeway_seconds)
        self._clock = clock

    @property
    def challenge(self) -> str:
        return _challenge()

    def verify(self, token: str) -> Session:
        """Return the session, or raise :class:`UnauthenticatedError`/:class:`AudienceMismatchError`.

        The order is deliberate: shape, then algorithm, then signature, then claims. Nothing from the
        payload is read as a fact before the signature that covers it has been checked.
        """
        if not token or not token.strip():
            raise UnauthenticatedError("no bearer token presented", challenge=self.challenge)
        parts = token.strip().split(".")
        if len(parts) != 3:
            raise UnauthenticatedError(
                "a JWT has three dot-separated segments", challenge=self.challenge
            )
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii", "strict")

        header = _json_object(_b64url_decode(parts[0], what="header"), what="header")
        if header.get("alg") != _SUPPORTED_ALG:
            # This is the algorithm-confusion refusal, and it is why the comparison is against a
            # literal: `none`, `RS256` and a missing `alg` all land here.
            raise UnauthenticatedError(
                f"the token's alg must be {_SUPPORTED_ALG}; "
                f"got {header.get('alg')!r}. Unsigned tokens are never accepted.",
                detail={"alg": header.get("alg")},
            )
        if "crit" in header:
            # RFC 7515: a recipient that does not understand `crit` must reject the token. We
            # understand none, so a token that requires any is refused rather than ignored past.
            raise UnauthenticatedError("the token requires a critical header we do not implement")

        expected = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        try:
            presented = _b64url_decode(parts[2], what="signature")
        except UnauthenticatedError:
            raise
        if not hmac.compare_digest(presented, expected):
            raise UnauthenticatedError("the token's signature does not verify")

        claims = _json_object(_b64url_decode(parts[1], what="payload"), what="payload")
        return self._validate_claims(claims, token.strip())

    def _validate_claims(self, claims: Mapping[str, Any], token: str) -> Session:
        for name in _REQUIRED_CLAIMS:
            if name not in claims:
                raise UnauthenticatedError(f"the token is missing the {name!r} claim")

        if claims["iss"] != self.issuer:
            raise UnauthenticatedError(
                "the token was issued by a different issuer", detail={"iss": claims["iss"]}
            )

        claimed_audience = claims["aud"]
        audiences = claimed_audience if isinstance(claimed_audience, list) else [claimed_audience]
        if self.audience not in audiences:
            # §9 in one branch: the token is authentic and is not for this surface.
            raise AudienceMismatchError(
                f"this session is for audience {self.audience!r}",
                detail={"aud": claimed_audience, "expected": self.audience},
            )

        if claims["kind"] != self.kind.value:
            raise AudienceMismatchError(
                f"this route accepts {self.kind.value} sessions",
                detail={"kind": claims["kind"], "expected": self.kind.value},
            )

        now = self._clock.now()
        expires_at = _numeric_date(claims, "exp")
        if expires_at is None:
            raise UnauthenticatedError("the token has no expiry")
        if now > expires_at + self.leeway:
            raise UnauthenticatedError(
                "the session has expired", detail={"expired_at": expires_at.isoformat()}
            )

        not_before = _numeric_date(claims, "nbf")
        if not_before is not None and now + self.leeway < not_before:
            raise UnauthenticatedError("the session is not valid yet")

        issued_at = _numeric_date(claims, "iat")
        if issued_at is not None and issued_at > now + self.leeway:
            raise UnauthenticatedError(
                "the token was issued in the future",
                detail={"issued_at": issued_at.isoformat()},
            )

        subject = claims["sub"]
        if not isinstance(subject, str) or not subject.strip():
            raise UnauthenticatedError("the token's subject is empty")

        patient_ref = claims.get("patient_ref")
        if self.kind is SessionKind.patient:
            # A patient session with no patient is not a scoped session; every workflow read is
            # scoped by this value, so an empty one would scope to nothing.
            if not isinstance(patient_ref, str) or not patient_ref.strip():
                raise UnauthenticatedError("a patient session must carry a patient_ref")
        elif patient_ref is not None and patient_ref != "":
            raise UnauthenticatedError("an admin session must not carry a patient_ref")

        return Session(
            subject=subject.strip(),
            kind=self.kind,
            audience=self.audience,
            patient_ref=patient_ref.strip()
            if isinstance(patient_ref, str) and patient_ref
            else None,
            issued_at=issued_at or now,
            expires_at=expires_at,
            token_fingerprint=_fingerprint(token),
        )


class JwtIssuer:
    """Mints the local fixture tokens the demo and the tests use.

    Deliberately in the same module as the verifier: the issuer produces exactly the shape the
    verifier accepts, and a signature change on one side without the other is a test failure rather
    than a runtime surprise. The demo profile's `/v1/auth/*` routes are the only callers; §9's Cloud
    Run profile replaces those with an audience-checked service identity and keeps the verifier.
    """

    def __init__(
        self,
        secret: str,
        *,
        kind: SessionKind,
        audience: str,
        issuer: str,
        ttl_seconds: int,
        clock: Clock,
    ) -> None:
        if not secret.strip():
            raise ValueError(f"the {kind.value} signing secret is empty; refusing to issue tokens")
        self._secret = secret.strip().encode("utf-8")
        self.kind = kind
        self.audience = audience
        self.issuer = issuer
        self.ttl_seconds = ttl_seconds
        self._clock = clock

    def issue(
        self,
        subject: str,
        *,
        patient_ref: str | None = None,
        ttl_seconds: int | None = None,
    ) -> str:
        """Sign a token for `subject`. `patient_ref` is required for a patient session."""
        if not subject.strip():
            raise ValueError("a session needs a subject")
        if self.kind is SessionKind.patient and not (patient_ref or "").strip():
            raise ValueError("a patient session needs a patient_ref")
        now = self._clock.now()
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": subject.strip(),
            "kind": self.kind.value,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=ttl_seconds or self.ttl_seconds)).timestamp()),
        }
        if patient_ref:
            claims["patient_ref"] = patient_ref.strip()
        header = {"alg": _SUPPORTED_ALG, "typ": "JWT"}
        signing_input = (
            f"{_b64url_encode(json.dumps(header, separators=(',', ':')).encode('utf-8'))}."
            f"{_b64url_encode(json.dumps(claims, separators=(',', ':')).encode('utf-8'))}"
        ).encode("ascii")
        signature = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        return f"{signing_input.decode('ascii')}.{_b64url_encode(signature)}"


# ---------------------------------------------------------------------------
# The FastAPI seam
# ---------------------------------------------------------------------------


def build_verifiers(settings: GatewaySettings, clock: Clock) -> tuple[JwtVerifier, JwtVerifier]:
    """The patient and admin verifiers, from the two secrets §9 keeps apart."""
    patient = JwtVerifier(
        settings.patient_secret,
        kind=SessionKind.patient,
        audience=settings.patient_audience,
        issuer=settings.jwt_issuer,
        leeway_seconds=settings.clock_skew_seconds,
        clock=clock,
    )
    admin = JwtVerifier(
        settings.admin_secret,
        kind=SessionKind.admin,
        audience=settings.admin_audience,
        issuer=settings.jwt_issuer,
        leeway_seconds=settings.clock_skew_seconds,
        clock=clock,
    )
    return patient, admin


def _bearer(
    verifier: JwtVerifier, authorization: str | None, *, allow_query_token: bool, token: str | None
) -> Session:
    """The shared body of both dependencies.

    `authorization` is a header; `token` is a query parameter, and it is only ever consulted by the
    stream route — see :func:`patient_session_dependency`. Neither the trace id nor any other header
    reaches this function, which is the point.
    """
    if authorization:
        scheme, _, credential = authorization.strip().partition(" ")
        if scheme.lower() != AUTH_SCHEME.lower() or not credential.strip():
            raise UnauthenticatedError(
                f"expected a {AUTH_SCHEME} credential in the Authorization header",
                challenge=verifier.challenge,
            )
        return verifier.verify(credential.strip())
    if allow_query_token and token:
        # Documented trade, not an oversight: `EventSource` cannot set request headers, so a browser
        # stream cannot carry an Authorization header at all. The query parameter is reachable only
        # in the demo profile (`settings.is_demo`), and a token in a URL must be assumed to reach
        # logs — so this is a local-fixture accommodation and the Cloud Run profile replaces it with
        # a short-lived stream ticket or a same-origin cookie.
        return verifier.verify(token)
    raise UnauthenticatedError(
        f"no {AUTH_SCHEME} credential presented ({TRACE_ID_HEADER} is correlation, not a credential)",
        challenge=verifier.challenge,
    )


def patient_session_dependency(
    verifier: JwtVerifier, *, allow_query_token: bool = False
) -> Callable[..., Session]:
    """A FastAPI dependency that yields a patient :class:`Session` or refuses the request.

    The return type is `Callable[..., Session]` rather than the parameter list below: what FastAPI
    calls, and reads the parameter names and annotations off at request time, is the inner function
    itself, so restating its parameters here would be a second copy of the dependency's signature
    that nothing checks against the first.
    """

    def require_patient_session(
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
        token: Annotated[str | None, Query()] = None,
    ) -> Session:
        return _bearer(verifier, authorization, allow_query_token=allow_query_token, token=token)

    return require_patient_session


def admin_session_dependency(verifier: JwtVerifier) -> Callable[..., Session]:
    """A FastAPI dependency that yields an admin :class:`Session` or refuses the request.

    No query-parameter form here on purpose: an admin surface is not reached by `EventSource`, so it
    has no reason to accept a credential in a URL.
    """

    def require_admin_session(
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> Session:
        return _bearer(verifier, authorization, allow_query_token=False, token=None)

    return require_admin_session
