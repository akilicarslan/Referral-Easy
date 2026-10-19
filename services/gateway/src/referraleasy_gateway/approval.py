"""Approval binding: consent is bound to five things, and the fifth is a hash (§9).

§9: *"Approval is bound to the patient + workflow + state_version + booking payload hash + expiry;
if the data changes, approval is invalid."* That is one sentence with five clauses, and the point of
it is the last
one — the invalidation is **structural**, not a rule someone remembers to check. If the payload
changes, the hash changes, the binding no longer matches, and the booking is refused.

The hash itself is not computed here. `referraleasy_domain.canonical_booking_payload_hash` owns the
canonical encoding, because the same hash has to be written onto `workflow.payload` for the
database's consent guard to compare against; a second implementation in the gateway would be a
second encoding, and the two would disagree on exactly the inputs nobody tests with (an explicit
null, a reordered key, a UUID). `tests/gateway/test_approval_binding.py` asserts the two agree rather
than assuming it.

What lives here is the tuple: `ApprovalBinding`, and `ApprovalService`, which grants one against the
workflow's *current* `state_version` and authorises a booking only when all five parts still agree.
A binding is single-use — §7's rule that a booking has exactly one authorising approval, which the
database enforces with `UNIQUE(approval_id)`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from referraleasy_domain import ApprovalRefusedError, canonical_booking_payload_hash
from referraleasy_domain.workflow import fetch_all, fetch_one
from referraleasy_gateway.psql import session
from referraleasy_gateway.timing import Clock

__all__ = [
    "DEFAULT_APPROVAL_TTL_SECONDS",
    "EVIDENCE_SOURCE_SYSTEM",
    "ApprovalBinding",
    "ApprovalLedger",
    "ApprovalService",
    "InMemoryApprovalLedger",
    "PostgresApprovalLedger",
    "payload_hash_of",
]

#: How long a patient's consent stands. §9 requires an expiry; the value is a demo choice, and it is
#: configuration rather than a constant of nature.
DEFAULT_APPROVAL_TTL_SECONDS = 15 * 60

#: The `source_system` the consent-capture evidence rows are written under. 006 keys evidence on
#: `(source_system, evidence_id)`, so naming the writer is what keeps one system's opaque ids from
#: colliding with another's — and this is the gateway's name for itself, not a claim about a hospital.
EVIDENCE_SOURCE_SYSTEM = "referraleasy-gateway"


def payload_hash_of(payload: Mapping[str, Any]) -> str:
    """The consent-binding hash, from the domain's one canonical encoder."""
    return canonical_booking_payload_hash(payload)


