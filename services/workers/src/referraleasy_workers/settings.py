"""Worker configuration, resolved once at startup and validated before anything runs (§7, §11).

The numbers a worker needs are of three kinds, and they are kept separate on purpose:

* **The spec's ceilings** — attempt time, attempts, the turn deadline, the backoff curve. Those are
  :class:`~referraleasy_agent_runtime.budget.BudgetCaps`, resolved from the environment by the agent
  runtime, and *reused here rather than re-declared*: an `AGENT_MAX_ATTEMPTS` that means one thing to
  an agent and another to a worker is the drift this package exists to avoid.
* **The worker's own lease and queue shape** — how long a claim is good for, how many outbox rows one
  pass takes, how far back a failed delivery is pushed. These are `WORKER_*` variables.
* **Where the outside world is** — the mock booking API, the notification sink. A URL that is absent
  is not an error at import time: it is an error at the moment a worker tries to reach it, with the
  message naming the variable.

Everything is validated at construction (:class:`ConfigurationError` from the agent runtime), because
a cap that cannot be enforced would otherwise fail mid-turn — after a booking request has left.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from referraleasy_agent_runtime import BudgetCaps, ConfigurationError

__all__ = [
    "BOOKING_API_TOKEN_ENV",
    "BOOKING_API_URL_ENV",
    "DATABASE_URL_ENV",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_RECONCILE_LOOKUP_BUDGET",
    "NOTIFICATION_URL_ENV",
    "OUTBOX_BACKOFF_BASE_SECONDS_ENV",
    "OUTBOX_BACKOFF_CAP_SECONDS_ENV",
    "OUTBOX_CLAIM_LIMIT_ENV",
    "OUTBOX_LEASE_SECONDS_ENV",
    "OUTBOX_MAX_BATCHES_ENV",
    "RECONCILE_LOOKUP_BUDGET_ENV",
    "WORKER_ID_ENV",
    "WORKER_LEASE_SECONDS_ENV",
    "WORKER_POLL_SECONDS_ENV",
    "WorkerSettings",
    "default_worker_id",
]

# ── Environment variable names ───────────────────────────────────────────────────────────────
DATABASE_URL_ENV: Final = "DATABASE_URL"
WORKER_ID_ENV: Final = "WORKER_ID"
WORKER_LEASE_SECONDS_ENV: Final = "WORKER_LEASE_SECONDS"
WORKER_POLL_SECONDS_ENV: Final = "WORKER_POLL_SECONDS"
OUTBOX_CLAIM_LIMIT_ENV: Final = "WORKER_OUTBOX_CLAIM_LIMIT"
OUTBOX_LEASE_SECONDS_ENV: Final = "WORKER_OUTBOX_LEASE_SECONDS"
OUTBOX_BACKOFF_BASE_SECONDS_ENV: Final = "WORKER_OUTBOX_BACKOFF_BASE_SECONDS"
OUTBOX_BACKOFF_CAP_SECONDS_ENV: Final = "WORKER_OUTBOX_BACKOFF_CAP_SECONDS"
OUTBOX_MAX_BATCHES_ENV: Final = "WORKER_OUTBOX_MAX_BATCHES"
RECONCILE_LOOKUP_BUDGET_ENV: Final = "WORKER_RECONCILE_LOOKUP_BUDGET"
BOOKING_API_URL_ENV: Final = "BOOKING_API_URL"
BOOKING_API_TOKEN_ENV: Final = "BOOKING_API_TOKEN"
NOTIFICATION_URL_ENV: Final = "NOTIFICATION_URL"
NOTIFICATION_TOKEN_ENV: Final = "NOTIFICATION_TOKEN"

# ── Defaults ─────────────────────────────────────────────────────────────────────────────────
#: `workflow_claim`'s own default, restated as the worker's. A claim has to be long enough to cover
#: the step it guards and short enough that a dead worker's workflow is reclaimable promptly.
DEFAULT_LEASE_SECONDS: Final = 60
DEFAULT_POLL_SECONDS: Final = 5.0
#: How many lookups one reconciliation may spend before it escalates (§7: "Always bound and
#: escalate"). Five attempts by an agent that can take four (§8 allows three, the first included)
#: gives the outage a chance to end without ever making the patient's wait unbounded.
DEFAULT_RECONCILE_LOOKUP_BUDGET: Final = 5


def default_worker_id() -> str:
    """A stable-enough identity for this process: ``<hostname>:<pid>``.

    Lease ownership is a *string in a column*, and the only thing that must hold is that two live
    processes never present the same one. Host plus pid gives that without a registry, and keeps the
    value readable in `workflow.owner_worker_id` when someone is looking at why a claim was refused.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """Everything a worker process needs to know about itself and its environment."""

    worker_id: str = field(default_factory=default_worker_id)
    #: Absent means "this process does not open a database", which is a legitimate mode for the
    #: offline tests and for a process that only relays a URL. The moment a store is built, the
    #: missing value is an error naming the variable (see :func:`referraleasy_workers.db.connect`).
    database_url: str | None = None

    lease_seconds: int = DEFAULT_LEASE_SECONDS
    poll_seconds: float = DEFAULT_POLL_SECONDS

    outbox_claim_limit: int = 10
    outbox_lease_seconds: int = 60
    outbox_backoff_base_seconds: int = 5
    outbox_backoff_cap_seconds: int = 3600
    #: `None` is the store's "until the queue is empty". A finite value bounds one unit of work, which
    #: is what lets a single process share itself between the queue and the reconciliation pass.
    outbox_max_batches: int | None = None

    reconcile_lookup_budget: int = DEFAULT_RECONCILE_LOOKUP_BUDGET

    booking_api_url: str | None = None
    booking_api_token: str = ""
    notification_url: str | None = None
    notification_token: str = ""

    #: The §8 ceilings, resolved from the same environment the agents read. Never re-declared here.
    caps: BudgetCaps = field(default_factory=BudgetCaps.from_env)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WorkerSettings:
        """Resolve settings from ``env`` (defaults to ``os.environ``), failing on nonsense.

        A value that cannot be enforced is refused here rather than clamped silently: a
        `WORKER_LEASE_SECONDS=0` would make every claim instantly stale, and a worker that started
        with one would report every write as a lost lease — a failure mode that looks like a database
        problem and is actually a typo.
        """
        resolved: Mapping[str, str] = env if env is not None else os.environ
        return cls(
            worker_id=_text(resolved, WORKER_ID_ENV) or default_worker_id(),
            database_url=_text(resolved, DATABASE_URL_ENV),
            lease_seconds=_int_env(resolved, WORKER_LEASE_SECONDS_ENV, DEFAULT_LEASE_SECONDS),
            poll_seconds=_float_env(resolved, WORKER_POLL_SECONDS_ENV, DEFAULT_POLL_SECONDS),
            outbox_claim_limit=_int_env(resolved, OUTBOX_CLAIM_LIMIT_ENV, 10),
            outbox_lease_seconds=_int_env(resolved, OUTBOX_LEASE_SECONDS_ENV, 60),
            outbox_backoff_base_seconds=_int_env(resolved, OUTBOX_BACKOFF_BASE_SECONDS_ENV, 5),
            outbox_backoff_cap_seconds=_int_env(resolved, OUTBOX_BACKOFF_CAP_SECONDS_ENV, 3600),
            outbox_max_batches=_optional_int_env(resolved, OUTBOX_MAX_BATCHES_ENV),
            reconcile_lookup_budget=_int_env(
                resolved, RECONCILE_LOOKUP_BUDGET_ENV, DEFAULT_RECONCILE_LOOKUP_BUDGET
            ),
            booking_api_url=_text(resolved, BOOKING_API_URL_ENV),
            booking_api_token=_text(resolved, BOOKING_API_TOKEN_ENV) or "",
            notification_url=_text(resolved, NOTIFICATION_URL_ENV),
            notification_token=_text(resolved, NOTIFICATION_TOKEN_ENV) or "",
            caps=BudgetCaps.from_env(resolved),
        )

    def require_database_url(self) -> str:
        """The database URL, or a :class:`ConfigurationError` that names the variable."""
        if not self.database_url:
            raise ConfigurationError(
                f"{DATABASE_URL_ENV} is not set; the workers own their connections and cannot "
                "start without a control-state database"
            )
        return self.database_url

    def require_booking_api_url(self) -> str:
        """The booking API base URL, or a :class:`ConfigurationError` that names the variable."""
        if not self.booking_api_url:
            raise ConfigurationError(
                f"{BOOKING_API_URL_ENV} is not set; a worker that cannot reach the booking API "
                "must not pretend it can, because a booking attempt is not retryable past its key"
            )
        return self.booking_api_url

    def require_notification_url(self) -> str:
        """The notification sink's base URL, or a :class:`ConfigurationError` naming the variable."""
        if not self.notification_url:
            raise ConfigurationError(
                f"{NOTIFICATION_URL_ENV} is not set; the outbox worker delivers to a notification "
                "service and must not be started to fail on every row it claims"
            )
        return self.notification_url


def _text(env: Mapping[str, str], name: str) -> str | None:
    """Return a stripped value, treating empty as absent (an unset variable in a compose file)."""
    raw = env.get(name, "").strip()
    return raw or None


def _int_env(env: Mapping[str, str], name: str, default: int, *, minimum: int = 1) -> int:
    raw = _text(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


def _optional_int_env(env: Mapping[str, str], name: str, *, minimum: int = 1) -> int | None:
    if _text(env, name) is None:
        return None
    return _int_env(env, name, minimum, minimum=minimum)


def _float_env(env: Mapping[str, str], name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = _text(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number of seconds, got {raw!r}") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value
