"""The human side: consent before sharing, one holder per ticket, and a resolution that books nothing.

§11 asks for five things, and each of them is a refusal somewhere — which is what makes them testable
as refusals rather than as promises:

* **the patient sees the summary before it is shared.** Consent gates the *row*, not a flag: a handoff
  with no consent (or with an empty summary) is refused with `ConsentRequiredError` and no `handoff`
  row is written, because the row *is* the share.
* **one holder per ticket.** `handoff_claim` is a compare-and-set on `claim_generation`, so two
  operators cannot both hold one ticket — and holding it is what sets `workflow.human_owner_id`, which
  suspends automation.
* **a resolution is not booking consent.** The booking states are absent from the function's
  allow-list *and* from the state machine, the refusal comes before the database is asked, and the
  outcome object says `booking_consent_granted=False` because a caller must not infer it.
* **a resolution is audited, and it is not the operator editing history.** The entry is written by the
  trigger, carries the from/to states and the resolution, and names the operator who did it. Two
  things about *where* it names them are wrong and are pinned rather than papered over: the closing of
  a ticket is filed under `actor_kind='automation'` (the function updates the handoff before it
  switches the transaction's actor), and the operator's id reaches the audit row as a leftover of the
  earlier claim, because `handoff_claim` restores the actor's *kind* and not its *id*. Both are
  migration-level defects, both are outside this phase's write scope, and both are asserted in one
  place with the reason they are asserted that way.
* **nobody is told a live operator was reached.** There is no such channel; the ticket is pending, the
  directory gives a route, and :attr:`OperatorResponse.live_operator_reached` is `False` by
  construction.

Two structural facts about the schema show up here as tests rather than as comments, because both are
places where a reader would otherwise guess: `handoff` has no columns for the patient-visible summary,
the consent timestamp, the reason detail, the sources, the operator's alternatives or their notes — so
the first four live in `workflow.payload["handoff"]` and the last two are flushed into
`handoff.resolution_note` at resolution. And the two allow-lists in play are *different lists*: the
function's (which admits `COVERAGE_CHECK`) and the state machine's (which has no edge there from
`HUMAN_HANDOFF`), so the gap between them is a real refusal from `workflow_guard` and not a
hypothetical.

The queue is the real one, over two real connections where a race is the subject; nothing here is
doubled — this module needs no facility, no clock and no sender.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from worker_doubles import (
    OPERATOR,
    Seeder,
    expect_database_refusal,
    expect_refusal,
    queue_and_claim_ticket,
    workflow_payload,
    workflow_row,
)

from referraleasy_contracts.generated.common import Generation, HandoffReasonClass
from referraleasy_contracts.generated.patient import AlternativeOption, AlternativeOptionKind
from referraleasy_contracts.generated.workflow import HandoffStatus, TicketStatus
from referraleasy_domain import Workflow
from referraleasy_domain.errors import (
    ApprovalRefusedError,
    DomainError,
    HumanOwnedError,
    StaleOwnershipError,
)
from referraleasy_workers.checkpoint import (
    CHECKPOINT_KEY,
    HANDOFF_KEY,
    make_checkpoint,
    read_checkpoint,
    write_payload,
)
from referraleasy_workers.errors import ConsentRequiredError, WorkerError
from referraleasy_workers.hitl import (
    BOOKING_CONSENT_STATES,
    OPERATOR_RETURN_STATES,
    DirectoryContact,
    HandoffRecord,
    HandoffRequest,
    HumanQueue,
    StaticDirectory,
    default_ticket_ref,
    queued_handoffs,
)
from referraleasy_workers.ownership import OwnedWorkflow

#: The other operator, and the reason two are needed at all: "held" must mean held by *one* person,
#: and a test that only ever named one operator could not tell that from "held by anybody".
OTHER_OPERATOR = "op-2"
#: The worker id the environment fixture names, so the automation side of the suspension is the same
#: worker the lease belongs to.
WORKER = "w-1"

SUMMARY = (
    "Here is what we would share with a member of our team:\n- a referral, and where it has got to"
)
SOURCE_LINE = "lookup #1 definitively_absent_for_same_key (200)"


def new_ticket_ref() -> str:
    """A fresh referral-ticket reference, because two workflows in one test must not share one.

    `workflow_active_ticket_key` is a unique index on `workflow.ticket_ref`, and its predicate reads
    `WHERE state NOT IN ('resolved', 'failed', 'cancelled')` — lowercase names the state vocabulary
    never uses, so the "terminal workflows release their ticket reference" the migration's comment
    promises does not happen and the index is global. A fixed reference like the seed double's
    `T-1001` therefore makes a second workflow in one test impossible, and would make a fresh referral
    for a re-opened ticket impossible in production. Per-test references also mean these tests cannot
    collide with another suite truncating the same database mid-run.
    """
    return f"T-{uuid4().hex[:10].upper()}"


def handoff_count(conn: Any) -> int:
    """Every `handoff` row, whatever its state. A count, because "no row" is the claim."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM handoff")
        return int(cur.fetchone()["n"])


