"""Reconciliation: one key, one verdict, and a person instead of a guess (§7, §17).

§7's rule about an unknown booking outcome is a rule about *keys*: "look up the result with the same
key; a second, independent booking key is never generated". Everything in this file exists to pin that
sentence from one side or the other:

* a lookup is issued **under the key the attempt used**, however many are needed, and the suite reads
  the keys off the fake facility (`distinct_lookup_keys`) rather than trusting the reconciler's own
  report of them;
* a **second key is refused before a request leaves the process** — both when a caller offers one and
  when the facility answers under one it was not asked about, because a facility answering under a key
  we did not mint means something is wrong that a worker must not paper over by adopting it;
* **no outcome is ever invented**. Failures and a spent budget escalate
  (`prolonged_unknown_booking`, §11) — never `resolved_not_booked`, because that verdict is exactly
  what authorises a new booking attempt. Two lookups that disagree escalate for the same reason;
* the loop is the **only retry owner**: three failures under a budget of three means three requests on
  the wire, not nine (a `run_with_retries` inside the loop would be the multiplication the runtime's
  budget exists to prevent);
* `pending` is a real answer — the caller's deadline arrived first — and it leaves the question open
  rather than closing it either way.

The facility here is `ScriptedBookingApi`, which implements the same `BookingApi` protocol the HTTP
adapter does, so the reconciler runs its real code and only the wire is missing. No model is involved
anywhere in this path and no test needs one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from worker_doubles import SPEC_CAPS, ScriptedBookingApi, _lookup_view

from referraleasy_agent_runtime import Clock, SystemClock
from referraleasy_contracts.generated.booking import ReconciliationStatus
from referraleasy_workers.booking import LookupOutcome, LookupView
from referraleasy_workers.errors import SecondKeyRefusedError
from referraleasy_workers.reconcile import (
    BookingReconciler,
    EscalationReason,
    ReconciliationAction,
    WallClock,
    needs_confirmation,
)

KEY = "idem-booking-1"
OTHER_KEY = "idem-somebody-elses"

FOUND = LookupOutcome.BOOKING_FOUND_UNDER_SAME_KEY
ABSENT = LookupOutcome.DEFINITIVELY_ABSENT_FOR_SAME_KEY
FAILED = LookupOutcome.LOOKUP_FAILED_OR_TIMED_OUT
DIFFERENT_KEY = LookupOutcome.BOOKING_FOUND_UNDER_A_DIFFERENT_KEY


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FixedClock:
    """A wall clock the test moves by hand — what `WallClock` asks for, and nothing else.

    Separate from `worker_doubles.FakeClock` on purpose: that one is the runtime's monotonic budget
    clock and has no `now()`. The reconciler reads wall time for its timestamps and for the caller's
    deadline, so the double it needs is this one. `test_the_clock_it_reads_is_a_wall_clock` pins the
    difference rather than leaving it to be rediscovered.
    """

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: Any) -> None:
        self._now += timedelta(**kwargs)


class Sleeps:
    """A `sleep` that records the delay instead of waiting for it.

    Both halves matter: the suite does not sleep for real, and the *delays* are one of the things under
    test — a backoff that ignored the provider's `Retry-After` would otherwise be invisible.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def reconciler(
    api: ScriptedBookingApi,
    *,
    lookup_budget: int = 5,
    clock: Any = None,
    sleep: Sleeps | None = None,
) -> BookingReconciler:
    return BookingReconciler(
        api=api,
        caps=SPEC_CAPS,
        lookup_budget=lookup_budget,
        clock=clock,
        sleep=sleep or Sleeps(),
    )


def reconcile(
    api: ScriptedBookingApi, *, key: str = KEY, candidate_key: str | None = None, **kwargs: Any
) -> Any:
    return asyncio.run(reconciler(api, **kwargs).reconcile(key=key, candidate_key=candidate_key))


# ---------------------------------------------------------------------------
# Answers, and what they resolve
# ---------------------------------------------------------------------------


