# ruff: noqa: RUF002  # §9's Turkish sentences are quoted verbatim
"""Assembling §9's three layers from the environment — the one function the wiring calls (§9).

Everything in this package can be built by hand, and the tests mostly do. This module is the other
path: the one the gateway's wiring uses, where a single call turns the environment into the three
layers, each either *configured* or *closed with a reason*.

Two rules make it worth having rather than inlining:

* **Nothing is substituted silently.** Layer 3 runs the pinned encoder or the rung an operator named
  (``GUARDRAIL_LOCAL_MODEL=offline/hashed-char-ngram-v1``) and nothing else; if the artifact is
  absent, the runtime is missing, or the hash does not match, the layer is closed. A report that said
  "layer 3 ran" while a weaker matcher stood in for the encoder would be worse than a closed layer.
* **Closed means closed.** :meth:`LayerSet.closed` says which layers have nothing behind them and
  why, so `/healthz`, the refusal message and the measurement report all read the same fact rather
  than three approximations of it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from referraleasy_contracts.generated.common import GuardrailLayer
from referraleasy_guardrails.config import GuardrailProfile, GuardrailSettings
from referraleasy_guardrails.corpus import ArtifactPin, LabelledCorpus, ReferenceSet
from referraleasy_guardrails.layer_classifier import (
    OFFLINE_METHOD_PIN,
    Encoder,
    EncoderUnavailable,
    HashedReferenceEncoder,
    LocalClassifierGuardrail,
    OnnxEncoder,
    ThresholdRecord,
    load_threshold_record,
)
from referraleasy_guardrails.layer_decision_model import (
    DecisionModelClient,
    DecisionModelGuardrail,
    OpenRouterDecisionModelClient,
)
from referraleasy_guardrails.layer_regex import RegexGuardrail
from referraleasy_guardrails.pipeline import Clock, GuardrailPipeline, SystemClock
from referraleasy_guardrails.types import GuardrailLayerCheck, LayerVerdict, ScreeningRequest

__all__ = [
    "CLOSED_STATE_COMPONENT",
    "OFFLINE_LAYER_2_REASON",
    "LayerSet",
    "build_layers",
    "closed_state_card",
    "layer_names",
]

#: The reason layer 2 gives when the offline profile is being run deliberately. Named here so the
#: evaluation report and the refusal message cannot drift apart.
OFFLINE_LAYER_2_REASON = (
    "MODEL_BACKEND=offline: the offline profile is defined as zero network calls, so layer 2 makes "
    "none and has no verdict to give. This is the closed state (§9), not an allow."
)

#: A §12 component, by the name the contract gives it. The closed state offers a handoff card rather
#: than a booking form, because there is no screening behind a booking and §9 refuses the write.
CLOSED_STATE_COMPONENT: Final = "HumanHandoffCard"

#: The fixed uuid5 namespace for closed-state cards: the card's instance id is a function of *which*
#: layers are missing, so re-rendering the same closed state replaces the same slot instead of
#: stacking a second identical card on the surface (§12).
_CARD_NAMESPACE: Final = uuid.UUID("6f3d1a9c-2e5b-4f7a-8c1d-0b2a3c4d5e6f")


def closed_state_card(closed: Mapping[str, str]) -> Mapping[str, object]:
    """The static card §9 keeps available when the guardrail dependency is closed.

    §9 puts two things in one sentence: *"Guardrail dependency kapalıyken booking yazılamaz"* and a
    static guide card that stays available. The write refusal is
    :meth:`referraleasy_guardrails.pipeline.GuardrailPipeline.require_complete`; this is the other
    half, and it has to be *well defined* rather than an intention — so it is a concrete §12 payload,
    it names the missing layers, it carries no action (nothing on it can book), and its instance id
    is a pure function of what is missing.
    """
    names = sorted(closed)
    return {
        "instance_id": str(
            uuid.uuid5(
                _CARD_NAMESPACE, "closed-state:" + ",".join(names) if names else "closed-state"
            )
        ),
        "component": CLOSED_STATE_COMPONENT,
        "props": {
            "static": True,
            "unavailable_layers": names,
            "reasons": {name: closed[name] for name in names},
            "reason": (
                "input screening is not available in this deployment ("
                + (", ".join(names) if names else "no layers configured")
                + "), so this assistant cannot book anything. A human coordinator can take the "
                "referral from here."
            ),
        },
        # Zero rather than a workflow version: this card is not built from a workflow state, and
        # claiming a version it did not read would let a stale client action look current.
        "workflow_state_version": 0,
        # No `action`, and `disabled` is not used to hide one: there is no control on this card.
        "action": None,
        "notice": "A static help card. No screening ran, so nothing here is authorised.",
    }


@dataclass(frozen=True, slots=True)
class LayerSet:
    """§9's three layers, in order, with the means to run them under either profile."""

    regex: RegexGuardrail
    decision_model: DecisionModelGuardrail
    local_classifier: LocalClassifierGuardrail
    settings: GuardrailSettings = field(default_factory=GuardrailSettings)

    @property
    def ordered(self) -> tuple[GuardrailLayerCheck, ...]:
        return (self.regex, self.decision_model, self.local_classifier)

    def pipeline(
        self, *, clock: Clock | None = None, profile: GuardrailProfile | None = None
    ) -> GuardrailPipeline:
        return GuardrailPipeline(
            layers=self.ordered,
            clock=clock or SystemClock(),
            profile=profile or self.settings.profile,
        )

    def configured(self) -> tuple[GuardrailLayer, ...]:
        return tuple(layer.name for layer in self.ordered if layer.is_configured())

    def closed(self) -> dict[str, str]:
        """Which layers have nothing behind them, and the reason each one gives."""
        closed: dict[str, str] = {}
        if not self.decision_model.is_configured():
            closed[self.decision_model.name.value] = self.decision_model.reason_if_unconfigured
        if not self.local_classifier.is_configured():
            closed[self.local_classifier.name.value] = self.local_classifier.reason_if_unconfigured
        return closed

    def all_configured(self) -> bool:
        return all(layer.is_configured() for layer in self.ordered)

    def closed_card(self) -> Mapping[str, object] | None:
        """The static card when something is closed, and ``None`` when nothing is.

        ``None`` rather than a card in the configured case, because a help card offered to every
        patient would be a UI change dressed as a safety measure. The two facts travel together:
        whether a write is refused (`closed()`) and what is offered instead (this).
        """
        closed = self.closed()
        return closed_state_card(closed) if closed else None

    def screens(self) -> tuple[Callable[[ScreeningRequest, str], LayerVerdict] | None, ...]:
        """The two model-backed layers as bare callables, or None when they are closed.

        This is the shape the gateway's seam takes: its ``DecisionModelGuardrail`` and
        ``LocalClassifierGuardrail`` accept a ``screen`` callable and treat ``None`` as "not
        configured", which is exactly the state this package reports. Passing ``None`` rather than a
        callable that would raise keeps §9's refusal in the wrapper that owns the 503.
        """
        return (
            self.decision_model.screen if self.decision_model.is_configured() else None,
            self.local_classifier.screen if self.local_classifier.is_configured() else None,
        )