def audit_entry(conn: Any, action: str, entity_id: str, *, to_state: str | None = None) -> Any:
    """The most recent audit row for an action on an entity — the audit of record, read straight.

    `to_state` narrows to one transition, because a workflow accumulates a `workflow.transition` entry
    for every state it passes through and the test wants the one it caused.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM audit
             WHERE action = %s
               AND entity_id = %s
               AND (%s::text IS NULL OR detail ->> 'to_state' = %s)
             ORDER BY seq DESC
             LIMIT 1
            """,
            (action, entity_id, to_state, to_state),
        )
        row = cur.fetchone()
    assert row is not None, f"no audit entry {action!r} for {entity_id} (to_state={to_state!r})"
    return row


def stage(
    conn: Any,
    *,
    reason_class: HandoffReasonClass = HandoffReasonClass.no_match,
    summary: str = SUMMARY,
    consent: bool = True,
    deadline: datetime | None = None,
    state: str = "HUMAN_HANDOFF",
) -> tuple[OwnedWorkflow, HandoffRequest]:
    """The pipeline's own two writes, in its order: stage the summary, then move the workflow.

    Staging happens while automation still owns the workflow (`write_payload`), and the move happens
    after — so a test that wants to see what a patient reviewed reads the same key the worker wrote.
    """
    workflow_id = Seeder(conn).workflow(ticket_ref=new_ticket_ref())
    request = HandoffRequest(
        workflow_id=workflow_id,
        reason_class=reason_class,
        patient_visible_summary=summary,
        reason_detail="staged by the test",
        sources=(SOURCE_LINE,),
        consent_given_at=datetime.now(UTC) if consent else None,
        response_deadline_at=deadline,
        ticket_ref=default_ticket_ref(),
    )
    owned = OwnedWorkflow.claim(conn, workflow_id, WORKER, expected_generation=0)
    write_payload(
        conn, workflow=owned.row, worker_id=WORKER, key=HANDOFF_KEY, value=request.payload()
    )
    owned.advance(state)
    return owned, request


def stage_and_queue(conn: Any, **kwargs: Any) -> tuple[HandoffRecord, HandoffRequest]:
    """The same, plus the queue write — the whole of §11's "there is now a ticket"."""
    _owned, request = stage(conn, **kwargs)
    return HumanQueue(conn).queue(request), request


# ---------------------------------------------------------------------------
# Consent gates the row
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_queue_is_never_reached_without_the_patients_consent(
    db: Any, refusals: Callable[[type[BaseException]], Any]
) -> None:
    """§11's gate is a missing row, not a false flag: the row is the share.

    The staging the patient will be shown is already written (that is what they consent *to*), and the
    queue refuses to write the ticket. Every other shape of this — an `open` row with `queued=false`
    beside it — would make "no consent, no share" a convention some other query could forget.
    """
    _owned, request = stage(db, consent=False)

    assert request.has_consent is False
    with refusals(ConsentRequiredError):
        HumanQueue(db).queue(request)

    assert handoff_count(db) == 0
    assert queued_handoffs(db) == []
    staged = workflow_payload(db, request.workflow_id)[HANDOFF_KEY]
    assert staged["patient_visible_summary"] == SUMMARY
    assert staged["consent_given_at"] is None


@pytest.mark.integration
def test_consent_to_an_empty_summary_is_not_consent(
    db: Any, refusals: Callable[[type[BaseException]], Any]
) -> None:
    """A timestamp is not the thing being consented to, so the text is checked as well as the time."""
    _owned, request = stage(db, summary="   ")

    assert request.has_consent is True, "the gate is not only the timestamp"
    with refusals(ConsentRequiredError):
        HumanQueue(db).queue(request)
    assert handoff_count(db) == 0


@pytest.mark.integration
def test_a_queued_ticket_carries_its_reason_class_verbatim_and_the_staged_evidence(db: Any) -> None:
    """What a human opens is the class, the summary the patient approved, and the lines behind it.

    The reason class travels in `handoff.reason` as the *class* — one vocabulary end to end, so a
    console's filter and §11's trigger list name the same thing — and everything the schema has no
    column for arrives at `view()` from the staging the worker wrote.
    """
    record, _request = stage_and_queue(db, reason_class=HandoffReasonClass.conflicting_sources)

    assert record.reason == HandoffReasonClass.conflicting_sources.value
    assert record.reason_class is HandoffReasonClass.conflicting_sources
    assert record.state == "open"
    assert record.status is HandoffStatus.queued
    assert record.is_live is True
    assert record.holds_human_ownership is False, "an open ticket suspends nothing"

    view = HumanQueue(db).view(record.id, operator_id=OPERATOR)
    assert view.addable is False, "nobody holds it yet"
    assert view.addable_by(OPERATOR) is False
    assert view.patient_visible_summary == SUMMARY
    assert view.consent_given_at is not None
    assert view.sources == (SOURCE_LINE,)
    assert view.ticket.final_approval_still_required is True

    lines = view.source_lines()
    assert f"summary shown to the patient: {SUMMARY}" in lines
    assert any(line.startswith("consent recorded at: ") for line in lines)
    assert SOURCE_LINE in lines