def test_a_definitively_absent_answer_resolves_without_a_second_lookup() -> None:
    """A definite "there is no booking under this key" is the answer, not the first of many.

    The facility has told us the truth about *this* key; asking again cannot change it, and spending
    the budget on repetition only delays the decision the caller needs. One lookup, one verdict, and the
    verdict authorises a booking attempt — under the same key, as an idempotent retry of one logical
    attempt, which is why `ACTION.BOOK` is not a licence to mint anything.
    """
    api = ScriptedBookingApi(lookup=[_lookup_view(key=KEY, outcome=ABSENT)])

    outcome = reconcile(api)

    assert outcome.status is ReconciliationStatus.resolved_not_booked
    assert outcome.action is ReconciliationAction.BOOK
    assert outcome.not_booked is True and outcome.booked is False
    assert outcome.lookup_attempts == 1
    assert len(api.lookup_keys) == 1
    assert api.distinct_lookup_keys == {KEY}
    assert outcome.key_never_changed is True
    assert outcome.escalation_reason is None
    assert "no appointment booked" in outcome.patient_message
    assert outcome.state().status is ReconciliationStatus.resolved_not_booked
    assert outcome.state().escalation_reason is None


def test_a_found_answer_confirms_the_booking_and_creates_nothing() -> None:
    """The flip side, and the one that must never become a booking attempt.

    `CONFIRM_BOOKING` means the appointment already exists and the workflow may move to `BOOKED` after
    the durable record agrees. What it may *not* do is send anything: the assertion that no create was
    ever issued (`created_keys == []`) is the structural version of "a reconciliation cannot
    double-book", and it is asserted against the facility rather than against the reconciler's report.
    """
    api = ScriptedBookingApi(
        lookup=[
            _lookup_view(
                key=KEY, outcome=FOUND, booking_request_id="B-7", booking_state="confirmed"
            )
        ]
    )

    outcome = reconcile(api)

    assert outcome.booked is True
    assert outcome.action is ReconciliationAction.CONFIRM_BOOKING
    assert outcome.booking_request_id == "B-7"
    assert outcome.booking_state == "confirmed"
    assert outcome.attempts_summary() == "#1 booking_found_under_same_key (200)"
    assert api.created_keys == [], "reconciliation never sends a booking"
    assert api.every_lookup_used_one_key is True


def test_a_found_answer_the_facility_is_still_reconciling_is_read_a_second_time() -> None:
    """A record that is still moving is not a record yet.

    The facility reporting `booking_found_under_same_key` *and* its own reconciliation as `pending` is
    telling us it has not settled the question either. Acting on the first read is how a workflow ends
    up `BOOKED` against a booking that is then cancelled by the other side; the second read costs one
    lookup and settles it. `needs_confirmation` is the whole rule, and it is a pure predicate so it can
    be asserted directly as well as through the loop.
    """
    unsettled = _lookup_view(
        key=KEY, outcome=FOUND, booking_request_id="B-7", facility_reconciliation="pending"
    )
    settled = _lookup_view(
        key=KEY, outcome=FOUND, booking_request_id="B-7", facility_reconciliation="settled"
    )
    api = ScriptedBookingApi(lookup=[unsettled, settled])

    assert needs_confirmation(unsettled) is True
    assert needs_confirmation(settled) is False
    assert needs_confirmation(_lookup_view(key=KEY, outcome=ABSENT)) is False
    assert needs_confirmation(_lookup_view(key=KEY, outcome=FAILED)) is False

    outcome = reconcile(api)

    assert outcome.booked is True
    assert outcome.lookup_attempts == 2
    assert len(api.lookup_keys) == 2
    assert api.every_lookup_used_one_key is True


def test_a_record_that_never_settles_is_corroborated_rather_than_re_read_forever() -> None:
    """The re-read above happens *once*: a found answer that survived it is trusted.

    Worth stating explicitly, because the opposite rule looks more cautious and is worse. A facility
    that always reports its own reconciliation as `pending` would then never resolve a booking it has
    told us exists — the patient would be escalated to a person while holding an appointment, and the
    workflow could never leave `BOOKING_UNVERIFIED`. The re-read exists because a provisional record
    *may* still change; a record that is still there on the second look has been corroborated, and the
    remaining risk (it changes later) is the same risk a facility that reports `settled` carries. The
    genuinely unanswered case has its own escalation paths: budget exhaustion and `FACILITY_GAVE_UP`.
    """
    pending = _lookup_view(
        key=KEY, outcome=FOUND, booking_request_id="B-7", facility_reconciliation="pending"
    )
    api = ScriptedBookingApi(lookup=[pending, pending, pending, pending])

    outcome = reconcile(api, lookup_budget=4)

    assert outcome.booked is True
    assert outcome.booking_request_id == "B-7"
    assert outcome.lookup_attempts == 2, "one re-read, not one per pass"
    assert len(api.lookup_keys) == 2, (
        "the budget carries over to a later pass instead of being spent here"
    )


