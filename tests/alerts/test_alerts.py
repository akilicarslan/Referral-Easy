"""§16's alerting: the rules, the transitions, and an outbox that cannot loop.

The file is organised around the four ways this design could be wrong, because each of them is a way
an alerting system fails silently:

* **a rule that cannot measure reports a pass** — a service that stops emitting has no failures *and*
  no evidence, and that is the moment an alert matters most;
* **a rule fires below its own minimum** — §16 says *"en az 20 invocation varken"*, so the count is
  part of the condition and not a footnote;
* **a gap closes an open alert** — treating `UNMEASURED` as a resolution ends an alert because the
  telemetry stopped, which is the same failure as the first one wearing a different hat;
* **a delivery failure opens a loop** — §16 forbids it, and the only reliable way to forbid it is for
  the path not to exist.

Everything here is pure: no OpenSearch, no database, no network. The rules take measurements and
return decisions, so the assertions are about decisions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from referraleasy_alerts import (
    RULES,
    Cooldown,
    DeliveryState,
    Engine,
    InMemoryTransitions,
    LatencyWindow,
    Outbox,
    QualityDrop,
    QualityWindow,
    RateWindow,
    RecordingSender,
    RuleName,
    Severity,
    State,
    SustainedLatency,
    TerminalFailureRate,
    compose,
)
from referraleasy_alerts.models import AlertEvent, Evaluation
from referraleasy_telemetry import Sample

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def sample(count: int, documents: int | None = None) -> Sample:
    return Sample(count=count, documents=documents if documents is not None else count)


def evaluation(
    rule: RuleName = RuleName.TERMINAL_FAILURE_RATE, state: State = State.OK
) -> Evaluation:
    """A shaped evaluation, for the tests that are about transitions rather than about rules."""
    return Evaluation(
        rule=rule,
        state=state,
        detail="a detail",
        observed="observed",
        threshold="threshold",
        sample=sample(100),
    )


# ---------------------------------------------------------------------------
# 1. A rule that cannot measure reports a gap, not a pass
# ---------------------------------------------------------------------------


class TestUnmeasuredIsNotOk:
    def test_a_quiet_window_has_no_failure_rate(self) -> None:
        """`0/0` is not `0%`. A window with no invocations has no rate, and calling it zero is how a
        service that stopped reporting looks like a service that is fine."""
        result = TerminalFailureRate().evaluate(
            RateWindow(total=sample(0, 0), failing=sample(0, 0), window_seconds=300)
        )

        assert result.state is State.UNMEASURED
        assert result.firing is False
        assert "no invocations" in result.detail

    def test_the_invocation_floor_is_part_of_the_condition(self) -> None:
        """§16's "en az 20 invocation varken". Four invocations with one failure is 25% and is not an
        incident — firing on it is how a rule gets muted."""
        result = TerminalFailureRate().evaluate(
            RateWindow(total=sample(4), failing=sample(1), window_seconds=300)
        )

        assert result.state is State.UNMEASURED, "fired below the rule's own minimum"
        assert "below the 20" in result.detail

    def test_a_missing_p95_window_cannot_count_as_below_the_ceiling(self) -> None:
        """Three windows, one of them with no observations: the rule has two measurements and a gap,
        and a gap is not evidence that latency is fine."""
        windows = (
            LatencyWindow(p95_ms=3000.0, sample=sample(50), label="w1"),
            LatencyWindow(p95_ms=None, sample=sample(0), label="w2"),
            LatencyWindow(p95_ms=3100.0, sample=sample(50), label="w3"),
        )
        result = SustainedLatency().evaluate(windows)

        assert result.state is State.UNMEASURED
        assert "w2" in result.detail

    def test_fewer_windows_than_the_rule_needs_is_a_gap(self) -> None:
        result = SustainedLatency().evaluate((LatencyWindow(p95_ms=9000.0, sample=sample(10)),))

        assert result.state is State.UNMEASURED
        assert "2 window(s)" not in result.detail  # it says how many were seen
        assert "1 window(s)" in result.detail

    def test_a_quality_drop_needs_samples_on_both_sides(self) -> None:
        """A confident baseline against a thin candidate is the comparison that invents regressions."""
        result = QualityDrop().evaluate(QualityWindow(score=0.60, baseline=0.90, sample=sample(5)))

        assert result.state is State.UNMEASURED
        assert "below the 30" in result.detail

    def test_a_missing_score_is_not_a_drop_of_zero(self) -> None:
        result = QualityDrop().evaluate(
            QualityWindow(score=None, baseline=0.90, sample=sample(100))
        )

        assert result.state is State.UNMEASURED
        assert "not a drop of zero" in result.detail


# ---------------------------------------------------------------------------
# 2. And a rule that can measure does fire, on the shape §16 describes
# ---------------------------------------------------------------------------


class TestFiring:
    def test_the_failure_rate_fires_above_the_threshold_with_enough_invocations(self) -> None:
        result = TerminalFailureRate().evaluate(
            RateWindow(total=sample(100), failing=sample(21), window_seconds=300)
        )

        assert result.state is State.FIRING
        assert result.observed == "21/100 = 21.0%"
        assert result.threshold.startswith("terminal failure > 20%")

    def test_exactly_at_the_threshold_is_not_above_it(self) -> None:
        """§16 says `>%20`. At 20% the rule is quiet, and the boundary is asserted because "over 20%"
        and "20% or more" are the kind of difference a later edit silently changes."""
        result = TerminalFailureRate().evaluate(
            RateWindow(total=sample(100), failing=sample(20), window_seconds=300)
        )

        assert result.state is State.OK

    def test_a_measured_zero_is_ok(self) -> None:
        """The other direction, and the reason `UNMEASURED` has to be separate: one hundred
        invocations and no failures is a real pass, not a gap."""
        result = TerminalFailureRate().evaluate(
            RateWindow(total=sample(100), failing=sample(0), window_seconds=300)
        )

        assert result.state is State.OK

    def test_all_three_consecutive_windows_must_be_over_the_ceiling(self) -> None:
        over = LatencyWindow(p95_ms=3000.0, sample=sample(50))
        under = LatencyWindow(p95_ms=100.0, sample=sample(50))

        assert SustainedLatency().evaluate((over, over, over)).state is State.FIRING
        assert SustainedLatency().evaluate((over, under, over)).state is State.OK

    def test_only_the_last_three_windows_are_considered(self) -> None:
        """The caller passes history oldest-first; a spike five windows ago is not a sustained one."""
        over = LatencyWindow(p95_ms=3000.0, sample=sample(50))
        under = LatencyWindow(p95_ms=100.0, sample=sample(50))

        assert SustainedLatency().evaluate((over, over, under, under, under)).state is State.OK

    def test_an_open_breaker_fires_and_a_closed_one_does_not(self) -> None:
        from referraleasy_alerts import CircuitBreakerOpen

        rule = CircuitBreakerOpen()
        assert rule.evaluate(open_=True, endpoint="/slots").state is State.FIRING
        assert rule.evaluate(open_=False, endpoint="/slots").state is State.OK

    def test_a_quality_drop_fires_on_an_absolute_fall_over_enough_cases(self) -> None:
        result = QualityDrop().evaluate(QualityWindow(score=0.70, baseline=0.90, sample=sample(60)))

        assert result.state is State.FIRING
        assert "fell 0.200" in result.detail

    def test_the_four_rules_are_the_four_s16_names(self) -> None:
        assert {rule.name for rule in RULES} == {
            RuleName.TERMINAL_FAILURE_RATE,
            RuleName.SUSTAINED_LATENCY,
            RuleName.CIRCUIT_OPEN,
            RuleName.QUALITY_DROP,
        }

    def test_every_rule_publishes_its_threshold(self) -> None:
        """A page has to be able to show what would have to change, so a rule without one is a rule
        whose alerts cannot be argued with."""
        for rule in RULES:
            instance = rule()
            assert instance.threshold, rule.__name__
            assert len(instance.threshold) > 10, rule.__name__


# ---------------------------------------------------------------------------
# 3. Transitions: a state is not an event, and a gap closes nothing
# ---------------------------------------------------------------------------


class TestEngine:
    def test_a_rule_that_stays_firing_produces_one_event(self) -> None:
        """The scheduler runs every minute against overlapping windows. Without this, one incident is
        sixty notifications an hour."""
        engine = Engine(InMemoryTransitions())

        first = engine.decide(evaluation(state=State.FIRING), now=NOW)
        second = engine.decide(evaluation(state=State.FIRING), now=NOW + timedelta(minutes=1))
        third = engine.decide(evaluation(state=State.FIRING), now=NOW + timedelta(minutes=2))

        assert first.event is not None and first.event.opened is True
        assert second.event is None
        assert third.event is None

    def test_a_resolution_is_an_event_of_its_own(self) -> None:
        """§16 asks for the resolved case to be visible. An alerting system that only announces
        openings leaves every reader to guess when it ended."""
        engine = Engine(InMemoryTransitions())
        engine.decide(evaluation(state=State.FIRING), now=NOW)

        resolved = engine.decide(evaluation(state=State.OK), now=NOW + timedelta(minutes=5))

        assert resolved.event is not None
        assert resolved.event.opened is False
        assert resolved.event.kind == "resolved"

    def test_a_gap_does_not_close_an_open_alert(self) -> None:
        """The property this design exists for. Telemetry stopping is the moment an operator most
        needs the alert to stay open, and treating the gap as a recovery would close it."""
        engine = Engine(InMemoryTransitions())
        engine.decide(evaluation(state=State.FIRING), now=NOW)

        gap = engine.decide(evaluation(state=State.UNMEASURED), now=NOW + timedelta(minutes=5))

        assert gap.event is None, "a gap was reported as a transition"
        assert engine.firing() != {}, "the alert was closed by a gap"

    def test_a_gap_does_not_open_one_either(self) -> None:
        engine = Engine(InMemoryTransitions())

        gap = engine.decide(evaluation(state=State.UNMEASURED), now=NOW)

        assert gap.event is None
        assert engine.firing() == {}

    def test_the_cooldown_suppresses_a_reopen_notification_and_keeps_the_state(self) -> None:
        """A cooldown that also suppressed the state would make the dashboard and the mailbox
        disagree about the same system."""
        engine = Engine(InMemoryTransitions(), cooldown=Cooldown(minutes=15))
        engine.decide(evaluation(state=State.FIRING), now=NOW)
        engine.decide(evaluation(state=State.OK), now=NOW + timedelta(minutes=1))

        reopened = engine.decide(evaluation(state=State.FIRING), now=NOW + timedelta(minutes=2))

        assert reopened.event is None
        assert reopened.suppressed_by_cooldown is True
        assert engine.firing() != {}, "the state was suppressed along with the notification"

    def test_a_reopen_after_the_cooldown_is_announced(self) -> None:
        engine = Engine(InMemoryTransitions(), cooldown=Cooldown(minutes=15))
        engine.decide(evaluation(state=State.FIRING), now=NOW)
        engine.decide(evaluation(state=State.OK), now=NOW + timedelta(minutes=1))

        later = engine.decide(evaluation(state=State.FIRING), now=NOW + timedelta(minutes=30))

        assert later.event is not None
        assert later.event.opened is True

    def test_a_series_gets_its_own_alert(self) -> None:
        """One agent firing and then another is two things worth saying; the same agent firing again
        is one. The series is how a caller says which it has."""
        engine = Engine(InMemoryTransitions())

        first = engine.decide(evaluation(state=State.FIRING), now=NOW, series="scheduling")
        second = engine.decide(evaluation(state=State.FIRING), now=NOW, series="coverage")

        assert first.event is not None
        assert second.event is not None, "a second subject was deduped into the first"

    def test_an_opening_is_a_page_for_the_rules_that_deserve_one(self) -> None:
        engine = Engine(InMemoryTransitions())

        opened = engine.decide(evaluation(rule=RuleName.CIRCUIT_OPEN, state=State.FIRING), now=NOW)

        assert opened.event is not None
        assert opened.event.severity is Severity.PAGE


# ---------------------------------------------------------------------------
# 4. The outbox: it composes, it records, and it cannot loop
# ---------------------------------------------------------------------------


class TestOutbox:
    def _event(self) -> AlertEvent:
        rule = TerminalFailureRate()
        result = rule.evaluate(
            RateWindow(total=sample(100), failing=sample(30), window_seconds=300)
        )
        return AlertEvent(
            rule=result.rule,
            severity=Severity.PAGE,
            opened=True,
            at=NOW.isoformat(),
            detail=result.detail,
            evaluation=result,
        )

    def test_a_message_carries_the_measurement_it_is_about(self) -> None:
        """An alert that cannot be acted on from the message itself is the kind that gets filtered
        into a folder."""
        email = compose(self._event())

        assert "30.0%" in email.body
        assert "at least 20 invocations" in email.body, "the threshold is not in the message"
        assert "100 event(s)" in email.body, "the sample is not in the message"

    def test_the_message_says_it_was_not_sent(self) -> None:
        assert "was not sent to anyone" in compose(self._event()).body

    def test_delivery_moves_the_state_and_records_the_attempt(self) -> None:
        sender = RecordingSender()
        outbox = Outbox(sender)
        message = outbox.add(self._event(), to="ops@demo", now=NOW)
        assert message is not None

        delivered = outbox.deliver(message, now=NOW)

        assert delivered.state is DeliveryState.DELIVERED
        assert delivered.attempt_count == 1
        assert delivered.delivered_at == NOW
        assert sender.sent == [delivered.id]

    def test_a_failed_delivery_is_retried_and_then_dies(self) -> None:
        """§16: "A notification failure does not start a new infinite email loop". A retry that
        never terminates is
        that loop one layer down, so the policy is bounded and terminal."""
        sender = RecordingSender(fail_next=5)
        outbox = Outbox(sender, max_attempts=3)
        message = outbox.add(self._event(), to="ops@demo", now=NOW)
        assert message is not None

        outbox.deliver(message, now=NOW)
        assert message.state is DeliveryState.FAILED
        outbox.deliver(message, now=NOW)
        assert message.state is DeliveryState.FAILED
        outbox.deliver(message, now=NOW)

        assert message.state is DeliveryState.DEAD
        assert message.attempt_count == 3
        assert outbox.pending() == (), "a dead message is still queued for retry"

    def test_a_dead_message_is_not_retried_by_a_later_pass(self) -> None:
        sender = RecordingSender(fail_next=1)
        outbox = Outbox(sender, max_attempts=1)
        message = outbox.add(self._event(), to="ops@demo", now=NOW)
        assert message is not None
        outbox.deliver(message, now=NOW)
        assert message.state is DeliveryState.DEAD

        outbox.deliver_pending(now=NOW + timedelta(minutes=5))

        assert message.attempt_count == 1, "a terminal message was tried again"

    def test_the_same_transition_is_never_queued_twice(self) -> None:
        """The loop, closed at the earliest point: an at-least-once delivery path that re-reaches
        `add` finds the key instead of composing a second message."""
        outbox = Outbox(RecordingSender())
        event = self._event()

        first = outbox.add(event, to="ops@demo", now=NOW)
        second = outbox.add(event, to="ops@demo", now=NOW)

        assert first is not None
        assert second is None
        assert len(outbox.items()) == 1

    def test_a_resolution_is_a_different_key_from_its_opening(self) -> None:
        """Otherwise the resolve would be swallowed as a duplicate of the alert it closes."""
        outbox = Outbox(RecordingSender())
        opened = self._event()
        resolved = AlertEvent(
            rule=opened.rule,
            severity=opened.severity,
            opened=False,
            at=NOW.isoformat(),
            detail="back to normal",
            evaluation=opened.evaluation,
        )

        assert outbox.add(opened, to="ops@demo", now=NOW) is not None
        assert outbox.add(resolved, to="ops@demo", now=NOW) is not None

    def test_the_state_tally_lists_every_state_even_at_zero(self) -> None:
        """A missing state reads as absent; a zero state reads as zero, and the page needs the
        difference."""
        counts = Outbox(RecordingSender()).counts()

        assert set(counts) == {str(state) for state in DeliveryState}
        assert all(value == 0 for value in counts.values())

    def test_a_sender_that_raises_is_a_failed_delivery_not_a_crash(self) -> None:
        sender = RecordingSender(fail_next=1)
        outbox = Outbox(sender)
        message = outbox.add(self._event(), to="ops@demo", now=NOW)
        assert message is not None

        outbox.deliver(message, now=NOW)

        assert message.state is DeliveryState.FAILED
        assert message.last_error is not None
        assert "refuses to deliver" in message.last_error


@pytest.mark.parametrize("rule", RULES)
def test_no_rule_can_return_ok_below_its_own_floor(rule) -> None:
    """A cross-rule guard, because the failure is a property of the family rather than of one rule.

    Each rule is called with a measurement that is *worse* than its threshold but computed from too
    little data. None of them may answer `OK` — which is what a rule that checked its condition
    before its sample would do.
    """
    instance = rule()
    result = None
    if isinstance(instance, TerminalFailureRate):
        result = instance.evaluate(
            RateWindow(total=sample(3), failing=sample(3), window_seconds=300)
        )
    elif isinstance(instance, SustainedLatency):
        result = instance.evaluate((LatencyWindow(p95_ms=90_000.0, sample=sample(0)),))
    elif isinstance(instance, QualityDrop):
        result = instance.evaluate(QualityWindow(score=0.0, baseline=1.0, sample=sample(1)))
    else:
        # The breaker takes a fact rather than a measurement, so it has no floor to fall below.
        return

    assert result is not None
    assert result.state is not State.OK, f"{rule.__name__} passed on a sample below its floor"