# ---------------------------------------------------------------------------
# One holder per ticket, and what holding means
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_two_operators_cannot_both_hold_one_ticket(db: Any, db2: Any) -> None:
    """The compare-and-set, refused twice for two reasons: a stale generation, then a live claim.

    The second refusal is the one that matters operationally — an operator who re-reads the ticket and
    tries again at the *current* generation must still lose to the colleague who is actually working
    it. Holding the ticket is also what sets `human_owner_id` on the workflow and clears the worker's
    lease, so the assertion about the workflow is part of the same fact.
    """
    record, request = stage_and_queue(db)
    queue = HumanQueue(db)
    first = queue.claim(record.id, operator_id=OPERATOR)
    db.commit()

    assert first.claimed_by == OPERATOR
    assert first.claim_generation == 1
    held = workflow_row(db, request.workflow_id)
    assert held["human_owner_id"] == OPERATOR
    assert held["owner_worker_id"] is None, "a human owner and a worker lease are not both"

    other = HumanQueue(db2)
    with expect_refusal(db2, StaleOwnershipError):
        other.claim(record.id, operator_id=OTHER_OPERATOR, expected_generation=0)
    with expect_refusal(db2, StaleOwnershipError):
        other.claim(
            record.id, operator_id=OTHER_OPERATOR, expected_generation=first.claim_generation
        )

    assert other.view(record.id).record.claimed_by == OPERATOR
    assert workflow_row(db2, request.workflow_id)["human_owner_id"] == OPERATOR


@pytest.mark.integration
def test_only_the_holder_can_annotate_a_ticket(
    db: Any, refusals: Callable[[type[BaseException]], Any]
) -> None:
    """Alternatives and notes belong to the person holding the ticket, and to nobody else.

    The non-holder's writes are refused (`RE001`) and leave nothing behind; the holder's accumulate in
    a draft that only reaches the durable record at resolution, because the workflow payload is
    unwritable while a human owns the workflow — the same suspension, seen from the operator's side.
    """
    record, _request = stage_and_queue(db)
    queue = HumanQueue(db)
    queue.claim(record.id, operator_id=OPERATOR)
    option = AlternativeOption(
        option_id=uuid4(),
        kind=AlternativeOptionKind.different_day,
        description="any Tuesday afternoon",
        # Empty on purpose: the contract's own description says an option with no evidence must not be
        # offered, but the field has no minimum — so refusing it is the patient API's rule, and this
        # package's job is to carry the operator's object through to the durable record unchanged.
        evidence_ids=[],
    )

    with refusals(StaleOwnershipError):
        queue.add_alternatives(record.id, operator_id=OTHER_OPERATOR, options=[option])
    with refusals(StaleOwnershipError):
        queue.add_notes(record.id, operator_id=OTHER_OPERATOR, notes="not mine to write")
    assert queue.view(record.id).ticket.added_alternatives is None

    draft = queue.add_alternatives(record.id, operator_id=OPERATOR, options=[option])
    assert draft.alternatives == (option,)
    queue.add_notes(record.id, operator_id=OPERATOR, notes="rang the family back")

    view = queue.view(record.id, operator_id=OPERATOR)
    assert view.addable is True
    assert view.addable_by(OPERATOR) is True
    assert view.addable_by(OTHER_OPERATOR) is False
    assert view.ticket.added_alternatives == [option]
    assert view.ticket.operator_notes == "rang the family back"
    assert view.draft_note is not None
    assert "any Tuesday afternoon" in view.draft_note
    assert view.record.resolution_note is None, "the draft is not the record until resolution"


@pytest.mark.integration
def test_a_held_ticket_suspends_automation_and_releasing_it_does_not(
    db: Any, refusals: Callable[[type[BaseException]], Any]
) -> None:
    """`RE003` while a human holds the workflow, `RE001` after they let go — two different answers.

    The distinction is the point: `RE003` means "a person owns this, stop and wait", and the worker's
    response is to leave the workflow alone. Once the ticket is released the same write is refused as a
    *lost claim*, which is the ordinary case a worker answers by re-reading the row and claiming the
    current generation — and once it does, the write lands. A system that reported both as "someone
    else has it" would make an operator's presence indistinguishable from a colleague's lease.
    """
    workflow_id = Seeder(db).workflow(ticket_ref=new_ticket_ref())
    record = queue_and_claim_ticket(db, workflow_id)
    assert record.claimed_by == OPERATOR

    stale_view = Workflow.from_row(workflow_row(db, workflow_id))
    with refusals(HumanOwnedError):
        write_payload(
            db, workflow=stale_view, worker_id=WORKER, key=CHECKPOINT_KEY, value={"by": WORKER}
        )

    queue = HumanQueue(db)
    released = queue.release(record.id, operator_id=OPERATOR)
    assert released.state == "open"
    assert released.claimed_by is None
    assert workflow_row(db, workflow_id)["human_owner_id"] is None

    with refusals(StaleOwnershipError):
        write_payload(
            db, workflow=stale_view, worker_id=WORKER, key=CHECKPOINT_KEY, value={"by": WORKER}
        )

    current = workflow_row(db, workflow_id)
    reclaimed = OwnedWorkflow.claim(
        db, workflow_id, WORKER, expected_generation=current["generation"]
    )
    reclaimed.write_checkpoint(
        make_checkpoint(reclaimed.row, step="send_booking", data={"by": WORKER})
    )

    stored = read_checkpoint(Workflow.from_row(workflow_row(db, workflow_id)))
    assert stored.verified is True
    assert stored.checkpoint is not None
    assert stored.checkpoint.data == {"by": WORKER}


