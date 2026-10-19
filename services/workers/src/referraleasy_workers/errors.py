"""The refusals the workers name themselves — and the ones they refuse to re-name (§7, §11).

Two rules decide what is in this module.

**A refusal the database makes is not re-declared here.** `RE001`..`RE008` are the control-state
schema's, they are typed in :mod:`referraleasy_domain.errors`, and the workers raise *those* classes
for *those* conditions. They are re-exported below so a caller has one import for everything a
worker can raise, but not one line of this module decides that a stale lease is stale — that
decision is a `WHERE` clause in SQL and the exception is the translation of its miss.

**A condition the database has no opinion about is named here, and only then.** Three such
conditions exist in the worker processes:

* :class:`SecondKeyRefusedError` — a booking lookup was about to be issued under a key other than the
  one the original attempt used (§7, quoted verbatim below). The database cannot
  refuse this one: a second key is a *valid* booking request, which is exactly why it is dangerous.
  The tripwire therefore has to be in Python, at the one place that could mint a key.
* :class:`ConsentRequiredError` — a handoff was about to be queued or read from without the patient's
  consent recorded (§11). The `handoff` table has no `consent_given_at` column, so the gate is here
  (see :mod:`referraleasy_workers.hitl` for what that means and what it does not).
* :class:`RecoveryRefusedError` — the state machine permits *no* action for this failure from this
  state. Recovery's contract is to choose among permitted actions (§3); when there are none, the
  honest answer is an exception that says so, not a transition someone invented.

What is deliberately **not** here is as load-bearing as what is: there is no `OperatorUnavailableError`.
An operator who cannot be reached produces a pending ticket and directory contact details (§11),
which is an outcome a patient is told about — not an exception a worker throws past them.
"""

from __future__ import annotations

from typing import Any, Final

from referraleasy_domain.errors import (
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

#: §7, quoted verbatim: "İkinci bağımsız booking key üretilmez" — a second, independent booking key is never  # noqa: RUF003
#: generated. The one requirement in this package that has to live in Python: a second key is a
#: valid request, which is exactly what makes it dangerous.
__all__ = [
    "AppendOnlyViolationError",
    "ApprovalRefusedError",
    "CommandConflictError",
    "ConsentRequiredError",
    "DomainError",
    "HumanOwnedError",
    "IllegalTransitionError",
    "OutboxClaimError",
    "PolicyDeniedError",
    "RecoveryRefusedError",
    "SecondKeyRefusedError",
    "StaleOwnershipError",
    "WorkerError",
    "ensure_same_key",
]


class WorkerError(RuntimeError):
    """Base for the conditions a worker recognises and a database cannot."""


class SecondKeyRefusedError(WorkerError):
    """A second, independent booking key was about to be used (§7).

    The dangerous case is not a wrong key — it is a *plausible* one. A fresh key turns every lookup
    into a second booking request, and a second request is how a patient ends up with two
    appointments, so this is refused before the request leaves the process.
    """

    def __init__(self, message: str, *, original_key: str, candidate_key: str) -> None:
        super().__init__(message)
        self.original_key = original_key
        self.candidate_key = candidate_key


class ConsentRequiredError(WorkerError):
    """A handoff was queued or read from without patient consent (§11).

    "No consent, no share": the patient reviews the exact summary before any part of it reaches a
    person. `consent_given_at` is the gate, and an un-consented handoff is not queued.
    """

    def __init__(self, message: str, *, handoff_id: Any | None = None) -> None:
        super().__init__(message)
        self.handoff_id = handoff_id


class RecoveryRefusedError(WorkerError):
    """No recovery action is permitted from this state (§3).

    Recovery chooses among the actions the state machine permits. This exception means the
    intersection was empty: the workflow is in a state with no outgoing edge the failure class can
    use, and the only honest response is to stop and say so rather than to pick a state the machine
    does not have.
    """

    def __init__(self, message: str, *, workflow_state: str, failure_class: str) -> None:
        super().__init__(message)
        self.workflow_state = workflow_state
        self.failure_class = failure_class


#: The hint every second-key refusal carries. Kept as a constant because the tests assert on it and
#: because the wording is the argument, not decoration.
SECOND_KEY_HINT: Final[str] = (
    "Reconcile with the idempotency key the attempt was sent with; a new key is a new booking."
)


def ensure_same_key(original_key: str, candidate_key: str) -> str:
    """Return ``candidate_key`` only when it *is* the original key; refuse otherwise.

    Called at the one place that could mint a key — immediately before a lookup is issued — so a
    future refactor that decides to "try a fresh key to be sure" fails loudly in the worker tests
    instead of quietly at a hospital.
    """
    if candidate_key != original_key:
        raise SecondKeyRefusedError(
            f"refused: lookup key {candidate_key!r} differs from the attempt key {original_key!r}. "
            + SECOND_KEY_HINT,
            original_key=original_key,
            candidate_key=candidate_key,
        )
    return candidate_key
