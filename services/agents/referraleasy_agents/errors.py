"""The specialists' own refusals — the ones the runtime cannot make for them.

`referraleasy_agent_runtime` refuses anything the *runtime* owns: a tool outside a
role's allowlist, a tool the policy table forbids in this workflow state, a
budget that has run out, a refusal that arrives as the wrong type. Those checks
are data-driven and shared.

This module holds the other kind: refusals that follow from §3's
forbidden-authority column, which is a statement about *meaning* rather than
about a table. Matching may not relax a hard constraint; Coverage may not promise
that a visit is covered; Intake may not invent a specialty the referral does not
state; Readiness may not count a document that does not exist; Scheduling may not
write without a live approval; Recovery may not call the same failed agent
forever. Each is a rule a prompt could describe and a model could ignore, so each
is implemented as a check that raises.

Every class subclasses the runtime's :class:`AgentRuntimeError`, so
:func:`referraleasy_agent_runtime.project_failure` projects it through the
contracts table like any other typed failure. Nothing here re-states the
outcome→protocol mapping: a class that wants a non-default projection overrides
`outcome` or `event`, and the projection itself stays where ADR-0030 put it.
"""

from __future__ import annotations

from referraleasy_agent_runtime import AgentRuntimeError, ToolRefused
from referraleasy_contracts.generated.common import ResultOutcome

__all__ = [
    "BookingOutcomeUnknown",
    "ConsentNotRecorded",
    "CoverageGuaranteeRefused",
    "DuplicateBookingKeyRefused",
    "HardConstraintRelaxedRefused",
    "InventedSpecialtyRefused",
    "MissingDataError",
    "NoApprovalRefused",
    "OneWayDoorRefused",
    "ProvenanceMissingError",
    "UnknownDataError",
    "UnreadableDocumentRefused",
]


class MissingDataError(AgentRuntimeError):
    """A decision rested on a fact nobody retrieved.

    §3, second sub-agent rule: *"Constraint Analyst, may not conceal missing data with a score"* —
    the analyst may not cover missing data with a score. The same
    refusal is needed everywhere a "fit" could be asserted over an absent fact,
    so it lives here rather than in the matching module alone.

    Terminal on purpose: re-running the same turn will not produce the missing
    fact, and a retryable classification would invite exactly the loop that
    Recovery exists to end.
    """


class ProvenanceMissingError(MissingDataError):
    """A tool result arrived without the provenance §5 requires.

    Every claim in a decision carries an `evidence_id`; a candidate pool whose
    entries cannot be attributed to a retrieved record is not evidence, it is
    recollection.
    """


class UnknownDataError(MissingDataError):
    """An unknown was about to be rounded to a value.

    `satisfied: null` means unknown (§2.2's unknown handling, applied to
    constraints). Writing `False` where the truth is "we did not learn this" is
    how a data gap becomes a statement about a hospital.
    """


class InventedSpecialtyRefused(AgentRuntimeError):
    """§3, Referral Intake: *"may not select a specialty absent from the referral"*.

    A specialty the referral does not state is not inferred, not guessed from the
    chief complaint, and not derived from the specialty of a document. The
    referral either names one or the field is missing.
    """


class CoverageGuaranteeRefused(AgentRuntimeError):
    """§3, Coverage & Network: *"may not guarantee insurance coverage or payment"*.

    A network status is a statement about a record with a freshness date. A
    guarantee is a promise the platform is not entitled to make, so a
    patient-facing sentence that asserts one is refused before it is returned.
    """


class HardConstraintRelaxedRefused(AgentRuntimeError):
    """§3, Provider Matching: *"hard constraint esnetemez"*.

    A hard constraint changes only on an explicit patient decision, recorded as a
    `ConstraintChange` with `decided_by`. Widening a radius because nothing
    matched is the failure this refuses.
    """


class UnreadableDocumentRefused(AgentRuntimeError):
    """§3, Referral Readiness: *"olmayan belgeyi var sayamaz"*.

    A document that was not supplied, or whose extracted fields came back
    unreadable, is not counted as satisfying a requirement. Refused rather than
    reported as satisfied-with-a-note.
    """


class NoApprovalRefused(ToolRefused):
    """§3, Scheduling: *"may not write without valid approval"*.

    Booking write authority is not the same as permission to use it. The write
    also needs a live approval bound to the exact payload (007's consent guard,
    from this side). Projected as a policy refusal because no call happens.
    """

    def __init__(self, message: str, *, role: str, tool: str, workflow_state: str | None) -> None:
        super().__init__(message, role=role, tool=tool, workflow_state=workflow_state)


class DuplicateBookingKeyRefused(ToolRefused):
    """§3/§7, Scheduling: *"may not create a duplicate with a new key after a timeout"*.

    After an unresolved write the next call is `lookup_request` **with the same
    idempotency key**. A second key is a second appointment. Refused as a policy
    refusal: the write never leaves the process.
    """

    def __init__(self, message: str, *, role: str, tool: str, workflow_state: str | None) -> None:
        super().__init__(message, role=role, tool=tool, workflow_state=workflow_state)


class BookingOutcomeUnknown(AgentRuntimeError):
    """A booking write was attempted and its fate is unknown (§7).

    Raised when the write path fails in a way that does not establish "nothing
    happened": a 5xx, a timeout, a connection that dropped after the request left.
    The projection is ``outcome_unknown``, **not** the retryable one that an
    ordinary transport fault produces, and that difference is the whole point —
    a retryable classification invites a second attempt, and a second attempt with
    a fresh key is a second appointment.

    Deliberately *not* a subclass of `UpstreamTransient`: `is_transient` would then
    classify it as retryable and the retry owner would do the wrong thing with the
    right intentions. The next step is `lookup_request` with the same idempotency
    key, which is a decision, not a retry.
    """

    def __init__(
        self, message: str, *, idempotency_key: str, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.idempotency_key = idempotency_key
        self.status_code = status_code

    @property
    def outcome(self) -> ResultOutcome | None:
        return ResultOutcome.outcome_unknown


class ConsentNotRecorded(MissingDataError):
    """§11, handoff: the summary the patient will read was never consented to.

    A handoff shares a description of the patient's situation with a human. The
    description is the patient's to authorise, so a handoff with no recorded
    consent is refused rather than queued — and §3 is explicit that creating a
    handoff is not itself consent.
    """


class OneWayDoorRefused(AgentRuntimeError):
    """§3/§8: after the third transient failure, the normal flow never calls that
    specialist again — the move to `RECOVERY` is one-way.

    Deliberately an error rather than a flag: a caller that forgets to check a
    boolean is exactly the caller that re-calls the failed agent.

    Projects to `terminal_error` for *this turn* because a further attempt at the
    same specialist inside the same turn is what must not happen; the workflow
    itself continues in `RECOVERY`.
    """

    @property
    def outcome(self) -> ResultOutcome | None:
        return ResultOutcome.terminal_error