# ---------------------------------------------------------------------------
# A resolution books nothing
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("return_state", sorted(BOOKING_CONSENT_STATES))
def test_a_resolution_cannot_move_a_workflow_into_a_booking_state(
    db: Any, refusals: Callable[[type[BaseException]], Any], return_state: str
) -> None:
    """The four states a booking could follow from are refused *before* the database is asked.

    The caller is a non-holder on purpose: the refusal has to come from the allow-list rather than from
    "you do not hold this ticket", or the rule would only bind people who had already passed a check.
    The ticket is left claimed and the workflow is left where it was, so a refused resolution is not
    half a resolution either.
    """
    record, request = stage_and_queue(db)
    queue = HumanQueue(db)
    queue.claim(record.id, operator_id=OPERATOR)

    with refusals(ApprovalRefusedError):
        queue.resolve(
            record.id,
            operator_id=OTHER_OPERATOR,
            resolution="looks booked to me",
            return_state=return_state,
        )

    held = queue.view(record.id).record
    assert held.state == "claimed"
    assert held.claimed_by == OPERATOR
    assert held.resolution is None
    assert held.claim_generation == 1
    assert workflow_row(db, request.workflow_id)["state"] == "HUMAN_HANDOFF"


@pytest.mark.integration
@pytest.mark.parametrize(
    "return_state", ["AWAITING_HOSPITAL_CHOICE", "AWAITING_DOCTOR_CHOICE", "BOOKED_ISH", ""]
)
def test_a_resolution_is_bounded_by_the_allow_list_and_not_by_taste(
    db: Any, refusals: Callable[[type[BaseException]], Any], return_state: str
) -> None:
    """States that are not booking states are still refused when they are not return states.

    `AWAITING_HOSPITAL_CHOICE` is the interesting one: it is nowhere near a booking, and it is still
    absent from the allow-list, because a ticket is resolved *back into* the flow's own shape rather
    than into whatever state looks adjacent. The list is the contract, and it lives in one place per
    side — this Python copy exists so the refusal names the rule before a statement runs.
    """
    record, _request = stage_and_queue(db)
    queue = HumanQueue(db)
    queue.claim(record.id, operator_id=OPERATOR)

    assert return_state not in OPERATOR_RETURN_STATES
    with refusals(ApprovalRefusedError):
        queue.resolve(record.id, operator_id=OPERATOR, resolution="done", return_state=return_state)
    assert queue.view(record.id).record.state == "claimed"


@pytest.mark.integration
def test_the_functions_allow_list_is_not_the_state_machine(db: Any) -> None:
    """Two lists, and the gap between them is a real refusal from `workflow_guard` (`RE002`).

    `handoff_resolve` admits `COVERAGE_CHECK`; `workflow_transition` has no `HUMAN_HANDOFF ->
    COVERAGE_CHECK` edge. A resolve naming it therefore passes the function's own check and is stopped
    by the transition table — and the ticket is not half-resolved, because the statement rolls back
    whole. The test is written against the *shipped* function rather than against this package, so it
    fails if either list moves.
    """
    record, request = stage_and_queue(db)
    queue = HumanQueue(db)
    claimed = queue.claim(record.id, operator_id=OPERATOR)

    assert "COVERAGE_CHECK" in OPERATOR_RETURN_STATES, "the function's list admits it"
    with expect_database_refusal(db, "RE002"), db.cursor() as cur:
        cur.execute(
            "SELECT handoff_resolve(%s, %s, %s, %s, %s)",
            (claimed.id, OPERATOR, claimed.claim_generation, "done elsewhere", "COVERAGE_CHECK"),
        )

    assert queue.view(record.id).record.state == "claimed"
    assert workflow_row(db, request.workflow_id)["state"] == "HUMAN_HANDOFF"


