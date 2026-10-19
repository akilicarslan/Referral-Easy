# ruff: noqa: RUF002  # §9's Turkish sentence about the short-circuit is quoted verbatim
"""The orchestrator: §9's order, the short-circuit, the two profiles, and what "complete" means (§9).

§9's sentence for this file is *"Tam showcase profilinde üç katman görünür; açık block kalan çağrıları
keser. … Daha hızlı conditional profil ayrıca karşılaştırılır"* — three layers shown, an explicit
block cutting the remaining calls, and the faster profile **compared** rather than assumed better.

The layers here are :class:`SpyLayer` doubles, deliberately: the rules under test are the
orchestrator's, and a double whose answer and configuration a test chose is the only way to see them.
What is asserted, in the order this file argues it:

**The order and the precedence.** Layers run in §9's order (a reordering is an exception at
construction, not a quiet policy change); the first non-``allow`` verdict decides; a later ``block``
replaces an earlier ``clarify`` because a refusal is stronger than a question; and an ``allow`` never
overrides a decision a layer already made. The outcome's reason is the *deciding* verdict's reason,
which is asserted separately because a ``clarify`` that quotes a layer which answered ``allow`` would
send an operator to look at the wrong layer.

**The short-circuit.** A ``block`` stops the calls after it — §9's *"açık block kalan çağrıları
keser"* — and the layers that never ran are neither ``available`` nor ``skipped``: they were not
consulted, and saying either would be a claim nobody can check.

**Two profiles, and the premise the faster one rests on.** ``conditional`` skips exactly the calls a
*decisive* verdict makes unnecessary (an explicit attack, an emergency, an oversized or empty message,
a short reply the anchored pattern matched in full). The comparison is not taken on faith: the
profiles are run over the same scripted scenarios and required to agree — *except* in the one shape
where the premise is false (a decisive ``allow`` and a later layer that would have blocked), which is
built here explicitly because §9 asks for the comparison rather than for the assumption. The corpus
measurement that decides whether that shape occurs on real messages lives in
``test_guardrail_measurement.py``.

**Closed means closed.** An unavailable layer is recorded as unavailable even by the profile that
does not call it, an incomplete screening never leaves an ``allow`` standing, and
:meth:`GuardrailPipeline.require_complete` refuses the write with the missing layers named.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from guardrail_fixtures import SpyLayer, TickClock, spy_layers, verdict

from referraleasy_contracts.generated.common import (
    GuardrailCategory,
    GuardrailDecision,
    GuardrailLayer,
)
from referraleasy_guardrails.pipeline import (
    MEASURED_PER_LAYER,
    GuardrailOutcome,
    GuardrailPipeline,
    InputPathClosed,
    SystemClock,
    layer_report,
)
from referraleasy_guardrails.types import LAYER_ORDER, mask_pii

REGEX = GuardrailLayer.regex
DECISION_MODEL = GuardrailLayer.decision_model
LOCAL_CLASSIFIER = GuardrailLayer.local_classifier

#: A message with the three identifier kinds the masking exists for, so "the layer above layer 1 saw
#: the masked text" is asserted on a text that had something to mask.
SENSITIVE = "randevu istiyorum, e-posta ali@example.com ve üye no 2205518"


def pipeline_for(layers: Sequence[SpyLayer], *, profile: str = "three_stage") -> GuardrailPipeline:
    from referraleasy_guardrails.config import GuardrailProfile

    return GuardrailPipeline(
        layers=tuple(layers), clock=TickClock(), profile=GuardrailProfile(profile)
    )


def screen(
    pipeline: GuardrailPipeline, text: str = "merhaba, randevu almak istiyorum"
) -> GuardrailOutcome:
    return pipeline.screen(text, workflow_state="INTAKE")


#: The scripted reason each layer gives, so "the outcome quotes the deciding verdict" is asserted
#: against a string a test can check rather than against whichever layer happened to answer last.
REASONS = {
    REGEX: "regex says so",
    DECISION_MODEL: "model says so",
    LOCAL_CLASSIFIER: "classifier says so",
}


# ---------------------------------------------------------------------------
# §9's order, and the precedence between verdicts
# ---------------------------------------------------------------------------


def test_the_pipeline_refuses_to_run_the_layers_in_any_order_other_than_step_9_s() -> None:
    """§9 names the order; the pipeline makes a reordering an exception rather than a policy change."""
    layers = spy_layers()
    with pytest.raises(ValueError) as raised:
        GuardrailPipeline(layers=(layers[1], layers[0], layers[2]), clock=TickClock())
    message = str(raised.value)
    assert "decision_model" in message and "regex" in message
    assert "order" in message
    # The correct order is accepted, so the check is not refusing everything.
    GuardrailPipeline(layers=layers, clock=TickClock())


def test_a_benign_message_runs_every_layer_and_comes_back_as_an_allow() -> None:
    layers = spy_layers()
    outcome = screen(pipeline_for(layers))
    assert outcome.decision is GuardrailDecision.allow
    assert outcome.category is GuardrailCategory.benign
    assert outcome.complete is True
    assert outcome.skipped == ()
    assert [verdict_.layer for verdict_ in outcome.verdicts] == list(LAYER_ORDER)
    assert [len(layer.calls) for layer in layers] == [1, 1, 1]
    assert outcome.response is None
    # No layer decided anything, so the reason says exactly that rather than borrowing one.
    assert outcome.reason == "no layer produced a verdict"


def test_a_block_at_layer_one_stops_the_calls_after_it() -> None:
    """§9's *"açık block kalan çağrıları keser"*: a message already refused costs no model call."""
    layers = spy_layers(
        regex=verdict(
            REGEX,
            GuardrailDecision.block,
            GuardrailCategory.prompt_injection,
            reason="explicit attack",
        )
    )
    outcome = screen(pipeline_for(layers))
    assert outcome.decision is GuardrailDecision.block
    assert outcome.category is GuardrailCategory.prompt_injection
    assert outcome.reason == "explicit attack"
    assert len(layers[0].calls) == 1
    assert layers[1].calls == [] and layers[2].calls == []
    # Not consulted is not the same as skipped or closed, and the outcome claims neither.
    assert outcome.skipped == ()
    assert outcome.unavailable == ()
    assert outcome.complete is True
    assert [verdict_.layer for verdict_ in outcome.verdicts] == [REGEX]