@dataclass(frozen=True, slots=True)
class ApprovalBinding:
    """One approval row: consent, and the five things it is tied to.

    `payload_hash` is a `str` rather than the payload, deliberately — the gateway never needs to
    re-derive consent from data it holds; it needs to compare a hash it recorded against a hash it
    computes now.
    """

    approval_id: UUID
    workflow_id: UUID
    patient_ref: str
    state_version: int
    payload_hash: str
    approved_at: datetime
    expires_at: datetime
    channel: str = "patient_self_service"
    consumed_at: datetime | None = None

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    def is_live(self, now: datetime) -> bool:
        """Unexpired and unused. Both are required; either alone is not consent."""
        return not self.is_consumed and now < self.expires_at

    def authorises(
        self,
        *,
        workflow_id: UUID,
        patient_ref: str,
        state_version: int,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> bool:
        """Whether this binding authorises *this* booking. All five parts, or it is not consent."""
        return (
            self.workflow_id == workflow_id
            and self.patient_ref == patient_ref
            and self.state_version == state_version
            and self.payload_hash == payload_hash_of(payload)
            and self.is_live(now)
        )


class ApprovalLedger(Protocol):
    """Where bindings are kept. The PostgreSQL profile is `approval` itself, through the domain."""

    def add(self, binding: ApprovalBinding) -> ApprovalBinding: ...

    def get(self, approval_id: UUID) -> ApprovalBinding | None: ...

    def for_workflow(self, workflow_id: UUID) -> tuple[ApprovalBinding, ...]: ...

    def consume(self, approval_id: UUID, *, at: datetime) -> ApprovalBinding: ...


class InMemoryApprovalLedger:
    """The memory profile's ledger: the same rules, no database.

    `consume` refuses a second consumption rather than overwriting: a double spend must be a refusal,
    because the alternative is the second booking §7 exists to prevent.
    """

    def __init__(self) -> None:
        self._bindings: dict[UUID, ApprovalBinding] = {}

    def add(self, binding: ApprovalBinding) -> ApprovalBinding:
        self._bindings[binding.approval_id] = binding
        return binding

    def get(self, approval_id: UUID) -> ApprovalBinding | None:
        return self._bindings.get(approval_id)

    def for_workflow(self, workflow_id: UUID) -> tuple[ApprovalBinding, ...]:
        """Every binding for a workflow, live or not. Liveness is the caller's question, not the
        ledger's: a refusal that cannot tell "expired" from "never approved" is a refusal a patient
        cannot act on."""
        return tuple(
            binding for binding in self._bindings.values() if binding.workflow_id == workflow_id
        )

    def consume(self, approval_id: UUID, *, at: datetime) -> ApprovalBinding:
        binding = self._bindings.get(approval_id)
        if binding is None:
            raise ApprovalRefusedError(f"no approval {approval_id} (RE004)")
        if binding.is_consumed:
            raise ApprovalRefusedError(
                f"approval {approval_id} was already consumed at {binding.consumed_at}; an "
                "approval authorises exactly one booking (RE004, §9)"
            )
        consumed = replace(binding, consumed_at=at)
        self._bindings[approval_id] = consumed
        return consumed


class PostgresApprovalLedger:
    """The deployment profile's ledger: `approval` itself, through the schema it is bound by.

    §9 says an approval is bound to patient + workflow + `state_version` + payload hash + expiry, and
    migration 007 makes that *structural* rather than conventional: the binding is a unique key, the
    payload hash is a CHECK'd 64-hex value, the expiry must be after the approval, and
    `approval_no_rebinding` makes the whole tuple immutable. So this class does not restate any of
    it. What it does is *write the row*, because the rules are about a row existing.

    **The window comes from the caller; the epoch comes from the database.** `ApprovalBinding`
    carries the TTL the service decided on, as `expires_at - approved_at`, and nothing else about
    time: the row is stamped by `now()` and the returned binding carries what the database wrote.
    That is the only arrangement in which this system's two clocks cannot disagree, and they can
    disagree — `approval_is_live` evaluates §9's expiry against the *database's* `now()`, while
    `ApprovalService.authorise` evaluates it against the injected `Clock`. A row expiring at
    `clock.now() + ttl` would be live to the gateway and long dead to the guard that actually decides
    whether a workflow may enter `BOOKING_IN_FLIGHT`. In production both clocks are the same wall
    clock and the distinction is invisible; in the demo profile the gateway runs a manual one, and
    this is what keeps the two in agreement there as well. Which clock wins is not a choice this
    class makes: the column's default is the database's, and the database is the store of record.

    **Consent is evidence before it is an approval.** 007 refuses an approval whose `evidence_id`
    does not name an authoritative, still-fresh `evidence` row, and 006 makes that table append-only.
    So `add` writes both, in one statement, under a data-modifying CTE: a consent record the database
    cannot trace to a source is an assertion with no provenance, and splitting this into two
    statements would leave a window where one existed without the other.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def add(self, binding: ApprovalBinding) -> ApprovalBinding:
        """Record the consent and its evidence, and return the row the database stored.

        The `evidence_id` is derived from the approval's own id rather than generated, so the two
        rows are traceable to each other in either direction without a join table. `fresh_for` and
        the expiry are both derived from the binding's TTL, so the evidence cannot be older than the
        consent it justifies: 007's guard would refuse the insert outright, which is the point.
        """
        ttl_seconds = (binding.expires_at - binding.approved_at).total_seconds()
        with session(self._dsn) as connection:
            row = fetch_one(
                connection,
                """
                WITH captured AS (
                    INSERT INTO evidence (evidence_id, kind, source_system, source_id,
                                          authoritative, observed_at, fresh_for, content_hash)
                    VALUES (%s, 'consent', %s, %s, true, now(), make_interval(secs => %s), %s)
                    RETURNING id
                )
                INSERT INTO approval (id, workflow_id, patient_ref, evidence_id, state_version,
                                      booking_payload_hash, channel, approved_by, expires_at)
                SELECT %s, %s, %s, captured.id, %s, %s, %s, %s,
                       now() + make_interval(secs => %s)
                  FROM captured
                RETURNING id, workflow_id, patient_ref, state_version, booking_payload_hash,
                          channel, approved_at, expires_at, consumed_at
                """,
                (
                    str(binding.approval_id),
                    EVIDENCE_SOURCE_SYSTEM,
                    str(binding.workflow_id),
                    ttl_seconds,
                    binding.payload_hash,
                    binding.approval_id,
                    binding.workflow_id,
                    binding.patient_ref,
                    binding.state_version,
                    binding.payload_hash,
                    binding.channel,
                    # The approver is the patient: this row is patient self-service consent, and the
                    # column exists so that a future operator-mediated channel can say so in the data
                    # rather than in the code path that wrote it.
                    binding.patient_ref,
                    ttl_seconds,
                ),
            )
        if row is None:  # pragma: no cover - an INSERT ... RETURNING always returns its row
            raise ApprovalRefusedError(f"approval {binding.approval_id} was not recorded (RE004)")
        return _binding(row)

    def get(self, approval_id: UUID) -> ApprovalBinding | None:
        with session(self._dsn) as connection:
            row = fetch_one(connection, _SELECT_APPROVAL + " WHERE id = %s", (approval_id,))
        return None if row is None else _binding(row)

    def for_workflow(self, workflow_id: UUID) -> tuple[ApprovalBinding, ...]:
        """Every binding for a workflow, live or not — the same question the memory ledger answers.

        Liveness stays the caller's question here too, for the reason the memory ledger gives: a
        refusal that cannot tell "expired" from "never approved" is a refusal a patient cannot act
        on, and `ApprovalService.authorise` is where that distinction is made. Both profiles answer
        with the columns the distinction needs, including `consumed_at`.
        """
        with session(self._dsn) as connection:
            rows = fetch_all(
                connection,
                _SELECT_APPROVAL + " WHERE workflow_id = %s ORDER BY approved_at, id",
                (workflow_id,),
            )
        return tuple(_binding(row) for row in rows)

    def consume(self, approval_id: UUID, *, at: datetime) -> ApprovalBinding:
        """Report the approval as the database holds it. **This writes nothing.**

        In this profile an approval is spent by the *booking*, not by the ledger: 008's
        `booking_request_consume_approval` runs AFTER INSERT on `booking_request` and is what sets
        `consumed_at`, in the same statement as the booking that spends it. By the time the transport
        reaches this call the consumption has already happened, in a transaction that is already
        committed, and a second write here would be a second source of truth for a fact the database
        recorded as part of writing the row. 007's `approval_no_rebinding` guard would refuse the
        update anyway — "consumption is one-way" is a trigger, not a convention.

        So the rule the memory profile enforces in this method — a double spend is a refusal — is the
        schema's here, and strictly stronger: `UNIQUE(approval_id)` makes a second booking against a
        spent approval impossible outright, and the trigger's `WHERE consumed_at IS NULL` means there
        is no window in which a booking exists against an approval that was not spent. The refusal a
        caller actually sees for a second attempt comes from `ApprovalService.authorise`, which reads
        the same rows through `for_workflow` and says which way the consent is gone. `at` is accepted
        and unused, because the timestamp that matters is the one the writing transaction stamped.

        The one thing this method still refuses is an approval that does not exist — there is no row
        for `authorise` to have found, and inventing one would be the two profiles disagreeing about
        the most basic question in the port.
        """
        del at
        binding = self.get(approval_id)
        if binding is None:
            raise ApprovalRefusedError(f"no approval {approval_id} (RE004)")
        return binding


#: The columns every read here needs, in one place so `get` and `for_workflow` cannot drift into
#: returning different shapes of the same row.
_SELECT_APPROVAL = (
    "SELECT id, workflow_id, patient_ref, state_version, booking_payload_hash, channel, "
    "approved_at, expires_at, consumed_at FROM approval"
)


def _binding(row: Mapping[str, Any]) -> ApprovalBinding:
    """A database row as the port's binding. The column names are 007's, kept verbatim."""
    return ApprovalBinding(
        approval_id=row["id"],
        workflow_id=row["workflow_id"],
        patient_ref=row["patient_ref"],
        state_version=row["state_version"],
        payload_hash=row["booking_payload_hash"],
        approved_at=row["approved_at"],
        expires_at=row["expires_at"],
        channel=row["channel"],
        consumed_at=row["consumed_at"],
    )


class ApprovalService:
    """Granting and spending consent. The two operations §9 names, in one place."""

    def __init__(
        self,
        ledger: ApprovalLedger,
        *,
        clock: Clock,
        ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._ttl = timedelta(seconds=ttl_seconds)

    def bind(
        self,
        *,
        workflow_id: UUID,
        patient_ref: str,
        state_version: int,
        payload: Mapping[str, Any],
    ) -> ApprovalBinding:
        """Record the patient's consent against the workflow version it was given at.

        `state_version` is passed rather than looked up so the caller has to name the version it is
        consenting at, and the database's consent guard compares it against `OLD.state_version` when
        the workflow enters `BOOKING_IN_FLIGHT` — so consent given at an older version simply does
        not open the door, with no code here deciding that.
        """
        now = self._clock.now()
        binding = ApprovalBinding(
            approval_id=uuid4(),
            workflow_id=workflow_id,
            patient_ref=patient_ref,
            state_version=state_version,
            payload_hash=payload_hash_of(payload),
            approved_at=now,
            expires_at=now + self._ttl,
        )
        return self._ledger.add(binding)

    def authorise(
        self,
        *,
        workflow_id: UUID,
        patient_ref: str,
        state_version: int,
        payload: Mapping[str, Any],
    ) -> ApprovalBinding:
        """The binding that authorises this booking, or a refusal naming what did not match.

        Refusals are specific because the four ways consent can fail are four different situations
        for the patient: they never approved, they approved something else, the workflow moved on
        (they were asked again), or they took too long.

        `state_version` is the version the consent must be **bound to**, which is not always the
        version the workflow is at when the question is asked: consent is recorded in the state the
        patient is asked in, and the workflow then moves on. The binding is the authority on which
        version that is (migration 014's guard compares against `OLD.state_version`), so a caller
        that is one transition past consent passes that earlier version and one that is asking
        *now* passes the current one.
        """
        now = self._clock.now()
        mine = [
            binding
            for binding in self._ledger.for_workflow(workflow_id)
            if binding.patient_ref == patient_ref
        ]
        if not mine:
            raise ApprovalRefusedError(
                f"no approval exists for workflow {workflow_id}; asking for consent is not consent "
                "(RE004, §9)"
            )

        candidates = [binding for binding in mine if binding.is_live(now)]
        if not candidates:
            # Say *which* way the consent is gone, because "you took too long" and "you already
            # approved" are different things to a person, and §9 makes them different rules.
            consumed = [binding for binding in mine if binding.is_consumed]
            if consumed:
                raise ApprovalRefusedError(
                    f"approval {consumed[0].approval_id} was already consumed; one approval "
                    "authorises exactly one booking (RE004, §9)"
                )
            raise ApprovalRefusedError(
                f"the approval for workflow {workflow_id} expired at "
                f"{mine[0].expires_at.isoformat()} (RE004, §9)"
            )

        for binding in candidates:
            if binding.state_version != state_version:
                raise ApprovalRefusedError(
                    f"approval {binding.approval_id} was given at state_version "
                    f"{binding.state_version}, but this booking is at {state_version}; the workflow "
                    "moved on after the patient answered (RE004, §9)"
                )
            if not binding.authorises(
                workflow_id=workflow_id,
                patient_ref=patient_ref,
                state_version=state_version,
                payload=payload,
                now=now,
            ):
                raise ApprovalRefusedError(
                    f"approval {binding.approval_id} is bound to payload hash "
                    f"{binding.payload_hash[:12]}…, but the booking payload now hashes to "
                    f"{payload_hash_of(payload)[:12]}…; changing the payload invalidates consent "
                    "(RE004, §9)"
                )
            return binding

        raise ApprovalRefusedError(  # pragma: no cover - the loop returns or raises per candidate
            f"no live approval for workflow {workflow_id} matches this booking (RE004, §9)"
        )

    def consume(self, approval_id: UUID) -> ApprovalBinding:
        """Spend a binding. Called once, at the moment the booking row is written."""
        return self._ledger.consume(approval_id, at=self._clock.now())