# ---------------------------------------------------------------------------
# Failures: retried under the same key, then handed to a person
# ---------------------------------------------------------------------------


def test_a_failed_lookup_is_retried_under_the_same_key_with_our_backoff() -> None:
    """The loop is the only retry owner, and the key it retries under never changes.

    Three failures under a budget of three means exactly three requests on the wire: a
    `run_with_retries` wrapped around this loop would have sent nine, and the reconciliation would then
    be hammering a system that is already unwell. The delays are the runtime's curve, recorded rather
    than waited for (`retry_backoff_base_ms` is the spec's 150 ms).
    """
    api = ScriptedBookingApi(lookup=[_lookup_view(key=KEY, outcome=FAILED, http_status=504)] * 3)
    sleeps = Sleeps()

    outcome = reconcile(api, lookup_budget=3, sleep=sleeps)

    assert len(api.lookup_keys) == 3, "three attempts, three requests — no nested retry loop"
    assert api.distinct_lookup_keys == {KEY}, "the key never changed, on any attempt"
    assert outcome.lookup_attempts == 3
    assert outcome.key_never_changed is True
    assert len(sleeps.delays) == 2, "a pause between attempts, not after the last one"
    assert sleeps.delays[0] == SPEC_CAPS.retry_backoff_base_ms / 1000
    assert sleeps.delays[1] > sleeps.delays[0], "the runtime's curve backs off"
    assert outcome.attempts_summary() == (
        "#1 lookup_failed_or_timed_out (504); #2 lookup_failed_or_timed_out (504); "
        "#3 lookup_failed_or_timed_out (504)"
    )


def test_the_lookup_budget_running_out_escalates_instead_of_answering_no() -> None:
    """Exhaustion is `escalated` — never `resolved_not_booked`, which would authorise a new booking.

    This is the requirement in one assertion pair: the verdict is an escalation, and the escalation is
    §11's `prolonged_unknown_booking` with a person attached. The alternative — treating "we could not
    find out" as "there is nothing" — is the double-booking bug arriving through the back door, because
    the next step after `resolved_not_booked` is sending a booking request.
    """
    api = ScriptedBookingApi(lookup=[_lookup_view(key=KEY, outcome=FAILED, http_status=503)] * 5)

    outcome = reconcile(api, lookup_budget=5)

    assert outcome.is_escalated is True
    assert outcome.is_resolved is False
    assert outcome.not_booked is False, "an unanswered question is not a no"
    assert outcome.action is ReconciliationAction.ESCALATE
    assert outcome.escalation_reason is EscalationReason.BUDGET_EXHAUSTED
    assert outcome.lookup_attempts == 5
    assert "no answer after 5 lookups under the same key" in outcome.detail
    assert "A person on our team is picking this up" in outcome.patient_message
    assert outcome.state().escalation_reason == EscalationReason.BUDGET_EXHAUSTED.value
    assert outcome.state().lookup_attempts == 5


def test_a_provider_that_names_a_delay_gets_it() -> None:
    """The provider's window beats our curve, and the pause is visible in the injected sleep.

    The runtime's `backoff_delay_ms` returns the named delay *instead of* the local curve rather than
    the greater of the two, which is the right way round here: a provider that answered 429 with a
    window has already scheduled its next refusal, and a worker that substitutes its own shorter pause
    spends the rest of its budget inside that window.
    """
    api = ScriptedBookingApi(
        lookup=[
            _lookup_view(key=KEY, outcome=FAILED, http_status=429, retry_after_s=2.0),
            _lookup_view(key=KEY, outcome=ABSENT),
        ]
    )
    sleeps = Sleeps()

    outcome = reconcile(api, sleep=sleeps)

    assert outcome.not_booked is True
    assert sleeps.delays == [2.0], "the provider's window, not the 150 ms base"
    assert api.distinct_lookup_keys == {KEY}