def test_a_block_at_layer_two_stops_the_call_after_it() -> None:
    layers = spy_layers(
        decision_model=verdict(
            DECISION_MODEL,
            GuardrailDecision.block,
            GuardrailCategory.medical_advice_request,
            reason="asks for advice",
        )
    )
    outcome = screen(pipeline_for(layers))
    assert outcome.decision is GuardrailDecision.block
    assert layers[2].calls == []
    assert [verdict_.layer for verdict_ in outcome.verdicts] == [REGEX, DECISION_MODEL]


def test_an_allow_never_overrides_a_verdict_a_layer_already_made() -> None:
    layers = spy_layers(
        regex=verdict(
            REGEX, GuardrailDecision.clarify, GuardrailCategory.off_topic, reason="layer one asked"
        ),
    )
    outcome = screen(pipeline_for(layers))
    assert outcome.decision is GuardrailDecision.clarify
    assert outcome.reason == "layer one asked"
    assert outcome.category is GuardrailCategory.off_topic
    # The later layers did run — an allow is an answer, not a reason to stop asking.
    assert [len(layer.calls) for layer in layers] == [1, 1, 1]


def test_the_first_question_decides_and_a_later_block_replaces_it() -> None:
    """Two clarifies: the first stands. A clarify then a block: the refusal wins."""
    first_question = spy_layers(
        regex=verdict(REGEX, GuardrailDecision.clarify, GuardrailCategory.off_topic, reason="one"),
        decision_model=verdict(
            DECISION_MODEL,
            GuardrailDecision.clarify,
            GuardrailCategory.pii_leak_attempt,
            reason="two",
        ),
    )
    outcome = screen(pipeline_for(first_question))
    assert outcome.decision is GuardrailDecision.clarify
    assert outcome.reason == "one"
    assert outcome.category is GuardrailCategory.off_topic

    later_block = spy_layers(
        regex=verdict(REGEX, GuardrailDecision.clarify, GuardrailCategory.off_topic, reason="one"),
        decision_model=verdict(
            DECISION_MODEL, GuardrailDecision.block, GuardrailCategory.abuse, reason="two"
        ),
    )
    outcome = screen(pipeline_for(later_block))
    assert outcome.decision is GuardrailDecision.block
    assert outcome.reason == "two"
    assert outcome.category is GuardrailCategory.abuse


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ((GuardrailDecision.allow, GuardrailDecision.allow, GuardrailDecision.allow), REGEX),
        (
            (GuardrailDecision.allow, GuardrailDecision.clarify, GuardrailDecision.allow),
            DECISION_MODEL,
        ),
        (
            (GuardrailDecision.allow, GuardrailDecision.allow, GuardrailDecision.clarify),
            LOCAL_CLASSIFIER,
        ),
        (
            (GuardrailDecision.clarify, GuardrailDecision.block, GuardrailDecision.allow),
            DECISION_MODEL,
        ),
    ],
)
def test_the_outcome_quotes_the_deciding_verdict_and_not_any_other(
    row: tuple[GuardrailDecision, GuardrailDecision, GuardrailDecision], expected: GuardrailLayer
) -> None:
    """A reason copied from a layer that answered ``allow`` would send an operator to the wrong layer.

    The first row is the one that says the layer names in the reason are not decoration: with three
    allows nobody decided, and the outcome's own reason says so while each verdict keeps its own.
    """
    layers = spy_layers(
        regex=verdict(REGEX, row[0], reason=REASONS[REGEX]),
        decision_model=verdict(DECISION_MODEL, row[1], reason=REASONS[DECISION_MODEL]),
        local_classifier=verdict(LOCAL_CLASSIFIER, row[2], reason=REASONS[LOCAL_CLASSIFIER]),
    )
    outcome = screen(pipeline_for(layers))
    if all(decision is GuardrailDecision.allow for decision in row):
        assert outcome.reason == "no layer produced a verdict"
        assert outcome.decision is GuardrailDecision.allow
    else:
        assert outcome.reason == REASONS[expected]
        assert any(
            verdict_.reason == outcome.reason and verdict_.layer is expected
            for verdict_ in outcome.verdicts
        )
    # Every verdict keeps its own answer, and the recorded verdicts stop at the block: the layers
    # after a refusal were not consulted, so they are not in the list either.
    assert [verdict_.decision for verdict_ in outcome.verdicts] == _recorded(row)