def build_layers(
    settings: GuardrailSettings | None = None,
    *,
    client: DecisionModelClient | None = None,
    encoder: Encoder | None = None,
    references: ReferenceSet | None = None,
    record: ThresholdRecord | None = None,
    corpus: Any | None = None,
    artifact_dir: Path | None = None,
) -> LayerSet:
    """Build the three layers. Injected collaborators win; the environment fills the rest.

    ``client`` and ``encoder`` are the seam the tests and the evaluation CLI use: layer 2 can be
    exercised with a scripted client and layer 3 with a deterministic encoder, without either of them
    reaching a provider or needing a 23 MB artifact, and without the layer's own rules changing.

    ``corpus`` defaults to the **committed** corpus rather than to nothing, and that default is
    load-bearing: the threshold record was measured against a corpus revision, so a layer that skipped
    the comparison would apply a threshold nobody measured. Passing ``None`` through to
    ``ThresholdRecord.mismatch`` is an ``AttributeError`` in the middle of startup — found by building
    this function the way the wiring calls it, with the offline rung named and no corpus in hand.
    """
    resolved = settings or GuardrailSettings.from_env()
    resolved_references = references or ReferenceSet.load()
    resolved_record = record if record is not None else load_threshold_record()
    resolved_corpus = corpus if corpus is not None else LabelledCorpus.load()

    decision_layer = DecisionModelGuardrail(
        resolved.decision_model,
        client=_decision_client(resolved, client),
        timeout_ms=resolved.total_timeout_ms,
        reason_if_unconfigured=_decision_model_reason(resolved, client),
    )
    classifier_layer = _classifier_layer(
        resolved,
        encoder=encoder,
        references=resolved_references,
        record=resolved_record,
        corpus=resolved_corpus,
        artifact_dir=artifact_dir,
    )
    return LayerSet(
        regex=RegexGuardrail(max_chars=resolved.max_message_chars),
        decision_model=decision_layer,
        local_classifier=classifier_layer,
        settings=resolved,
    )