@pytest.mark.integration
def test_resolving_a_ticket_is_not_booking_consent(db: Any) -> None:
    """The resolution closes the ticket, returns the workflow, and grants nothing.

    Four things are asserted together because they are one fact: the workflow is back in the flow with
    no human owner, no `booking_request` row exists or could exist from here, the outcome object says
    the patient's own approval is still required, and the audit entry carries the from/to states, the
    resolution and the operator. The operator's draft — alternatives and notes — reaches the durable
    record at exactly this moment, which is why it is checked here rather than earlier.
    """
    record, request = stage_and_queue(db, reason_class=HandoffReasonClass.missing_document)
    queue = HumanQueue(db)
    queue.claim(record.id, operator_id=OPERATOR)
    option = AlternativeOption(
        option_id=uuid4(),
        kind=AlternativeOptionKind.different_provider,
        description="the clinic across town",
        evidence_ids=[],
    )
    queue.add_alternatives(record.id, operator_id=OPERATOR, options=[option])
    queue.add_notes(record.id, operator_id=OPERATOR, notes="document chased by phone")

    outcome = queue.resolve(
        record.id,
        operator_id=OPERATOR,
        resolution="returned to matching",
        return_state="PROVIDER_MATCHING",
        note="operator note",
    )

    assert outcome.final_approval_still_required is True
    assert outcome.booking_consent_granted is False
    assert outcome.return_state == "PROVIDER_MATCHING"

    row = workflow_row(db, request.workflow_id)
    assert row["state"] == "PROVIDER_MATCHING"
    assert row["human_owner_id"] is None
    assert row["human_claimed_at"] is None

    view = queue.view(record.id)
    assert view.record.state == "resolved"
    assert view.record.status is HandoffStatus.resolved
    assert view.record.resolution == "returned to matching"
    assert view.record.resolved_at is not None
    assert view.record.resolution_note is not None
    assert "operator note" in view.record.resolution_note
    assert "document chased by phone" in view.record.resolution_note
    assert "the clinic across town" in view.record.resolution_note
    assert view.draft_note is None, "the draft was flushed, not kept in the process"

    entry = audit_entry(db, "handoff.resolved", str(record.id))
    assert entry["entity_type"] == "handoff"
    assert entry["outcome"] == "allow"
    assert str(entry["workflow_id"]) == str(request.workflow_id)
    assert entry["detail"]["from_state"] == "claimed"
    assert entry["detail"]["to_state"] == "resolved"
    assert entry["detail"]["resolution"] == "returned to matching"
    assert entry["detail"]["claimed_by"] == OPERATOR

    # Who did it is recorded, and *how* it is recorded is wrong — pinned rather than papered over, in
    # both of its halves, because the fix belongs in the migration rather than in this package:
    #
    # 1. `handoff_resolve` writes the handoff UPDATE before it switches the transaction's actor context
    #    to the operator, and the trigger that audits the resolution (`audit_handoff_change_trigger`,
    #    AFTER UPDATE) fires during that statement. So the closing of a ticket is filed under the
    #    `automation` actor — and an audit reader filtering `actor_kind = 'human'` sees every workflow
    #    move a person caused and not one ticket resolution. The workflow's own entry, below, is filed
    #    correctly, which is the proof that the switch happens too late rather than not at all.
    # 2. `handoff_claim` (like `release` and `resolve`) saves `current_actor_kind()`, sets the kind and
    #    the id to the operator, and restores only the kind. `set_config(..., true)` is transaction-
    #    local, so the operator's id survives to the end of the transaction — every later audit row
    #    written in it pairs an operator's id with somebody else's kind. Here the operator's own ticket
    #    is the beneficiary of a bug that, in a transaction holding two operators' work, would file one
    #    operator's act under another's.
    assert entry["actor_kind"] == "automation"
    assert entry["actor_id"] == OPERATOR, (
        "the id leaked out of the earlier claim in this transaction"
    )
    moved = audit_entry(
        db, "workflow.transition", str(request.workflow_id), to_state="PROVIDER_MATCHING"
    )
    assert moved["actor_kind"] == "human", (
        "the same transaction's workflow move *is* the operator's"
    )
    assert moved["actor_id"] == OPERATOR
    assert moved["detail"]["from_state"] == "HUMAN_HANDOFF"

    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM booking_request WHERE workflow_id = %s",
            (request.workflow_id,),
        )
        assert int(cur.fetchone()["n"]) == 0


@pytest.mark.integration
def test_one_live_handoff_per_workflow(db: Any) -> None:
    """A second escalation while one ticket is live is refused by the index, not by a check.

    The database's answer to "we already escalated this" is a duplicate-key error, and the only way to
    tell it from any other database error is the constraint name in the message: the domain's
    translation layer maps the SQLSTATEs it has a crate-wide meaning for and re-raises the rest as a
    plain `DomainError`, so this refusal is deliberately *not* asserted as a typed one. After the first
    ticket is resolved the constraint no longer applies — history is kept, and a second escalation is a
    new row rather than an edit of the old one.
    """
    record, request = stage_and_queue(db)
    again = HandoffRequest(
        workflow_id=request.workflow_id,
        reason_class=HandoffReasonClass.technical_outage,
        patient_visible_summary=SUMMARY,
        reason_detail="the same referral, a second time",
        sources=(SOURCE_LINE,),
        consent_given_at=datetime.now(UTC),
        ticket_ref=default_ticket_ref(),
    )

    # `expect_refusal` plus a `match`: the savepoint has to be here either way, and the constraint name
    # is the part that says *which* refusal this was.
    with pytest.raises(DomainError, match="handoff_one_live_per_workflow"), db.transaction():
        HumanQueue(db).queue(again)
    assert handoff_count(db) == 1

    queue = HumanQueue(db)
    queue.claim(record.id, operator_id=OPERATOR)
    queue.resolve(record.id, operator_id=OPERATOR, resolution="done", return_state="INTAKE")
    assert queued_handoffs(db) == []

    reopened = queue.queue(again)
    assert reopened.id != record.id
    assert reopened.state == "open"
    assert reopened.reason_class is HandoffReasonClass.technical_outage
    assert [ticket.id for ticket in queued_handoffs(db)] == [reopened.id]