def _recorded(row: Sequence[GuardrailDecision]) -> list[GuardrailDecision]:
    """The row up to and including the first ``block`` — what a short-circuiting pipeline records."""
    for index, decision in enumerate(row):
        if decision is GuardrailDecision.block:
            return list(row[: index + 1])
    return list(row)


def test_a_block_short_circuits_before_it_can_examine_a_closed_layer() -> None:
    """The short-circuit's own reading of "complete", stated rather than left to be discovered.

    A ``block`` at layer 1 ends the loop, so the layers after it are never *examined* — including a
    layer that has nothing behind it. The outcome is therefore ``complete`` in the sense this package
    means it (no layer answered "I have nothing behind me"), and that is safe for exactly one reason:
    the decision is a ``block``, so nothing is authorised on the strength of the layer nobody looked
    at. The closure itself is still a fact about the deployment, and ``LayerSet.closed()`` is where a
    caller asks for it. The conditional profile's *skip* path, which does examine each layer, is
    asserted separately below.
    """
    layers = spy_layers(
        regex=verdict(REGEX, GuardrailDecision.block, GuardrailCategory.abuse, reason="refused"),
        closed=(LOCAL_CLASSIFIER,),
    )
    outcome = screen(pipeline_for(layers))
    assert outcome.decision is GuardrailDecision.block
    assert outcome.unavailable == ()
    assert outcome.missing_layers == ()
    assert outcome.complete is True
    assert layers[2].calls == []


