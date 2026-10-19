# ruff: noqa: RUF002  # Turkish quoted from §9 of ReferralEasy_Architecture_v3.md is kept verbatim
"""What the gateway refuses, and the one place a refusal becomes an HTTP status (§7, §9).

Two families of failure reach a caller, and they are deliberately shaped the same way so the web
client needs one handler rather than two:

* **The gateway's own refusals** — no session, the wrong audience, a workflow that is not yours, a
  stale `workflow_state_version` on a `ClientAction`, a body the contract cannot have produced.
* **The database's refusals** — `packages/domain/errors.py` translates each SQLSTATE the migrations
  raise into a typed exception, and this module maps that type to a status code.

The mapping is a single dictionary rather than a chain of `isinstance` checks wherever a refusal is
raised. §9 checks ownership and scope on *every* action; that only stays true if there is one table
to add a row to when a rule is added, instead of one `except` clause per endpoint to forget.

An unrecognised `DomainError` maps to 500, not 409: a refusal the gateway does not recognise is a
refusal it cannot explain, and reporting "conflict" for it would invite a client to retry something
that is wrong on our side. The same direction applies to `RE008` (append-only): mutating an
append-only row is a programming error, never something a caller did.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from referraleasy_domain import (
    AppendOnlyViolationError,
    ApprovalRefusedError,
    CommandConflictError,
    DomainError,
    HumanOwnedError,
    IllegalTransitionError,
    OutboxClaimError,
    PolicyDeniedError,
    StaleOwnershipError,
)

__all__ = [
    "DEFAULT_CHALLENGE",
    "ERROR_ENVELOPE_OPENAPI",
    "REFUSAL_STATUS",
    "AudienceMismatchError",
    "BookingHaltedError",
    "ContractViolationError",
    "GatewayError",
    "GuardrailUnavailableError",
    "InputBlockedError",
    "NotFoundError",
    "UnauthenticatedError",
    "refusal_status",
    "to_envelope",
]

#: The `WWW-Authenticate` challenge for this service, declared once. It lives here rather than in
#: `auth` so that a 401 raised from *anywhere* carries it: the scheme and the realm are properties of
#: the service, and a raise site that had to name them again is a raise site that can forget.
DEFAULT_CHALLENGE: Final = 'Bearer realm="referraleasy"'

#: Domain refusal -> HTTP status. Keyed by exception class and walked through the MRO, so a future
#: subclass of, say, `PolicyDeniedError` inherits the status of the rule it refines.
REFUSAL_STATUS: Final[Mapping[type[DomainError], int]] = {
    # §7: the caller's view of who owns the work, or of how far along it is, is behind.
    StaleOwnershipError: 409,
    # §7/§9: an illegal edge, or a state version that no longer matches. A stale ClientAction
    # lands here, which is what "refused rather than applied" means concretely.
    IllegalTransitionError: 409,
    # §11: automation is suspended while a human holds the workflow. Not a conflict the caller can
    # fix by retrying harder.
    HumanOwnedError: 423,
    # §9: consent is missing, expired, bound to a different payload, or already consumed.
    ApprovalRefusedError: 403,
    # §7: one command_id, one outcome. Reuse with a different body is refused, not coerced.
    CommandConflictError: 409,
    # §4/§9: the policy table does not permit this agent/tool in this state.
    PolicyDeniedError: 403,
    # §7: the outbox row is held by another worker.
    OutboxClaimError: 409,
    # RE008 and anything unknown: our side is wrong, and saying "conflict" would hide that.
    AppendOnlyViolationError: 500,
    DomainError: 500,
}

#: Stable machine-readable name per SQLSTATE. The `code` field is what a client branches on; the
#: `sqlstate` field is the database's own identifier, carried so a log line and a test can name the
#: exact rule that refused.
_CODE_BY_SQLSTATE: Final[Mapping[str, str]] = {
    "RE001": "stale_ownership",
    "RE002": "illegal_transition",
    "RE003": "human_owned",
    "RE004": "approval_refused",
    "RE005": "command_conflict",
    "RE006": "policy_denied",
    "RE007": "outbox_claim",
    "RE008": "append_only",
}


@dataclass
class GatewayError(Exception):
    """A refusal the gateway itself produces.

    `headers` exists for exactly one caller-visible case today — the `WWW-Authenticate` challenge on
    a 401 — but it is part of the base class so a new refusal cannot be raised without deciding what
    the client is supposed to do with it.
    """

    status_code: int
    code: str
    message: str
    detail: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    sqlstate: str | None = None

    def __post_init__(self) -> None:
        # The base `Exception` still carries the message, so tracebacks and logs read correctly
        # without every call site repeating it.
        Exception.__init__(self, self.message)

    def to_envelope(self, *, request_id: str) -> dict[str, Any]:
        return to_envelope(self, request_id=request_id)


class UnauthenticatedError(GatewayError):
    """401 — no usable credential. §9: a trace id is not a credential.

    The `WWW-Authenticate` challenge defaults to this service's realm, and that default is the point:
    a 401 without a challenge is a 401 a client cannot act on, and the way that happens is a raise
    site that had to remember. `def err(): raise UnauthenticatedError("...")` must produce a complete
    response on its own, which is what the tests assert by checking the header on *every* refusal.
    """

    def __init__(self, message: str, *, challenge: str | None = None, detail: Any = None) -> None:
        super().__init__(
            status_code=401,
            code="unauthenticated",
            message=message,
            detail=detail,
            headers={"WWW-Authenticate": challenge or DEFAULT_CHALLENGE},
        )


class AudienceMismatchError(GatewayError):
    """403 — the credential is valid but is not for this surface.

    A patient session presenting itself at an admin route is the case §9 names explicitly ("Hasta/
    admin oturumları ve servis kimlikleri ayrıdır"). 403 rather than 401: re-authenticating as the
    same kind of principal would not help, and a 401 would invite exactly that.
    """

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(status_code=403, code="audience_mismatch", message=message, detail=detail)


class NotFoundError(GatewayError):
    """404 — no such workflow *for this caller*.

    Also the answer when a patient asks for another patient's workflow. §9 scopes every action to
    the workflow's owner; answering 403 there would confirm that the workflow exists, which is a
    fact about another patient.
    """

    def __init__(self, message: str = "no such workflow", *, detail: Any = None) -> None:
        super().__init__(status_code=404, code="not_found", message=message, detail=detail)


class ContractViolationError(GatewayError):
    """422 — the body is not something the frozen contract could have produced (§1.1).

    Distinct from a policy or version refusal: nothing about this request can be applied, and a
    client that retries it unchanged will fail again.
    """

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(status_code=422, code="contract_violation", message=message, detail=detail)


class GuardrailUnavailableError(GatewayError):
    """503 — a write needs the guardrail dependency and it is not configured (§9).

    The rule is "Guardrail dependency kapalıyken booking yazılamaz": with the layers unavailable the
    gateway cannot screen the input a write depends on, so the write is refused. Reads that need no
    screening — a static help card — keep working, which is the other half of that rule.
    """

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(
            status_code=503, code="guardrails_not_configured", message=message, detail=detail
        )


class BookingHaltedError(GatewayError):
    """503 — the admin kill switch has stopped booking writes (§16).

    503 rather than 403: this is a condition of the *service*, not of the caller. The same operator
    who was allowed to book a minute ago is refused now, and re-authenticating or asking for a
    different permission would not help. The alternative that reads well — 409, "conflict with the
    current state" — would suggest the request could succeed after a retry, which is the one thing
    that is not true while the switch is engaged.

    The detail carries the operator's own reason and when they gave it, because a halt with no stated
    reason is indistinguishable from a bug.
    """

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(status_code=503, code="booking_halted", message=message, detail=detail)


class InputBlockedError(GatewayError):
    """422 — the guardrail refused the input itself (§9 layer 1).

    Distinct from :class:`GuardrailUnavailableError`: here the layers ran and *decided*. The
    distinction matters to an operator debugging the demo (a refusal that means "not configured" is
    a deployment problem; one that means "blocked" is the guardrail working) and to a client, which
    can act on a category and cannot act on an absence.

    `detail` always carries the `category`, because "blocked" alone is not actionable and §9's
    categories (`prompt_injection`, `emergency`, `abuse`, …) each imply a different next step. For an
    emergency it also carries the approved fixed response, so a client has something to show the
    person rather than an error string.
    """

    def __init__(self, message: str, *, category: str, detail: Any = None) -> None:
        payload: dict[str, Any] = {"category": category}
        if detail:
            payload.update(detail)
        super().__init__(status_code=422, code="guardrail_blocked", message=message, detail=payload)


def refusal_status(error: DomainError) -> int:
    """The status for a database refusal, by the most specific class it belongs to."""
    for cls in type(error).__mro__:
        if cls in REFUSAL_STATUS:
            return REFUSAL_STATUS[cls]
    return 500  # pragma: no cover - DomainError is the last entry in the table


def to_envelope(error: GatewayError | DomainError, *, request_id: str) -> dict[str, Any]:
    """Render a refusal as the one error shape every non-2xx in this service uses.

    Shaped like `services/mock-api`'s envelope (`{"error": {...}}`) so the demo has a single error
    contract across its services, with `sqlstate` added when the refusal came from the database.
    """
    if isinstance(error, DomainError):
        sqlstate = error.sqlstate or None
        return {
            "error": {
                "code": _CODE_BY_SQLSTATE.get(sqlstate, "domain_refusal")
                if sqlstate
                else "domain_refusal",
                "sqlstate": sqlstate,
                "message": str(error),
                "request_id": request_id,
                "detail": None,
            }
        }
    return {
        "error": {
            "code": error.code,
            "sqlstate": error.sqlstate,
            "message": error.message,
            "request_id": request_id,
            "detail": error.detail,
        }
    }


#: The envelope, as an OpenAPI response component. Referenced by route declarations so the published
#: document describes the failures as well as the successes.
ERROR_ENVELOPE_OPENAPI: Final[dict[str, Any]] = {
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {
            "type": "object",
            "required": ["code", "message", "request_id"],
            "properties": {
                "code": {"type": "string"},
                "sqlstate": {"type": ["string", "null"]},
                "message": {"type": "string"},
                "request_id": {"type": "string"},
                "detail": {},
            },
        }
    },
}
