# ruff: noqa: RUF001, RUF002  # §9's Turkish sentences are quoted verbatim
"""§9's closed dependency: the write is refused, the help card stays, the emergency still routes (§9).

§9 puts two things in one sentence — *"Guardrail dependency kapalıyken booking yazılamaz"* — and a
static guide card that stays available. This file is where both halves are asserted as behaviour:

* **The refusal names the layers.** ``LayerSet.closed()`` is the single fact `/healthz`, the refusal
  message and the measurement report all read; ``require_complete`` is the write path's gate, and its
  message names what is missing rather than saying "the guardrail is off".
* **The card is a card.** It is a §12 ``ComponentInstance`` the contract accepts, it carries no
  action, no booking state, no entity id and no time — nothing on it can be read as a claim about a
  record — and it passes the output guardrail, because a help card a patient sees has to.
* **The emergency route survives the closure.** An emergency phrase is not silently blocked as
  off-topic (§9): layer 1 answers it with the approved fixed response even when every layer behind it
  has nothing behind it. The write is still refused, and the response still reaches the patient —
  those are two different things and the test asserts both.

The cards are checked with the *real* output guardrail and a stub oracle, not with a shape assertion:
"it looks like a card" and "it is a card that may be shown" are different claims.
"""

from __future__ import annotations

import uuid

import pytest
from guardrail_fixtures import (
    ScriptedDecisionModelClient,
    StubOracle,
    case,
    json_reply,
)

from referraleasy_contracts.generated.common import GuardrailCategory, GuardrailDecision
from referraleasy_contracts.generated.sse import ComponentInstance, RenderableComponent
from referraleasy_guardrails.build import (
    CLOSED_STATE_COMPONENT,
    LayerSet,
    build_layers,
    closed_state_card,
    layer_names,
)
from referraleasy_guardrails.config import GuardrailSettings
from referraleasy_guardrails.emergency import APPROVED_EMERGENCY_RESPONSE
from referraleasy_guardrails.layer_classifier import OFFLINE_METHOD_PIN, HashedReferenceEncoder
from referraleasy_guardrails.output import ENTITY_ID_FIELDS, OutputGuardrail
from referraleasy_guardrails.pipeline import InputPathClosed
from referraleasy_guardrails.types import LAYER_ORDER

#: What the output guardrail reports it looked at when it runs every check.
ALL_CHECKS = (
    "schema",
    "entity_ids",
    "times",
    "sourced_contacts",
    "booking_state",
    "coverage",
    "clinical_language",
)


def online_settings() -> GuardrailSettings:
    """A deployment that names a model, the offline rung, and agrees with the recorded threshold.

    The model id is named as well as injected, because a layer is *configured* only when it has both
    a client and a model to attribute its verdicts to — a client with no model id is one more way to
    be closed, which is what the next test says.
    """
    return GuardrailSettings(
        decision_model="openai/gpt-5-nano",
        local_model=OFFLINE_METHOD_PIN,
        local_threshold="0.32",
    )


def configured_layers() -> LayerSet:
    return build_layers(
        online_settings(),
        client=ScriptedDecisionModelClient(replies=[json_reply()]),
        encoder=HashedReferenceEncoder(),
    )


def closed_layers() -> LayerSet:
    """Nothing behind layers 2 and 3: the offline profile with no rung named at all."""
    return build_layers(GuardrailSettings())


# ---------------------------------------------------------------------------
# What "closed" says, and what it offers instead
# ---------------------------------------------------------------------------


def test_a_layer_set_with_everything_behind_it_offers_no_help_card() -> None:
    """A help card shown to every patient would be a UI change dressed as a safety measure."""
    layers = configured_layers()
    assert layers.all_configured() is True
    assert layers.closed() == {}
    assert layers.closed_card() is None
    assert layers.configured() == tuple(layer.name for layer in layers.ordered)
    assert layers.screens() != (None, None), "a configured layer contributes its screen callable"
    assert all(callable(screen) for screen in layers.screens())


def test_a_closed_dependency_names_each_missing_layer_and_its_reason() -> None:
    layers = closed_layers()
    closed = layers.closed()
    assert set(closed) == {"decision_model", "local_classifier"}, (
        "layer 1 has no dependency and is never closed"
    )
    assert all(reason.strip() for reason in closed.values())
    assert "offline" in closed["decision_model"]
    assert "GUARDRAIL_LOCAL_MODEL" in closed["local_classifier"]
    assert layers.all_configured() is False
    assert layers.screens() == (None, None), (
        "a closed layer contributes no callable, so the wrapper that owns the refusal decides"
    )


def test_a_client_without_a_model_id_is_still_a_closed_layer() -> None:
    """A verdict nobody can attribute is not a verdict: the model id is part of being configured."""
    layers = build_layers(
        GuardrailSettings(local_model=OFFLINE_METHOD_PIN, local_threshold="0.32"),
        client=ScriptedDecisionModelClient(replies=[json_reply()]),
        encoder=HashedReferenceEncoder(),
    )
    closed = layers.closed()
    assert set(closed) == {"decision_model"}
    assert "model" in closed["decision_model"]
    assert layers.closed_card() is not None