def test_every_verdict_carries_the_latency_its_layer_took() -> None:
    """§9 measures added latency per layer; a latency nobody attributed to a layer is a total."""
    outcome = screen(pipeline_for(spy_layers()))
    # TickClock advances one millisecond per reading and the pipeline reads twice around each layer,
    # so this is arithmetic rather than a race: three layers cost one millisecond each, and the whole
    # screening costs the seven readings it took.
    assert [verdict_.latency_ms for verdict_ in outcome.verdicts] == [1, 1, 1]
    assert outcome.latency_ms == 7
    assert isinstance(SystemClock().monotonic_ms(), float)


def test_masking_happens_once_and_every_layer_sees_the_masked_message() -> None:
    """§9 hands the decision model a masked message; the pipeline is where that is decided."""
    layers = spy_layers()
    calls: list[str] = []

    def counting_masker(text: str) -> str:
        calls.append(text)
        return mask_pii(text)

    pipeline = pipeline_for(layers)
    pipeline.masker = counting_masker
    outcome = pipeline.screen(SENSITIVE, workflow_state="INTAKE")

    assert calls == [SENSITIVE], "the message is masked once, not once per layer"
    masked = mask_pii(SENSITIVE)
    for layer in layers:
        assert layer.calls[0][1] == masked, f"{layer.name.value} was handed the unmasked message"
        # The layer still sees the *request*: layer 1 reads `request.text`, and the masking is a rule
        # about what travels upward rather than a rewrite of what the workflow said.
        assert layer.calls[0][0].text == SENSITIVE
    assert "ali@example.com" not in masked and "2205518" not in masked
    assert outcome.decision is GuardrailDecision.allow


# ---------------------------------------------------------------------------
# The two profiles
# ---------------------------------------------------------------------------


def test_three_stage_never_skips_even_when_a_verdict_is_decisive() -> None:
    layers = spy_layers(
        regex=verdict(
            REGEX,
            GuardrailDecision.block,
            GuardrailCategory.emergency,
            reason="emergency",
            decisive=True,
        )
    )
    outcome = screen(pipeline_for(layers))
    # The block still cuts the calls after it; what is asserted here is that the *profile* recorded no
    # skip — the short-circuit is the pipeline's rule, not the conditional profile's.
    assert outcome.decision is GuardrailDecision.block
    assert outcome.skipped == ()
    assert layers[1].calls == []


def test_the_conditional_profile_skips_the_calls_a_decisive_verdict_makes_unnecessary() -> None:
    layers = spy_layers(
        regex=verdict(
            REGEX,
            GuardrailDecision.allow,
            GuardrailCategory.benign,
            reason="the anchored short-reply pattern matched in full while a question was open",
            decisive=True,
        )
    )
    outcome = screen(pipeline_for(layers, profile="conditional"), "evet")
    assert outcome.decision is GuardrailDecision.allow
    assert outcome.skipped == (DECISION_MODEL, LOCAL_CLASSIFIER)
    assert layers[1].calls == [] and layers[2].calls == []
    # Skipped is not missing: the layers were configured and the screening is complete, so a write
    # that depends on this outcome is not refused.
    assert outcome.complete is True
    outcome = pipeline_for(layers, profile="conditional").require_complete(outcome)
    assert outcome.decision is GuardrailDecision.allow