@pytest.mark.integration
def test_the_queue_lists_live_tickets_oldest_first(db: Any) -> None:
    """What an operator's queue shows: work that is still open or claimed, in the order it arrived.

    A claimed ticket is still in the queue — it is being worked, not finished — and a resolved one
    leaves it. Oldest first is not decoration: an unattended queue is a queue nobody can triage.
    """
    newer, _newer_request = stage_and_queue(db)
    older, _older_request = stage_and_queue(db)
    db.execute(
        "UPDATE handoff SET created_at = now() - interval '1 hour' WHERE id = %s", (older.id,)
    )

    assert [ticket.id for ticket in queued_handoffs(db)] == [older.id, newer.id]

    queue = HumanQueue(db)
    queue.claim(newer.id, operator_id=OPERATOR)
    assert [ticket.id for ticket in queued_handoffs(db)] == [older.id, newer.id]

    queue.resolve(newer.id, operator_id=OPERATOR, resolution="done", return_state="INTAKE")
    assert [ticket.id for ticket in queued_handoffs(db)] == [older.id]


@pytest.mark.integration
def test_a_ticket_only_expires_on_a_deadline_that_has_arrived(
    db: Any, refusals: Callable[[type[BaseException]], Any]
) -> None:
    """Expiry is a decision with two preconditions, and the caller owns the clock.

    The schema has no `expired` state and no deadline column, so the deadline is the one staged in the
    payload and the moment is the caller's — which makes "has it expired?" checkable rather than
    implied. A ticket with nothing recorded is refused (expiry is not justified), a ticket before its
    deadline is refused, and only then does it close — as a cancellation, which is the closest thing
    the ladder has to "nobody came".
    """
    undated, _undated_request = stage_and_queue(db)
    queue = HumanQueue(db)
    with pytest.raises(WorkerError, match="no response deadline"):
        queue.expire(undated.id)

    deadline = datetime.now(UTC) + timedelta(hours=24)
    dated, _dated_request = stage_and_queue(db, deadline=deadline)
    with pytest.raises(WorkerError, match="cannot expire"):
        queue.expire(dated.id, now=deadline - timedelta(minutes=1))

    closed = queue.expire(dated.id, now=deadline + timedelta(seconds=1))
    assert closed.state == "cancelled"
    assert closed.status is HandoffStatus.withdrawn
    assert closed.resolution is not None and "expired" in closed.resolution
    assert [ticket.id for ticket in queued_handoffs(db)] == [undated.id]

    with refusals(StaleOwnershipError):
        queue.expire(dated.id, now=deadline + timedelta(hours=1))


# ---------------------------------------------------------------------------
# Nobody is pretending
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_nobody_is_told_a_live_operator_was_reached(db: Any) -> None:
    """There is no live-agent channel, so the honest answer is a pending ticket and a route.

    The message is asserted verbatim because it is patient-facing text: it says a ticket exists and
    that nobody has picked it up yet. A directory contact is added when there is one — and the flag
    stays `False` either way, so no caller can turn "we will contact you" into "someone is with you".
    """
    record, _request = stage_and_queue(db, reason_class=HandoffReasonClass.technical_outage)

    without_directory = HumanQueue(db).request_operator(record.id)
    assert without_directory.live_operator_reached is False
    assert without_directory.is_pending is True
    assert without_directory.ticket.status is TicketStatus.pending
    assert without_directory.ticket.final_approval_still_required is True
    assert without_directory.directory_contact is None
    assert without_directory.patient_message == (
        "We have opened a ticket for a member of our team to look at this referral. "
        "Nobody has picked it up yet, and we will contact you as soon as somebody has."
    )

    contact = DirectoryContact(name="Referral desk", channel="phone", detail="01234 567890")
    directory = StaticDirectory({HandoffReasonClass.technical_outage: contact})
    with_contact = HumanQueue(db, directory=directory).request_operator(record.id)

    assert with_contact.live_operator_reached is False
    assert with_contact.directory_contact == contact
    assert with_contact.patient_message == (
        "We have opened a ticket for a member of our team to look at this referral, and nobody "
        "has picked it up yet. You can also reach "
        f"{contact.name} on {contact.channel} ({contact.detail}) about this referral."
    )
    assert "is with you" not in with_contact.patient_message
    assert contact.line() == "Referral desk — phone (01234 567890)"


@pytest.mark.integration
def test_a_pending_ticket_reports_the_state_it_is_in(db: Any) -> None:
    """The ticket a patient is handed and the row an operator works are the same ticket.

    Ticket status is derived from the handoff's own state in one place (`_TICKET_STATUS_BY_STATE`), so
    a patient told "pending" and a console showing "claimed" cannot disagree. This test pins every
    stage of the ticket's life that does not involve a booking: open, claimed, resolved.
    """
    record, _request = stage_and_queue(db)
    queue = HumanQueue(db)

    assert queue.request_operator(record.id).ticket.status is TicketStatus.pending
    queue.claim(record.id, operator_id=OPERATOR)
    claimed = queue.request_operator(record.id)
    assert claimed.ticket.status is TicketStatus.claimed
    assert claimed.ticket.claimed_by == OPERATOR
    assert claimed.ticket.claim_generation == Generation(root=1), (
        "the queue's own counter, on the row"
    )
    assert claimed.ticket.claimed_at is not None

    queue.resolve(record.id, operator_id=OPERATOR, resolution="done", return_state="INTAKE")
    resolved = queue.request_operator(record.id)
    assert resolved.ticket.status is TicketStatus.resolved
    assert resolved.live_operator_reached is False, "the honesty invariant holds at every stage"