def test_the_facility_reporting_its_own_escalation_stops_the_loop() -> None:
    """When the other side says it has given up, spending our budget on the same question is not due
    diligence — it is waiting for an answer that is not coming.

    The reason class is distinct from `BUDGET_EXHAUSTED` on purpose: one means "we ran out", the other
    means "they ran out", and a human reading the ticket needs to know which.
    """
    api = ScriptedBookingApi(
        lookup=[
            _lookup_view(
                key=KEY, outcome=FAILED, http_status=504, facility_reconciliation="escalated"
            )
        ]
    )

    outcome = reconcile(api)

    assert outcome.is_escalated is True
    assert outcome.escalation_reason is EscalationReason.FACILITY_GAVE_UP
    assert outcome.lookup_attempts == 1, "the rest of the budget would have bought nothing"
    assert len(api.lookup_keys) == 1


def test_two_lookups_that_disagree_go_to_a_person() -> None:
    """Contradiction is not resolved by recency, and not by majority.

    A pass that saw both "it is there" and "it is not there" must not return whichever arrived last: one
    of the two answers is wrong, the two possible actions are "confirm an appointment" and "book
    another", and guessing between them is exactly the §11 situation (`conflicting_sources`) a person
    exists for.

    The sequence is the only one that can reach it, and that is a fact about the other tests here: a
    *definite* answer ends the pass, so the first of the two has to be the one unsettled found read
    that did not yet decide (`test_a_found_answer_the_facility_is_still_reconciling_is_read_a_second_time`)
    — the record the facility showed us and then withdrew.
    """
    unsettled = _lookup_view(
        key=KEY, outcome=FOUND, booking_request_id="B-7", facility_reconciliation="pending"
    )
    api = ScriptedBookingApi(lookup=[unsettled, _lookup_view(key=KEY, outcome=ABSENT)])

    outcome = reconcile(api)

    assert outcome.is_escalated is True
    assert outcome.escalation_reason is EscalationReason.LOOKUPS_DISAGREE
    assert outcome.booked is False and outcome.not_booked is False
    assert outcome.booking_request_id is None, (
        "neither of the two answers is reported as *the* answer"
    )
    assert outcome.lookup_attempts == 2
    assert "one lookup reported the booking exists and another that it does not" in outcome.detail
    assert "A person on our team" in outcome.patient_message


def test_a_definite_answer_is_trusted_on_the_first_read_and_spends_one_lookup() -> None:
    """No answer is re-read "just in case", and the contradiction above is unreachable without one.

    Same-key assertions on both sides: the pass that resolved did so under one key, and the answer
    scripted behind it was never requested — a worker that read every answer twice would spend the
    budget it needs for the pass where the facility is actually unwell.
    """
    found = _lookup_view(
        key=KEY, outcome=FOUND, booking_request_id="B-7", facility_reconciliation="settled"
    )
    api = ScriptedBookingApi(lookup=[found, _lookup_view(key=KEY, outcome=ABSENT)])

    outcome = reconcile(api)

    assert outcome.booked is True
    assert outcome.lookup_attempts == 1
    assert api.lookup_keys == [KEY], "the second scripted answer was never asked for"
    assert outcome.state().status is ReconciliationStatus.resolved_booked
    assert outcome.state().lookup_attempts == 1


# ---------------------------------------------------------------------------
# The second key (§7, verbatim)
# ---------------------------------------------------------------------------


def test_a_candidate_second_key_is_refused_before_the_lookup_is_issued() -> None:
    """The refusal has to happen before the request, so the fake facility's call log is the assertion.

    A second key is not an invalid request — it is a perfectly valid *new booking*, which is why the
    database cannot refuse it and why this is the one requirement that lives in Python. The error
    carries both keys so the caller can see what it nearly did.
    """
    api = ScriptedBookingApi()

    with pytest.raises(SecondKeyRefusedError) as refused:
        reconcile(api, candidate_key=OTHER_KEY)

    assert refused.value.original_key == KEY
    assert refused.value.candidate_key == OTHER_KEY
    assert "a new key is a new booking" in str(refused.value)
    assert api.lookup_keys == [], "nothing left the process"
    assert api.created_keys == []


def test_an_answer_about_another_key_is_refused_rather_than_adopted() -> None:
    """An adapter that answers for a key nobody asked about is a bug, and it must not be indistinguishable.

    Two different situations produce it — a broken adapter and a facility answering under a key we did
    not mint — and this module refuses both at the same point, immediately after the answer arrives.
    Adoption is the dangerous response: it would make `AWAITING_...`→`BOOKED` rest on an identity this
    workflow never created.
    """
    api = ScriptedBookingApi(
        lookup=[
            LookupView(
                outcome=FOUND, idempotency_key=OTHER_KEY, http_status=200, booking_request_id="B-9"
            )
        ]
    )

    with pytest.raises(SecondKeyRefusedError) as refused:
        reconcile(api)

    assert refused.value.candidate_key == OTHER_KEY
    assert api.distinct_lookup_keys == {KEY}, "the key we asked under, whatever came back"