def test_the_help_card_is_a_contract_component_carrying_no_action_and_no_claim() -> None:
    card = closed_layers().closed_card()
    assert card is not None
    assert card["component"] == CLOSED_STATE_COMPONENT == RenderableComponent.HumanHandoffCard.value
    assert card["action"] is None
    assert card["workflow_state_version"] == 0, "the card is not built from a workflow state"
    model = ComponentInstance.model_validate(card)
    assert model.action is None and model.disabled is None and model.submitted is None
    assert model.notice and "No screening ran" in model.notice

    props = card["props"]
    assert set(props) == {"static", "unavailable_layers", "reasons", "reason"}, (
        "the props are a fixed set: nothing here can become a claim about a record by accident"
    )
    assert props["static"] is True
    assert props["unavailable_layers"] == sorted(closed_layers().closed())
    assert props["reasons"] == closed_layers().closed()
    assert "cannot book anything" in props["reason"]
    assert "human coordinator" in props["reason"]
    # No entity id, no time, no contact, no booking state — the four things the output guardrail
    # exists to check are not on this card, which is why it can be shown while screening is closed.
    assert set(props) & ENTITY_ID_FIELDS == set()


def test_the_help_card_passes_the_output_guardrail() -> None:
    """It is shown to a patient, so it is a card like any other — checked by the real guardrail."""
    card = closed_layers().closed_card()
    assert card is not None
    result = OutputGuardrail(StubOracle()).check(card, sources=())
    assert result.ok is True, [violation.as_dict() for violation in result.violations]
    assert result.checked == ALL_CHECKS
    assert result.violations == ()


def test_the_help_card_s_identity_is_a_function_of_which_layers_are_missing() -> None:
    """Re-rendering the same closed state replaces the same slot rather than stacking a second card."""
    first = closed_layers().closed_card()
    second = closed_layers().closed_card()
    assert first is not None and second is not None
    assert first["instance_id"] == second["instance_id"]
    uuid.UUID(str(first["instance_id"]))

    only_layer_two = closed_state_card({"decision_model": "no client"})
    both = closed_state_card({"decision_model": "no client", "local_classifier": "no artifact"})
    assert only_layer_two["instance_id"] != both["instance_id"]
    assert only_layer_two["instance_id"] != first["instance_id"]


def test_even_the_degenerate_closure_produces_a_card_rather_than_an_empty_one() -> None:
    card = closed_state_card({})
    model = ComponentInstance.model_validate(card)
    assert model.props["unavailable_layers"] == []
    assert "no layers configured" in model.props["reason"]
    assert OutputGuardrail(StubOracle()).check(card).ok is True


def test_the_layer_names_are_step_9_s_order() -> None:
    assert layer_names() == ("regex", "decision_model", "local_classifier")
    assert tuple(layer.value for layer in LAYER_ORDER) == layer_names()


# ---------------------------------------------------------------------------
# The refusal, and the two things that keep working through it
# ---------------------------------------------------------------------------


def test_a_closed_dependency_refuses_the_write_and_the_card_still_serves() -> None:
    """§9's sentence, as one test: the booking write is refused, the static card is available."""
    layers = closed_layers()
    pipeline = layers.pipeline()
    outcome = pipeline.screen(
        "merhaba, yarın için bir randevu almak istiyorum", workflow_state="INTAKE"
    )

    assert outcome.complete is False
    assert outcome.decision is GuardrailDecision.clarify, "a closed layer never leaves an allow"
    with pytest.raises(InputPathClosed) as raised:
        pipeline.require_complete(outcome)
    message = str(raised.value)
    assert "decision_model" in message and "local_classifier" in message
    assert "static help card is still available" in message
    assert raised.value.missing == (LAYER_ORDER[1], LAYER_ORDER[2])

    card = layers.closed_card()
    assert card is not None
    assert card["props"]["unavailable_layers"] == ["decision_model", "local_classifier"]


def test_the_emergency_response_still_reaches_a_patient_while_the_dependency_is_closed() -> None:
    """§9: an emergency phrase is not silently blocked as off-topic, whatever is closed behind it.

    The refusal this message gets is the *decision's*, not the closure's: layer 1 answered and the
    pipeline stopped there, so the layers behind it were never consulted and the outcome does not
    claim a closure it did not examine. What the closure means for this deployment is still on the
    layer set — ``closed()`` and ``closed_card()`` — and it is asserted here so the two readings
    cannot be confused.
    """
    emergency = case("emergency-chest-pain-en")
    layers = closed_layers()
    pipeline = layers.pipeline()
    outcome = pipeline.screen(
        emergency.text,
        workflow_state=emergency.workflow_state,
        short_reply_expected=emergency.short_reply_expected,
    )

    assert outcome.decision is GuardrailDecision.block
    assert outcome.category is GuardrailCategory.emergency
    assert outcome.response == APPROVED_EMERGENCY_RESPONSE
    assert outcome.emergency is True
    assert "112" in APPROVED_EMERGENCY_RESPONSE and "cannot" in APPROVED_EMERGENCY_RESPONSE
    # A blocked message never becomes a booking: `blocks` is the write path's other gate, and the
    # short-circuit means this outcome carries no closure to report.
    assert pipeline.require_complete(outcome).blocks is True
    assert outcome.unavailable == ()
    # The deployment's own state is unchanged by the case: the dependency is still closed.
    assert set(layers.closed()) == {"decision_model", "local_classifier"}
    assert layers.closed_card() is not None


def test_a_configured_layer_set_lets_the_same_message_through_to_a_write() -> None:
    """The other side of the gate: with everything behind the layers, the write is authorised."""
    layers = configured_layers()
    pipeline = layers.pipeline()
    outcome = pipeline.screen(
        "merhaba, yarın için bir randevu almak istiyorum", workflow_state="INTAKE"
    )
    assert outcome.complete is True
    assert pipeline.require_complete(outcome) is outcome
    assert layers.closed_card() is None
