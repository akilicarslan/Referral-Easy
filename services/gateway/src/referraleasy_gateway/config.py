"""Gateway settings, read from the environment once at startup (§19, §9).

The environment contract is §19's; the variable names are the ones `.env.example` already carries
(`JWT_PATIENT_SECRET`, `JWT_ADMIN_SECRET`, `GUARDRAIL_MODE`, `DATABASE_URL`), so the gateway needs no
new name to be added to the demo's environment. Nothing secret has a default: a blank signing key
would authenticate every token, so it is a startup failure rather than a wildcard — the same rule
`packages/agent-runtime` applies to `A2A_SERVICE_TOKEN`.

Two profiles come out of this:

* **memory** (the default) — no database. The control-state port is served by `InMemoryControlState`
  and the policy table comes from `policy_seed`. This is what the unit tests run against and what
  `make demo` can start with nothing but Python.
* **postgres** — `GATEWAY_CONTROL_BACKEND=postgres`, `DATABASE_URL` set. The port is served by the
  `packages/domain` stores, so the rules stay in SQL where §7 puts them.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Self
from urllib.parse import urlsplit

from referraleasy_gateway.release import resolve_release_manifest_id

__all__ = [
    "CONTROL_BACKEND_ENV",
    "DEFAULT_ADMIN_AUDIENCE",
    "DEFAULT_ISSUER",
    "DEFAULT_PATIENT_AUDIENCE",
    "OFFLINE_PROFILE_LABEL",
    "WORKER_ID_ENV",
    "ConfigurationError",
    "GatewaySettings",
]

#: §18: the offline fixture profile is always labelled, and every artefact is `synthetic: true`.
OFFLINE_PROFILE_LABEL: Final = "OFFLINE FIXTURE PROFILE — canned model responses"

#: §9: patient and admin sessions are separate. The audience is what makes them separate — a token
#: minted for one is not accepted by a route declared for the other, and that is checked by the
#: verifier rather than by each endpoint.
DEFAULT_PATIENT_AUDIENCE: Final = "referraleasy-patient"
DEFAULT_ADMIN_AUDIENCE: Final = "referraleasy-admin"
DEFAULT_ISSUER: Final = "referraleasy-local"

#: The variable that selects the control-state port implementation.
CONTROL_BACKEND_ENV: Final = "GATEWAY_CONTROL_BACKEND"

#: The identity this process claims workflows under (§7). Only the postgres profile reads it — the
#: in-memory one has no lease to take — and it is what `workflow.owner_worker_id` is set to. It is
#: settable because a deployment may want a name that survives a restart (a Cloud Run revision, a
#: hostname behind a load balancer) rather than the ephemeral default below.
WORKER_ID_ENV: Final = "GATEWAY_WORKER_ID"

#: The `.env.example` placeholder prefix. `scripts/validate_env.py` refuses a `live` backend that
#: still carries one, and the gateway applies the same rule to its own two secrets: a signing key
#: that is still the template value is a signing key every reader of the public repository knows.
PLACEHOLDER_PREFIX: Final = "CHANGEME"


class ConfigurationError(RuntimeError):
    """The environment does not describe a runnable gateway. Raised at startup, never mid-request."""


def _int(env: Mapping[str, str], name: str, fallback: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - a malformed env var is a deployment mistake
        raise ConfigurationError(f"{name}={raw!r} is not an integer") from exc


def _bool(env: Mapping[str, str], name: str, fallback: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return fallback
    return raw not in {"0", "false", "no", "off"}


def _url_host(url: str) -> str | None:
    """`host:port` from a URL, or `None`. Never the userinfo, never the query.

    The one thing `describe()` logs about the telemetry cluster. A URL may carry credentials
    (`http://user:password@host:9200`), and every string-slicing shortcut that looks safe here does
    not: `Path(url).name` returns `password@host:9200`, and a log line is where a password stops
    being a secret. `splitport` on `hostname` rather than `netloc` for the same reason — `netloc` is
    the field that contains the credentials.
    """
    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover - urlsplit raises for a malformed IPv6 literal
        return None
    if not parts.hostname:
        return None
    return f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    """Everything the app needs to be built. Immutable, so a test can share one safely."""

    # -- environment -------------------------------------------------------------------------
    app_env: str = "demo"
    host: str = "127.0.0.1"
    port: int = 8080
    public_url: str = ""

    # -- sessions (§9) -----------------------------------------------------------------------
    jwt_issuer: str = DEFAULT_ISSUER
    patient_audience: str = DEFAULT_PATIENT_AUDIENCE
    admin_audience: str = DEFAULT_ADMIN_AUDIENCE
    patient_secret: str = ""
    admin_secret: str = ""
    session_ttl_seconds: int = 3600
    #: Clock skew tolerated on `exp`/`nbf`/`iat`. Small on purpose: it is a tolerance for two
    #: machines disagreeing by seconds, not a grace period for an expired session.
    clock_skew_seconds: int = 30

    # -- guardrails (§9) ---------------------------------------------------------------------
    guardrail_mode: str = "three_stage"
    #: Layer 2's pinned model id. Empty means *not configured*, which is a refusal for writes, not
    #: a fallback to "allow" — see `guardrails.GuardrailPipeline`.
    guardrail_decision_model: str = ""
    #: Layer 3's pinned local artifact. Empty means not configured, as above.
    guardrail_local_model: str = ""
    guardrail_max_message_chars: int = 2000

    # -- the stream (§12) --------------------------------------------------------------------
    #: How long a stream holds open waiting for new events after it has replayed the log. `0`
    #: drains and closes, which is how the tests consume a stream with a plain request.
    stream_hold_ms: int = 25_000
    stream_heartbeat_ms: int = 15_000
    #: Ceiling on how many events one replay delivers, so a long-lived workflow cannot be replayed
    #: into an unbounded response.
    stream_replay_limit: int = 500

    # -- control state (§7) ------------------------------------------------------------------
    control_backend: str = "memory"
    database_url: str = ""
    #: The worker identity the postgres profile claims workflows under. Empty means "derive one from
    #: this process", which is what `resolved_worker_id` does.
    worker_id: str = ""
    #: Whether this deployment holds only synthetic data (§18). The gateway stamps every event with
    #: it, and no other value is reachable from this codebase.
    synthetic: bool = True

    # -- the admin read API (§12, ADR-0039) ----------------------------------------------------
    #: The telemetry cluster the nine admin pages read. Empty means *no source*, which is a supported
    #: state rather than a misconfiguration: §18's core profile runs without OpenSearch, and a stack
    #: whose admin charts have no data is not a stack that should refuse every booking.
    opensearch_url: str = ""

    # -- dispatching to the coordinator (§3, §5) ------------------------------------------------
    #: The peer pattern `A2A_BASE_URL` carries for the agents, with `{role}` substituted. Empty
    #: means *no dispatch*: a deployment that cannot reach an agent records commands and nothing
    #: decides them, which is a supported state — it is what the unit suite runs in — and it is
    #: reported rather than guessed at.
    a2a_base_url: str = ""
    #: The service token an A2A request is authenticated with. Both of these come from the same
    #: shared env block the seven agents read, so the stack configures them once.
    a2a_service_token: str = ""
    #: The release every dispatched task is pinned to (§13). Resolved from `RELEASE_MANIFEST_ID`
    #: or the built `registry/releases/latest.json` — see `referraleasy_gateway.release`. Empty
    #: means no pin could be named, and the dispatcher reports itself unconfigured rather than
    #: sending a task under an invented release.
    release_manifest_id: str = ""

    def __post_init__(self) -> None:
        if not self.patient_secret.strip() or not self.admin_secret.strip():
            raise ConfigurationError(
                "JWT_PATIENT_SECRET and JWT_ADMIN_SECRET must both be set. A blank signing key "
                "would authenticate every token, so the gateway refuses to start rather than "
                "serve open sessions (§9)."
            )
        if self.patient_audience == self.admin_audience:
            raise ConfigurationError(
                "the patient and admin audiences are equal, so one session kind would satisfy the "
                "other's routes. §9 requires them to be separate."
            )
        if not self.is_demo:
            # Same rule as `scripts/validate_env.py`, and for the same reason: this repository is
            # public, so `CHANGEME_LOCAL_FIXTURE_SECRET` is a published signing key. The demo profile
            # is exempt because it *is* the fixture profile, and it is labelled as one everywhere it
            # answers (`fixture: true`, `synthetic: true`).
            unresolved = sorted(
                name
                for name, value in (
                    ("JWT_PATIENT_SECRET", self.patient_secret),
                    ("JWT_ADMIN_SECRET", self.admin_secret),
                )
                if value.startswith(PLACEHOLDER_PREFIX)
            )
            if unresolved:
                raise ConfigurationError(
                    f"{', '.join(unresolved)} still carries the .env.example placeholder. In "
                    f"APP_ENV={self.app_env!r} the gateway would be signing sessions with a value "
                    "published in this repository; set a real secret, or use the demo profile where "
                    "the fixture secrets are the point and every answer says so."
                )
        if self.control_backend not in {"memory", "postgres"}:
            raise ConfigurationError(
                f"{CONTROL_BACKEND_ENV}={self.control_backend!r} is not 'memory' or 'postgres'"
            )
        if self.control_backend == "postgres" and not self.database_url.strip():
            raise ConfigurationError(
                f"{CONTROL_BACKEND_ENV}=postgres needs DATABASE_URL. The PostgreSQL profile keeps "
                "the rules in the database (§7); it cannot fall back to the in-memory one."
            )
        if self.stream_hold_ms < 0 or self.stream_heartbeat_ms < 0:
            raise ConfigurationError("stream timings cannot be negative")

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        **overrides: Any,
    ) -> Self:
        """Build settings from the process environment, then apply explicit overrides.

        Overrides are how the tests pin a fixture secret and a zero stream hold without touching
        the real environment.
        """
        source = os.environ if env is None else env
        port = _int(source, "PORT", 8080)
        settings = cls(
            app_env=source.get("APP_ENV", "demo").strip() or "demo",
            host=source.get("HOST", "127.0.0.1"),
            port=port,
            public_url=source.get("GATEWAY_URL", f"http://localhost:{port}"),
            jwt_issuer=source.get("JWT_ISSUER", DEFAULT_ISSUER).strip() or DEFAULT_ISSUER,
            patient_audience=source.get("JWT_PATIENT_AUDIENCE", DEFAULT_PATIENT_AUDIENCE),
            admin_audience=source.get("JWT_ADMIN_AUDIENCE", DEFAULT_ADMIN_AUDIENCE),
            patient_secret=source.get("JWT_PATIENT_SECRET", "").strip(),
            admin_secret=source.get("JWT_ADMIN_SECRET", "").strip(),
            session_ttl_seconds=_int(source, "JWT_TTL_SECONDS", 3600),
            clock_skew_seconds=_int(source, "JWT_CLOCK_SKEW_SECONDS", 30),
            guardrail_mode=source.get("GUARDRAIL_MODE", "three_stage").strip() or "three_stage",
            guardrail_decision_model=source.get("GUARDRAIL_DECISION_MODEL", "").strip(),
            guardrail_local_model=source.get("GUARDRAIL_LOCAL_MODEL", "").strip(),
            guardrail_max_message_chars=_int(source, "GUARDRAIL_MAX_MESSAGE_CHARS", 2000),
            stream_hold_ms=_int(source, "GATEWAY_STREAM_HOLD_MS", 25_000),
            stream_heartbeat_ms=_int(source, "GATEWAY_STREAM_HEARTBEAT_MS", 15_000),
            stream_replay_limit=_int(source, "GATEWAY_STREAM_REPLAY_LIMIT", 500),
            control_backend=source.get(CONTROL_BACKEND_ENV, "memory").strip().lower() or "memory",
            database_url=source.get("DATABASE_URL", "").strip(),
            worker_id=source.get(WORKER_ID_ENV, "").strip(),
            opensearch_url=source.get("OPENSEARCH_URL", "").strip(),
            a2a_base_url=source.get("A2A_BASE_URL", "").strip(),
            a2a_service_token=source.get("A2A_SERVICE_TOKEN", "").strip(),
            release_manifest_id=resolve_release_manifest_id(source),
        )
        return replace(settings, **overrides) if overrides else settings

    @property
    def resolved_worker_id(self) -> str:
        """The identity this process writes into `workflow.owner_worker_id` (§7).

        §7 gives a workflow exactly one owning worker, and the database enforces it: a second worker
        presents its generation and is refused `RE001`. The driver's answer is to make the identity
        *this process* rather than a configured string, so two `uvicorn` workers cannot accidentally
        present as one and take each other's claims. `GATEWAY_WORKER_ID` overrides it for the case
        where an operator does want a name that outlives the process — a Cloud Run revision, say.
        """
        return self.worker_id.strip() or f"gateway:{socket.gethostname()}:{os.getpid()}"

    @property
    def guardrails_configured(self) -> bool:
        """Whether every layer §9 names has something behind it.

        Both model-backed layers count. A profile with the regex layer only is a profile where a
        booking must be refused, because the three layers are what the rule is about.
        """
        return bool(self.guardrail_decision_model) and bool(self.guardrail_local_model)

    @property
    def is_demo(self) -> bool:
        """Whether the local-fixture conveniences (a token issuer route, a query-param token) are
        reachable. In the Cloud Run profile §9 describes they are not, because there the session
        identity comes from an audience-checked service identity instead."""
        return self.app_env in {"demo", "test", "ci"}

    def describe(self) -> dict[str, Any]:
        """A loggable summary. No secret is included, by construction rather than by review."""
        return {
            "app_env": self.app_env,
            "public_url": self.public_url,
            "control_backend": self.control_backend,
            "guardrails_configured": self.guardrails_configured,
            "guardrail_mode": self.guardrail_mode,
            "synthetic": self.synthetic,
            "offline_profile_label": OFFLINE_PROFILE_LABEL,
            "database": Path(self.database_url or "").name or None,
            # The host, never the URL: an operator needs to know which cluster the admin pages read,
            # and any credentials in the URL are the reason a whole URL is not logged. Parsed rather
            # than sliced — `Path("http://user:secret@host:9200").name` is `"secret@host:9200"`,
            # which would put a password in the log line this exists to avoid.
            "telemetry_host": _url_host(self.opensearch_url),
            # §7: which worker a workflow claimed here belongs to. Logged because "who owns it" is
            # the first question asked of a stuck workflow, and the answer is a column, not a guess.
            "worker_id": self.resolved_worker_id if self.control_backend == "postgres" else None,
        }