@pytest.mark.integration
def test_a_handoff_row_is_the_only_thing_the_queue_writes(db: Any) -> None:
    """What `queue` did and did not write: a row, and nothing about the workflow.

    The staging is the worker's write (it needs the lease) and the move is the worker's move, so a
    queue that also touched the workflow would be a second writer into the same guarded row with no
    ownership tokens to present. Asserted structurally, because "it happens to work" is not the claim.
    """
    _owned, request = stage(db)
    before = workflow_row(db, request.workflow_id)

    HumanQueue(db).queue(request)

    after = workflow_row(db, request.workflow_id)
    assert after == before
    assert handoff_count(db) == 1
    assert after["state"] == "HUMAN_HANDOFF", "the caller moved the workflow, not the queue"


# ---------------------------------------------------------------------------
# §15's two HITL measurements
# ---------------------------------------------------------------------------
#
# The emissions themselves fire inside `HumanQueue.resolve`, which needs a database. What is tested
# here without one is the arithmetic they are built on — and the arithmetic is where the interesting
# decision lives: a timestamp that is missing means the event did not happen, and reporting it as
# zero would put "resolved instantly" into a distribution that describes how long people took.


class TestHandoffTimings:
    def test_the_two_intervals_are_measured_from_different_instants(self) -> None:
        """Wait is queue-to-close; resolve is claim-to-close. §15 asks for both, and averaging them
        would produce a number that describes neither the patient's wait nor the operator's work."""
        from datetime import UTC, datetime, timedelta

        from referraleasy_workers.hitl import _elapsed_ms

        queued = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
        claimed = queued + timedelta(minutes=5)
        closed = queued + timedelta(minutes=20)

        assert _elapsed_ms(queued, closed) == 20 * 60 * 1000
        assert _elapsed_ms(claimed, closed) == 15 * 60 * 1000

    def test_a_missing_instant_is_none_and_never_zero(self) -> None:
        """A ticket that was never claimed has no pickup time. Zero would read as an operator who
        answered in no time at all, which is the opposite of what an unclaimed ticket means."""
        from datetime import UTC, datetime

        from referraleasy_workers.hitl import _elapsed_ms

        now = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)

        assert _elapsed_ms(None, now) is None
        assert _elapsed_ms(now, None) is None
        assert _elapsed_ms(None, None) is None
        # And a genuine zero is still zero: an instant resolution is a measurement.
        assert _elapsed_ms(now, now) == 0.0

    def test_an_unclaimed_ticket_emits_only_the_wait(self) -> None:
        """`_emit_timings` called for real, with no connection.

        The method is private and takes its two records directly; the only collaborator it touches is
        the emitter, so passing `None` for the connection drives the *actual* rule rather than a copy
        of it. A test that re-implemented the branch would pass while the branch was gone, which is
        the failure mode this file has already been written to avoid once.
        """
        from datetime import UTC, datetime, timedelta

        from referraleasy_workers.hitl import HandoffRecord, HumanQueue

        queued = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
        closed = queued + timedelta(minutes=30)
        base = {
            "id": uuid4(),
            "workflow_id": uuid4(),
            "ticket_ref": "T-1",
            "reason": "no_match",
            "state": "PROVIDER_MATCHING",
            "claim_generation": 1,
            "claimed_by": None,
            "claimed_at": None,
            "claim_expires_at": None,
            "resolved_at": closed,
            "resolution": "alternative_offered",
            "resolution_note": None,
            "created_at": queued,
            "updated_at": closed,
        }

        class Recorder:
            def __init__(self) -> None:
                self.calls: list[tuple[str, float]] = []

            def hitl_wait(self, *, wait_ms: float, **_kw) -> None:
                self.calls.append(("wait", wait_ms))

            def hitl_resolve(self, *, resolve_ms: float, **_kw) -> None:
                self.calls.append(("resolve", resolve_ms))

        recorder = Recorder()
        queue = HumanQueue(None, telemetry=recorder)  # type: ignore[arg-type]
        queue._emit_timings(
            HandoffRecord.from_row(base),
            HandoffRecord.from_row({**base, "state": "resolved"}),
        )

        assert recorder.calls == [("wait", 30 * 60 * 1000)], (
            "an unclaimed ticket produced something other than exactly the wait measurement; §15's "
            "resolve metric is claim-to-close and there was no claim"
        )

    def test_a_claimed_ticket_emits_both(self) -> None:
        """The other half, so the test above is not satisfied by emitting nothing ever."""
        from datetime import UTC, datetime, timedelta

        from referraleasy_workers.hitl import HandoffRecord, HumanQueue

        queued = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
        claimed = queued + timedelta(minutes=5)
        closed = queued + timedelta(minutes=20)
        row = {
            "id": uuid4(),
            "workflow_id": uuid4(),
            "ticket_ref": "T-2",
            "reason": "no_match",
            "state": "PROVIDER_MATCHING",
            "claim_generation": 1,
            "claimed_by": "operator-1",
            "claimed_at": claimed,
            "claim_expires_at": claimed + timedelta(minutes=15),
            "resolved_at": closed,
            "resolution": "alternative_offered",
            "resolution_note": None,
            "created_at": queued,
            "updated_at": closed,
        }

        calls: list[tuple[str, float]] = []

        class Recorder:
            def hitl_wait(self, *, wait_ms: float, **_kw) -> None:
                calls.append(("wait", wait_ms))

            def hitl_resolve(self, *, resolve_ms: float, **_kw) -> None:
                calls.append(("resolve", resolve_ms))

        HumanQueue(None, telemetry=Recorder())._emit_timings(  # type: ignore[arg-type]
            HandoffRecord.from_row(row), HandoffRecord.from_row(row)
        )

        # Wait is queue-to-close (20 min); resolve is claim-to-close (15 min). Two different
        # intervals from the same row, which is the point of measuring both.
        assert calls == [("wait", 20 * 60 * 1000), ("resolve", 15 * 60 * 1000)]

    def test_no_emitter_is_not_an_error(self) -> None:
        """A worker with no collector resolves tickets; it just does not measure them."""
        from datetime import UTC, datetime

        from referraleasy_workers.hitl import HandoffRecord, HumanQueue

        now = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
        row = {
            "id": uuid4(),
            "workflow_id": uuid4(),
            "ticket_ref": "T-3",
            "reason": "no_match",
            "state": "PROVIDER_MATCHING",
            "claim_generation": 1,
            "claimed_by": None,
            "claimed_at": None,
            "claim_expires_at": None,
            "resolved_at": now,
            "resolution": None,
            "resolution_note": None,
            "created_at": now,
            "updated_at": now,
        }

        HumanQueue(None)._emit_timings(  # type: ignore[arg-type]
            HandoffRecord.from_row(row), HandoffRecord.from_row(row)
        )

    def test_the_queue_accepts_an_emitter_and_defaults_to_none(self) -> None:
        """The seam has to exist for a caller that has a collector, and be absent for one that does
        not: a worker with no OTLP endpoint must still be able to resolve tickets."""
        import inspect

        from referraleasy_workers.hitl import HumanQueue

        signature = inspect.signature(HumanQueue.__init__)
        assert "telemetry" in signature.parameters
        assert signature.parameters["telemetry"].default is None