def test_a_facility_answering_under_a_different_key_escalates_with_that_key_named() -> None:
    """The *reported* version of the same situation: the facility says it found it under another key.

    Here the answer is well-formed and about a booking that may really exist — under a key this
    workflow did not create. Escalating with the other key in the detail is the only honest response:
    the patient may have an appointment, and who put it there is a question for a person.
    """
    api = ScriptedBookingApi(
        lookup=[
            _lookup_view(
                key=KEY,
                outcome=DIFFERENT_KEY,
                other_key=OTHER_KEY,
                booking_request_id="B-9",
            )
        ]
    )

    outcome = reconcile(api)

    assert outcome.is_escalated is True
    assert outcome.escalation_reason is EscalationReason.DIFFERENT_KEY
    assert outcome.key == KEY, "the outcome is about our key, not the one they answered with"
    assert OTHER_KEY in outcome.detail, "the other key is named, for the person reading the ticket"
    assert outcome.lookup_attempts == 1, "no retrying around a contradiction a person can explain"
    assert api.created_keys == []
    assert "A person on our team" in outcome.patient_message


# ---------------------------------------------------------------------------
# Time: the caller's deadline, and the clock this module reads
# ---------------------------------------------------------------------------


def test_a_deadline_that_arrives_first_is_pending_rather_than_a_verdict() -> None:
    """A caller's deadline is not an answer: the question stays open and is re-run later, same key.

    `pending` exists so that a pass which ran out of *caller* time does not close a question it never
    asked. Nothing was looked up here at all, so the outcome cannot be mistaken for a verdict — and the
    workflow stays in `BOOKING_UNVERIFIED`, which `test_pipeline.py` asserts from the other side.
    """
    api = ScriptedBookingApi()
    clock = FixedClock()

    outcome = asyncio.run(reconciler(api, clock=clock).reconcile(key=KEY, deadline=clock.now()))

    assert outcome.is_pending is True
    assert outcome.action is ReconciliationAction.RETRY_LATER
    assert outcome.status is ReconciliationStatus.pending
    assert outcome.lookup_attempts == 0
    assert api.lookup_keys == []
    assert "still checking with the hospital" in outcome.patient_message
    assert outcome.state().escalation_reason is None
    assert outcome.last_lookup_at is None, "no lookup happened, so there is no last lookup time"


def test_the_clock_it_reads_is_a_wall_clock_and_not_the_budgets() -> None:
    """The two clocks answer different questions, and the reconciler needs only one of them.

    `WallClock` exists because the runtime's `Clock` is a *monotonic budget* clock: it has
    `monotonic_ms`/`sleep_ms` and no `now()`, so the parameter used to be annotated with a protocol
    whose one method this module calls was missing from it. Passing a `SystemClock` then raised
    `AttributeError` inside a reconciliation — a crash in the middle of the one code path that exists
    to keep a booking from being duplicated. The assertions below pin both halves: the protocol this
    module asks for offers `now()`, and the runtime's budget clock is not it. If the runtime ever grows
    one, the second assertion fails and the two protocols can be merged deliberately.
    """
    assert hasattr(WallClock, "now"), (
        "the parameter's protocol asks for the wall time this module reads"
    )
    assert not hasattr(SystemClock, "now"), (
        "the budget clock is monotonic: it has no wall time in it"
    )
    assert hasattr(SystemClock, "monotonic_ms") and hasattr(Clock, "monotonic_ms")

    clock = FixedClock()
    api = ScriptedBookingApi(lookup=[_lookup_view(key=KEY, outcome=ABSENT)])

    outcome = asyncio.run(reconciler(api, clock=clock).reconcile(key=KEY))
    clock.advance(minutes=2)

    assert outcome.started_at == datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    assert outcome.finished_at == outcome.started_at, "the timestamps come from the injected clock"
    assert outcome.last_lookup_at == outcome.finished_at
    # The contract wraps its timestamps (`IsoTimestamp`), so the comparison is unwrapped here rather
    # than left as a type error waiting for the next person: the stored value is the same instant.
    assert outcome.state().started_at.root == outcome.started_at