def test_the_conditional_profile_does_not_skip_on_a_verdict_that_is_not_decisive() -> None:
    """Profanity inside a genuine request is exactly the branch a skip must not swallow (§9).

    Layer 1 answers ``allow`` for it and deliberately does *not* mark it decisive, because the same
    message can carry an injection — the corpus contains that shape — so the conditional profile has
    to run the layers that could tell.
    """
    layers = spy_layers(
        regex=verdict(
            REGEX,
            GuardrailDecision.allow,
            GuardrailCategory.benign,
            reason="profanity, genuine request",
        )
    )
    outcome = screen(
        pipeline_for(layers, profile="conditional"), "bu ne biçim randevu sistemi, randevu ver"
    )
    assert outcome.skipped == ()
    assert [len(layer.calls) for layer in layers] == [1, 1, 1]


def test_a_closed_layer_is_recorded_unavailable_even_by_the_profile_that_skips_it() -> None:
    """A skip is about a *call*, never about a dependency: skipping must not manufacture completeness.

    Layer 1 answered ``allow`` decisively, so the conditional profile would skip both later layers.
    Layer 2 has nothing behind it, and examining it is what separates the skip rule from the closure
    rule: it is recorded as unavailable rather than skipped, the ``allow`` does not survive an
    incomplete screening, and the write is refused with the layer named.
    """
    layers = spy_layers(
        regex=verdict(
            REGEX,
            GuardrailDecision.allow,
            reason="the anchored short-reply pattern matched in full",
            decisive=True,
        ),
        closed=(DECISION_MODEL,),
        closed_reason="the operator provided no client",
    )
    outcome = screen(pipeline_for(layers, profile="conditional"), "evet")
    assert outcome.missing_layers == (DECISION_MODEL,)
    assert outcome.complete is False
    assert outcome.skipped == (LOCAL_CLASSIFIER,)
    assert outcome.decision is GuardrailDecision.clarify, (
        "a closed layer never leaves an allow standing"
    )
    assert layers[1].calls == [], "a closed layer is not called; its closure is enough"
    with pytest.raises(InputPathClosed) as raised:
        pipeline_for(layers, profile="conditional").require_complete(outcome)
    assert "decision_model" in str(raised.value)


def test_an_unavailable_layer_never_leaves_an_allow_standing() -> None:
    """§9: the dependency being closed is a refusal. It is not a block either — nobody decided."""
    layers = spy_layers(closed=(DECISION_MODEL,), closed_reason="the operator provided no client")
    outcome = screen(pipeline_for(layers))
    assert outcome.decision is GuardrailDecision.clarify
    assert outcome.category is GuardrailCategory.off_topic
    assert outcome.response is None
    assert outcome.complete is False
    assert outcome.needs_human is True
    # The reason is the closure's own text, which is the exception's message and names the layer.
    assert outcome.reason.startswith("guardrail layer decision_model is not configured")
    assert "the operator provided no client" in outcome.reason
    assert outcome.missing_layers == (DECISION_MODEL,)
    assert outcome.as_dict()["unavailable_layers"] == ["decision_model"]


def test_the_pipeline_keeps_the_write_refused_with_every_missing_layer_named() -> None:
    pipeline = pipeline_for(spy_layers(closed=(DECISION_MODEL, LOCAL_CLASSIFIER)))
    outcome = screen(pipeline)
    assert outcome.missing_layers == (DECISION_MODEL, LOCAL_CLASSIFIER)
    with pytest.raises(InputPathClosed) as raised:
        pipeline.require_complete(outcome)
    message = str(raised.value)
    assert "decision_model" in message and "local_classifier" in message
    assert "static help card" in message
    assert raised.value.missing == (DECISION_MODEL, LOCAL_CLASSIFIER)
    # A complete outcome passes the same gate, so the refusal is not refusing everything.
    assert pipeline_for(spy_layers()).require_complete(screen(pipeline_for(spy_layers())))