# ---------------------------------------------------------------------------
# The operator command (§11's first caller)
# ---------------------------------------------------------------------------


class TestHandoffCommand:
    """`python -m referraleasy_workers handoff ...` — the entry point's operator half.

    Tested for what it *refuses*, which is where the thinking is. A resolve that filled in a default
    operator or a default return state would be a command that silently attributes a clinical
    decision to nobody and moves a workflow to a state a database trigger might or might not accept,
    depending on the day.
    """

    def _settings(self, monkeypatch, tmp_path):
        from referraleasy_workers.settings import WorkerSettings

        monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
        return WorkerSettings.from_env()

    def test_resolve_without_an_operator_is_refused(self, monkeypatch, capsys) -> None:
        from referraleasy_workers.__main__ import _handoff_resolve

        code = _handoff_resolve(
            self._settings(monkeypatch, None), ["abc-123", "--return-state", "PROVIDER_MATCHING"]
        )

        assert code == 2
        assert "--operator" in capsys.readouterr().err

    def test_resolve_without_a_return_state_is_refused(self, monkeypatch, capsys) -> None:
        from referraleasy_workers.__main__ import _handoff_resolve

        code = _handoff_resolve(
            self._settings(monkeypatch, None), ["abc-123", "--operator", "op-1"]
        )

        assert code == 2
        assert "--return-state" in capsys.readouterr().err

    def test_resolve_without_a_ticket_id_is_refused(self, monkeypatch, capsys) -> None:
        from referraleasy_workers.__main__ import _handoff_resolve

        code = _handoff_resolve(self._settings(monkeypatch, None), [])

        assert code == 2
        assert "needs a ticket id" in capsys.readouterr().err

    def test_a_dangling_flag_is_refused_rather_than_ignored(self, monkeypatch, capsys) -> None:
        """`--operator` with nothing after it is a caller's mistake, not a missing option — and
        reading the next argument as a value would silently take it from whatever followed."""
        from referraleasy_workers.__main__ import _handoff_resolve

        code = _handoff_resolve(self._settings(monkeypatch, None), ["abc-123", "--operator"])

        assert code == 2
        assert "cannot read" in capsys.readouterr().err

    def test_the_service_reports_its_own_version_for_the_resource_attribute(self) -> None:
        """`service.version` is on every span; a name that raised would take the worker down at
        startup for a label."""
        from referraleasy_workers.__main__ import service_version

        assert service_version()