def _decision_client(
    settings: GuardrailSettings, injected: DecisionModelClient | None
) -> DecisionModelClient | None:
    if injected is not None:
        return injected
    if not settings.live_model_backend or not settings.decision_model:
        return None
    if not settings.openrouter_api_key:
        return None
    return OpenRouterDecisionModelClient(
        settings.openrouter_api_key, base_url=settings.openrouter_base_url
    )


def _decision_model_reason(
    settings: GuardrailSettings, injected: DecisionModelClient | None
) -> str:
    if injected is not None:
        return "no decision model is injected and none is configured"
    if not settings.live_model_backend:
        return OFFLINE_LAYER_2_REASON
    if not settings.decision_model:
        return (
            "GUARDRAIL_DECISION_MODEL is empty: §9 layer 2 needs the small decision model chosen "
            "explicitly in the environment, and an unnamed model is not a resolved one"
        )
    if not settings.openrouter_api_key:
        return "OPENROUTER_API_KEY is empty, so the OpenRouter call cannot be made (ADR-0002)"
    return "the decision-model layer is not configured"


def _classifier_layer(
    settings: GuardrailSettings,
    *,
    encoder: Encoder | None,
    references: ReferenceSet,
    record: ThresholdRecord | None,
    corpus: Any | None,
    artifact_dir: Path | None,
) -> LocalClassifierGuardrail:
    """Pick the rung the environment names, or close the layer with the reason it could not.

    The order is: an injected encoder (tests, evaluation), then the offline rung when it is *named*,
    then a pinned artifact, then closed. An artifact pin that is not in the pin table is a refusal
    rather than a download — the pin table is what carries the hash, and a hash nobody recorded is not
    a verified artifact (ADR-0005).
    """
    closed_reason = ""
    selected: Encoder | None = encoder
    if selected is None:
        pin_value = settings.local_model.strip()
        if pin_value == OFFLINE_METHOD_PIN:
            selected = HashedReferenceEncoder()
        elif pin_value:
            pin = ArtifactPin.find(pin_value)
            if pin is None:
                closed_reason = (
                    f"GUARDRAIL_LOCAL_MODEL={pin_value!r} names no pinned artifact. ADR-0005 pins "
                    f"layer 3 by content hash and names the offline rung as {OFFLINE_METHOD_PIN!r}; "
                    "an unpinned value is refused rather than fetched."
                )
            else:
                try:
                    selected = OnnxEncoder.load(pin, artifact_dir or settings.artifact_dir)
                except EncoderUnavailable as error:
                    closed_reason = str(error)
        else:
            closed_reason = (
                "GUARDRAIL_LOCAL_MODEL is empty: §9 layer 3 needs the artifact (or the explicitly "
                f"named offline rung, {OFFLINE_METHOD_PIN!r}) selected in the environment"
            )

    return LocalClassifierGuardrail.build(
        pin=settings.local_model.strip(),
        encoder=selected,
        references=references,
        record=record,
        corpus=corpus,
        env_threshold=settings.local_threshold,
        closed_reason=closed_reason,
    )


def layer_names() -> Sequence[str]:
    """§9's order as names, for a health payload or a log line."""
    return tuple(
        layer.value
        for layer in (
            GuardrailLayer.regex,
            GuardrailLayer.decision_model,
            GuardrailLayer.local_classifier,
        )
    )