def test_a_layer_that_answers_with_something_that_is_not_a_closure_is_re_raised() -> None:
    """Only a closure closes the path. A genuine bug inside a layer must not read as "the guardrail is off"."""
    layers = list(spy_layers())
    layers[1].raises = ValueError("a bug, not a closure")
    with pytest.raises(ValueError, match="a bug, not a closure"):
        screen(pipeline_for(layers))


def test_configured_and_the_emergency_reading_follow_the_verdicts() -> None:
    assert pipeline_for(spy_layers()).configured is True
    assert pipeline_for(spy_layers(closed=(LOCAL_CLASSIFIER,))).configured is False

    plain_block = spy_layers(
        regex=verdict(REGEX, GuardrailDecision.block, GuardrailCategory.abuse, reason="abuse")
    )
    assert screen(pipeline_for(plain_block)).emergency is False

    emergency = spy_layers(
        regex=verdict(
            REGEX,
            GuardrailDecision.block,
            GuardrailCategory.emergency,
            reason="emergency",
            response="call 112 now",
        )
    )
    outcome = screen(pipeline_for(emergency))
    assert outcome.emergency is True
    assert outcome.response == "call 112 now"
    assert outcome.as_dict()["layers"][0]["reason"] == "emergency"


def test_the_json_form_carries_the_profile_and_the_per_layer_report() -> None:
    outcome = screen(pipeline_for(spy_layers(), profile="conditional"))
    payload = outcome.as_dict()
    assert set(payload) == {
        "decision",
        "category",
        "reason",
        "complete",
        "profile",
        "unavailable_layers",
        "skipped_layers",
        "latency_ms",
        "layers",
    }
    assert payload["profile"] == "conditional"
    assert set(payload["layers"][0]) == {
        "layer",
        "decision",
        "category",
        "reason",
        "latency_ms",
        "method",
        "similarity",
    }


def test_the_pipeline_bounds_the_outcomes_it_keeps_and_keeps_every_verdict() -> None:
    pipeline = pipeline_for(spy_layers())
    pipeline.outcome_limit = 2
    for _ in range(3):
        screen(pipeline)
    assert len(pipeline.outcomes_seen) == 2
    assert len(pipeline.verdicts_seen) == 9


# ---------------------------------------------------------------------------
# The comparison §9 asks for, with its premise stated rather than assumed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row",
    [
        (GuardrailDecision.allow, GuardrailDecision.allow, GuardrailDecision.allow),
        (GuardrailDecision.block, GuardrailDecision.allow, GuardrailDecision.allow),
        (GuardrailDecision.allow, GuardrailDecision.block, GuardrailDecision.allow),
        (GuardrailDecision.clarify, GuardrailDecision.allow, GuardrailDecision.allow),
        (GuardrailDecision.allow, GuardrailDecision.allow, GuardrailDecision.block),
    ],
)
def test_both_profiles_reach_the_same_decision_where_a_skip_cannot_change_the_answer(
    row: tuple[GuardrailDecision, GuardrailDecision, GuardrailDecision],
) -> None:
    """§9 asks for the comparison; this is the half of it that holds by construction.

    A decisive verdict at layer 1 skips the calls after it, and every later scripted verdict here
    agrees with what layer 1 already said — so the two profiles must produce the same decision.
    """
    decisions = []
    for profile in ("three_stage", "conditional"):
        layers = spy_layers(
            regex=verdict(
                REGEX, row[0], reason="regex", decisive=row[0] is not GuardrailDecision.allow
            ),
            decision_model=verdict(DECISION_MODEL, row[1], reason="model"),
            local_classifier=verdict(LOCAL_CLASSIFIER, row[2], reason="classifier"),
        )
        decisions.append(screen(pipeline_for(layers, profile=profile)).decision)
    assert decisions[0] is decisions[1]


def test_the_conditional_profile_s_skip_is_a_premise_and_this_is_what_breaks_it() -> None:
    """The one shape where the profiles disagree, built on purpose.

    Layer 1 says the message is fine and says it *decisively* (a short reply the anchored pattern
    matched in full), while layer 3 would have blocked it. The conditional profile never asks, so it
    allows what the three-stage profile refuses. That is not a bug in the skip — the verdict layer 1
    produced is the one §9 makes decisive — it is the premise the profile rests on, and it is written
    here so the suite cannot claim a general equivalence nobody checked. The corpus measurement
    (``profile_comparison`` in ``test_guardrail_measurement.py``) is where the same comparison is run
    on real messages and *reported* rather than asserted.
    """
    full: dict[str, GuardrailOutcome] = {}
    for profile in ("three_stage", "conditional"):
        layers = spy_layers(
            regex=verdict(
                REGEX, GuardrailDecision.allow, reason="short reply matched in full", decisive=True
            ),
            local_classifier=verdict(
                LOCAL_CLASSIFIER,
                GuardrailDecision.block,
                GuardrailCategory.prompt_injection,
                reason="layer three would refuse",
            ),
        )
        outcome = screen(pipeline_for(layers, profile=profile), "evet")
        assert (layers[2].calls == []) is (profile == "conditional")
        full[profile] = outcome

    assert full["three_stage"].decision is GuardrailDecision.block
    assert full["conditional"].decision is GuardrailDecision.allow
    assert full["conditional"].skipped == (DECISION_MODEL, LOCAL_CLASSIFIER)


# ---------------------------------------------------------------------------
# The per-layer report
# ---------------------------------------------------------------------------


def test_the_layer_report_counts_only_the_layers_that_produced_a_verdict() -> None:
    """A layer that did not run is **absent**, not present with a zero: a zero latency reads as instant."""
    pipeline = pipeline_for(
        spy_layers(
            regex=verdict(REGEX, reason="regex allow", method="regex-v1"),
            decision_model=verdict(
                DECISION_MODEL, GuardrailDecision.block, reason="model block", method="openrouter:x"
            ),
        )
    )
    outcome = screen(pipeline)
    report = layer_report([outcome])
    assert report["invocations"] == 1
    assert report["synthetic"] is True
    assert report["incomplete"] == 0
    by_layer = report["by_layer"]
    assert set(by_layer) == {"regex", "decision_model"}, (
        "layer 3 never ran and must not be in the report"
    )
    assert by_layer["regex"]["verdicts"] == 1
    assert by_layer["regex"]["methods"] == ["regex-v1"]
    assert by_layer["decision_model"]["methods"] == ["openrouter:x"]
    assert by_layer["regex"]["mean_latency_ms"] == 1
    assert by_layer["regex"]["max_latency_ms"] == 1

    # A layer that ran and answered with no method contributes no method rather than an empty string.
    unsigned = layer_report(
        [screen(pipeline_for(spy_layers(regex=verdict(REGEX, reason="x", method=""))))]
    )
    assert unsigned["by_layer"]["regex"]["methods"] == []


def test_the_layer_report_counts_an_incomplete_screening_as_incomplete() -> None:
    pipeline = pipeline_for(spy_layers(closed=(DECISION_MODEL,)))
    report = layer_report([screen(pipeline)])
    assert report["incomplete"] == 1
    assert set(report["by_layer"]) == {"regex", "local_classifier"}
    assert layer_report([])["invocations"] == 0
    assert layer_report([])["by_layer"] == {}


def test_the_measured_keys_are_the_four_numbers_step_9_names_plus_the_call_and_the_cost() -> None:
    """Kept as data so a report that dropped one of §9's numbers would be visibly short."""
    assert MEASURED_PER_LAYER == (
        "false_positives",
        "false_negatives",
        "mean_latency_ms",
        "provider_calls",
        "tokens",
        "cost_usd",
    )
