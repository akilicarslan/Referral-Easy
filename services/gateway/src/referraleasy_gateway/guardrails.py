"""The write path's gate: §9's three layers, composed, with the refusal §9 requires.

§9 names three layers and their order — **regex**, then an **OpenRouter decision model** on a masked
message, then a **local small semantic model** — and one rule that is a product rule rather than an
engineering one: *"Booking writes are prohibited while a guardrail dependency is unavailable"*. A
write that needs a screened
input is refused with `503 guardrails_not_configured` while any layer has nothing behind it, and a
static help card that needs no screening is still served. Both halves are asserted in
`tests/gateway/test_guardrails.py`.

**The layers are the package's.** `packages/guardrails` is where §9 is implemented and measured
(ADR-0038): the patterns, the masking, the decision-model client, the encoder and its threshold
record, and the per-layer measurement. This module is the composition — it builds those layers from
the environment, runs them in §9's order, short-circuits, and owns the exception class that closes the
write path.

The duplication that used to live here is gone and is worth recording, because it was deliberate and
paid for rather than accidental: this module carried a *second* layer 1 and a *second* masker, and
`tests/guardrails/test_guardrail_layer1_parity.py` compared the two verdict for verdict over the whole
labelled corpus so the demo could not be showing one implementation while the service served the
other. That test's own docstring said it would be deleted rather than updated when the wiring landed.
It has been. The masker's digit floor moved from nine to seven in the same change: the directory's
member ids are seven digits, and a nine-digit floor sent exactly the identifier the masking exists to
remove — found by the package's own test, not by reading.

Two behaviours in here are §9 rules rather than implementation details, and both were wrong before:

* **An incomplete screening is not an allow.** The decision used to keep the `allow` it started with
  while `complete` was `False`, so a caller reading `decision` alone read *permission* where nothing
  had been screened — harmless only for as long as every caller also called `require_complete`. It now
  becomes `clarify`, which is what it is: nobody decided, so it is a question.
* **The decision, its category, its reason and its response come from the same verdict.** The reason
  used to be copied from the last verdict of *any* kind while the category came from the last
  non-allow one, so a `clarify` could be explained by the reason of a layer that answered `allow` —
  sending an operator to look at the wrong layer.

Masking is still done once, here, rather than by each layer: the decision model is given a masked
message (§9), and a layer that forgot to mask would send the raw text to a provider.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from referraleasy_contracts.generated.common import (
    GuardrailCategory,
    GuardrailDecision,
    GuardrailLayer,
)
from referraleasy_gateway.config import GatewaySettings
from referraleasy_gateway.errors import GuardrailUnavailableError
from referraleasy_gateway.timing import Clock

# The three layers, the masker and the approved emergency text are the **package's**, not copies of
# them. `packages/guardrails` is where §9 is implemented and measured (ADR-0038); this module used to
# carry a second layer 1 and a second masker, and the duplication was paid for by a parity test that
# is deleted with the copies. These are re-exports rather than deletions of the *names* so a caller
# that imported `mask_pii` from here still gets the one implementation — the floor is now 7, which is
# the widening §9 needed: the directory's member ids are seven digits, so the old nine-digit floor
# sent exactly the identifier the masking exists to remove.
from referraleasy_guardrails.build import build_layers
from referraleasy_guardrails.config import GuardrailSettings
from referraleasy_guardrails.emergency import APPROVED_EMERGENCY_RESPONSE
from referraleasy_guardrails.layer_regex import RegexGuardrail
from referraleasy_guardrails.types import (
    LayerVerdict,
    ScreeningRequest,
    is_unavailable,
    mask_pii,
)
from referraleasy_telemetry import Synthetic, TelemetryEmitter

__all__ = [
    "APPROVED_EMERGENCY_RESPONSE",
    "GuardrailLayerCheck",
    "GuardrailOutcome",
    "GuardrailPipeline",
    "GuardrailUnavailable",
    "LayerVerdict",
    "RegexGuardrail",
    "ScreeningRequest",
    "mask_pii",
]

#: The order §9 states. Declared as data so the pipeline can assert it rather than assume it.
LAYER_ORDER: Final[tuple[GuardrailLayer, ...]] = (
    GuardrailLayer.regex,
    GuardrailLayer.decision_model,
    GuardrailLayer.local_classifier,
)


class GuardrailUnavailable(RuntimeError):
    """A layer has nothing behind it. **Not** an allow, and never a fallback to one.

    §9's rule is that the dependency being off closes the write path. A layer that answered "allow"
    because it could not answer anything would be a bypass wearing a layer's name.
    """

    def __init__(self, layer: GuardrailLayer, reason: str) -> None:
        super().__init__(f"guardrail layer {layer.value} is not configured: {reason}")
        self.layer = layer
        self.reason = reason


class GuardrailLayerCheck(Protocol):
    """A layer. `screen` raises :class:`GuardrailUnavailable` when it has nothing behind it."""

    name: GuardrailLayer

    def is_configured(self) -> bool: ...

    def screen(self, request: ScreeningRequest, masked_text: str) -> LayerVerdict: ...


@dataclass(frozen=True, slots=True)
class GuardrailOutcome:
    """The pipeline's answer: a decision, the verdicts behind it, and whether it was complete.

    `unavailable` is the field that matters most. An outcome with anything in it is *not* a decision
    — it is "we could not decide", and the only thing it authorises is showing a static card. The
    `decision` field agrees with that rather than contradicting it: an incomplete screening reports
    `clarify`, never the `allow` it started with (see the module docstring).
    """

    decision: GuardrailDecision
    category: GuardrailCategory
    reason: str
    verdicts: tuple[LayerVerdict, ...] = ()
    unavailable: tuple[GuardrailUnavailable, ...] = ()
    response: str | None = None

    @property
    def complete(self) -> bool:
        """Whether every layer §9 names actually ran."""
        return not self.unavailable

    @property
    def blocks(self) -> bool:
        return self.decision is GuardrailDecision.block

    def as_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "category": self.category.value,
            "reason": self.reason,
            "complete": self.complete,
            "unavailable_layers": [error.layer.value for error in self.unavailable],
            "layers": [
                {
                    "layer": verdict.layer.value,
                    "decision": verdict.decision.value,
                    "category": verdict.category.value,
                    "reason": verdict.reason,
                    "latency_ms": verdict.latency_ms,
                }
                for verdict in self.verdicts
            ],
        }


# ---------------------------------------------------------------------------
# Layers 2 and 3 — the wrappers that own §9's refusal
# ---------------------------------------------------------------------------


class DecisionModelGuardrail:
    """§9 layer 2: the OpenRouter decision model, run on a **masked** message.

    `screen` is injected, and it is the package's layer 2 (`referraleasy_guardrails.build.
    build_layers` supplies it through `LayerSet.screens()`). Keeping a wrapper here rather than
    calling the package's layer directly is deliberate: this class is what reports *unconfigured* as
    an outage the pipeline can collect, so the refusal that leaves a closed layer is raised by the
    class the writing path has an `except` clause for. A different-but-equivalent exception would
    surface as an unhandled `500` instead of §9's `503`.
    """

    name: GuardrailLayer = GuardrailLayer.decision_model

    def __init__(
        self,
        model_id: str,
        *,
        screen: Callable[[ScreeningRequest, str], LayerVerdict] | None = None,
        reason_if_unconfigured: str = (
            "no decision model is wired: §9 layer 2 needs a named model and the OpenRouter client, "
            "and until both are present a write that needs this layer is refused rather than allowed"
        ),
    ) -> None:
        self.model_id = model_id
        self._screen = screen
        self._reason = reason_if_unconfigured

    def is_configured(self) -> bool:
        return self._screen is not None and bool(self.model_id)

    def screen(self, request: ScreeningRequest, masked_text: str) -> LayerVerdict:
        if not self.is_configured():
            raise GuardrailUnavailable(self.name, self._reason)
        assert self._screen is not None  # narrowed by is_configured
        return self._screen(request, masked_text)


class LocalClassifierGuardrail:
    """§9 layer 3: the local small semantic model, with a recorded artifact hash.

    Same shape as layer 2 and the same reason for the wrapper. The package closes this layer when the
    encoder is absent, the runtime is missing, the artifact hash does not match, or the environment
    threshold differs from the recorded one — and its `LayerSet.screens()` returns `None` for a closed
    layer, which lands here as `is_configured() == False` and therefore as §9's refusal.
    """

    name: GuardrailLayer = GuardrailLayer.local_classifier

    def __init__(
        self,
        artifact: str,
        *,
        screen: Callable[[ScreeningRequest, str], LayerVerdict] | None = None,
        reason_if_unconfigured: str = (
            "no local classifier is wired: §9 layer 3 needs the pinned encoder (or the explicitly "
            "named offline rung) and a threshold that matches its recorded one"
        ),
    ) -> None:
        self.artifact = artifact
        self._screen = screen
        self._reason = reason_if_unconfigured

    def is_configured(self) -> bool:
        return self._screen is not None and bool(self.artifact)

    def screen(self, request: ScreeningRequest, masked_text: str) -> LayerVerdict:
        if not self.is_configured():
            raise GuardrailUnavailable(self.name, self._reason)
        assert self._screen is not None  # narrowed by is_configured
        return self._screen(request, masked_text)


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


@dataclass
class GuardrailPipeline:
    """The three layers in §9's order, with the short-circuit and the completeness rule.

    An explicit `block` stops the remaining calls ("an explicit block stops the remaining calls"),
    so a message
    layer 1 has already refused never reaches a provider. Everything else continues, and the first
    non-allow verdict wins — a later block replaces an earlier clarify, because a block is strictly
    stronger than a question, and the pipeline does not average the layers' opinions into a
    compromise nobody designed.
    """

    layers: tuple[GuardrailLayerCheck, ...]
    clock: Clock
    masker: Callable[[str], str] = mask_pii
    #: Every verdict this pipeline has produced, for the per-layer measurement §9 asks for. A list
    #: rather than counters because `layer_report` needs the latencies, not just the counts.
    verdicts_seen: list[LayerVerdict] = field(default_factory=list)
    #: The outcomes themselves, bounded. `/v1/admin/metrics` reads *these* rather than a summary a
    #: caller could fabricate, which is the difference between a measurement and a chart.
    outcomes_seen: list[GuardrailOutcome] = field(default_factory=list)
    outcome_limit: int = 500
    #: §15's guardrail decisions, written where the decision is made. `None` is supported and means
    #: the unit suite's app: it must not reach a collector, and it must not need a branch at every
    #: call site either.
    emitter: TelemetryEmitter | None = None

    def __post_init__(self) -> None:
        declared = tuple(layer.name for layer in self.layers)
        if declared != LAYER_ORDER:
            raise ValueError(
                f"the guardrail layers must run in §9's order {LAYER_ORDER}, got {declared}"
            )

    @classmethod
    def from_settings(
        cls,
        settings: GatewaySettings,
        clock: Clock,
        *,
        emitter: TelemetryEmitter | None = None,
    ) -> GuardrailPipeline:
        """Build the pipeline the environment actually describes.

        The package does the assembling (`build_layers`), which is what makes the layers *configured*:
        its `LayerSet.screens()` hands back the package's layer-2 and layer-3 `screen` callables, or
        `None` for a layer it could not build. `None` is not a degradation here — it is §9's closed
        state, and it is what leaves the write path refused with a `503`.

        A layer the package closes carries the package's own reason, which is more specific than this
        module could invent: "GUARDRAIL_LOCAL_MODEL is empty", or "MODEL_BACKEND=offline: the offline
        profile is defined as zero network calls". Those reasons travel to the operator unchanged.
        """
        layers = build_layers(GuardrailSettings.from_env())
        decision_screen, classifier_screen = layers.screens()
        closed = layers.closed()
        return cls(
            layers=(
                layers.regex,
                DecisionModelGuardrail(
                    settings.guardrail_decision_model,
                    screen=decision_screen,
                    reason_if_unconfigured=cls._closed_reason(
                        closed, GuardrailLayer.decision_model
                    ),
                ),
                LocalClassifierGuardrail(
                    settings.guardrail_local_model,
                    screen=classifier_screen,
                    reason_if_unconfigured=cls._closed_reason(
                        closed, GuardrailLayer.local_classifier
                    ),
                ),
            ),
            clock=clock,
            masker=mask_pii,
            emitter=emitter,
        )

    @staticmethod
    def _closed_reason(closed: Mapping[str, str], layer: GuardrailLayer) -> str:
        """The package's reason for a closed layer, falling back to a statement of the fact."""
        return closed.get(layer.value, f"layer {layer.value} is not configured")

    @property
    def configured(self) -> bool:
        """Whether every layer has something behind it *right now*, in this process."""
        return all(layer.is_configured() for layer in self.layers)

    def screen(
        self,
        text: str,
        *,
        workflow_state: str,
        short_reply_expected: bool = False,
        synthetic: bool = True,
    ) -> GuardrailOutcome:
        request = ScreeningRequest(
            text=text,
            workflow_state=workflow_state,
            short_reply_expected=short_reply_expected,
            synthetic=synthetic,
        )
        masked = self.masker(text)
        verdicts: list[LayerVerdict] = []
        unavailable: list[GuardrailUnavailable] = []
        #: The verdict that decided, kept as one object so the decision, its category, its reason and
        #: its response cannot come from four different layers.
        decided: LayerVerdict | None = None

        for layer in self.layers:
            started = self.clock.monotonic_ms()
            try:
                verdict = layer.screen(request, masked)
            except Exception as error:  # the class is the caller's, the rule is §9's
                # A layer that cannot answer closes the input path. Both this module's exception and
                # the package's are recognised by name, because which one arrives depends on which
                # side raised it and neither of them is a permission.
                if is_unavailable(error):
                    unavailable.append(
                        GuardrailUnavailable(
                            getattr(error, "layer", layer.name),
                            getattr(error, "reason", str(error)),
                        )
                    )
                    continue
                raise
            measured = LayerVerdict(
                layer=verdict.layer,
                decision=verdict.decision,
                category=verdict.category,
                reason=verdict.reason,
                latency_ms=int(self.clock.monotonic_ms() - started),
                response=verdict.response,
            )
            verdicts.append(measured)
            if measured.decision is not GuardrailDecision.allow:
                if decided is None or measured.decision is GuardrailDecision.block:
                    decided = measured
                if measured.decision is GuardrailDecision.block:
                    # §9: an explicit block cuts the remaining calls. A message already refused is
                    # not worth a provider round trip, and sending it would be spending money on it.
                    break

        decision = decided.decision if decided is not None else GuardrailDecision.allow
        category = decided.category if decided is not None else GuardrailCategory.benign
        response = decided.response if decided is not None else None

        if unavailable and decision is GuardrailDecision.allow:
            # §9's closed-dependency rule, at the level of the decision: an incomplete screening is
            # not an allow. It is not a block either — nobody decided — so it is a question, and the
            # write path additionally refuses (see `require_complete`).
            decision = GuardrailDecision.clarify
            category = GuardrailCategory.off_topic
            response = None

        self.verdicts_seen.extend(verdicts)
        self._record(verdicts, synthetic=synthetic)
        reason = (
            decided.reason
            if decided is not None
            else (unavailable[0].reason if unavailable else "no layer produced a verdict")
        )
        outcome = GuardrailOutcome(
            decision=decision,
            category=category,
            reason=reason,
            verdicts=tuple(verdicts),
            unavailable=tuple(unavailable),
            response=response,
        )
        self.outcomes_seen.append(outcome)
        if len(self.outcomes_seen) > self.outcome_limit:
            del self.outcomes_seen[: len(self.outcomes_seen) - self.outcome_limit]
        return outcome

    def _record(self, verdicts: Sequence[LayerVerdict], *, synthetic: bool) -> None:
        """Emit §15's guardrail decision, **one record per layer that answered**.

        Per layer rather than per screening, because stage is a dimension §15 groups by: "which
        layer decided" is the question an operator asks when a good message is refused, and a single
        record carrying the winning layer would answer it only for the messages that were refused.
        A three-layer screening therefore writes up to three records, and the layer that never ran
        because an earlier `block` cut the call writes none — a layer that was not consulted did not
        decide anything, and recording an `allow` for it would invent one.

        No `rule_id`. The field is §15's "the rule that fired", and the only close candidate here is
        the category (`emergency`, `profanity`), which is *what the rule matched* rather than which
        rule it was. Putting one in the other's field is the kind of near-miss that reads as correct
        in a chart and is wrong in a filter.
        """
        if self.emitter is None:
            return
        for verdict in verdicts:
            self.emitter.guardrail_decision(
                decision=str(verdict.decision),
                stage=verdict.layer.value,
                synthetic=Synthetic.SYNTHETIC if synthetic else Synthetic.LIVE_DEMO,
            )

    def require_complete(self, outcome: GuardrailOutcome) -> GuardrailOutcome:
        """Refuse a write whose screening was incomplete (§9).

        The refusal carries which layers were missing, because "the guardrail is off" is not something
        an operator can act on and "layer 2 has no model id" is.
        """
        if not outcome.complete:
            missing = ", ".join(error.layer.value for error in outcome.unavailable)
            raise GuardrailUnavailableError(
                f"the guardrail dependency is not configured ({missing}), so a write cannot be "
                "authorised (§9). A static help card is still available.",
                detail={"unavailable_layers": [error.layer.value for error in outcome.unavailable]},
            )
        return outcome

    def __call__(
        self, text: str, *, workflow_state: str, short_reply_expected: bool = False
    ) -> GuardrailOutcome:
        return self.screen(
            text, workflow_state=workflow_state, short_reply_expected=short_reply_expected
        )


def layer_report(outcomes: Sequence[GuardrailOutcome]) -> Mapping[str, object]:
    """Per-layer counts and mean latency, for the admin guardrails view (§9's per-layer measurement).

    Measured, not illustrative: it counts the outcomes it was actually given, and reports nothing
    when it was given none. A layer that did not run is **absent** rather than present with a zero,
    because a zero latency reads as "instant" and would be a measurement of nothing. `incomplete` is
    reported alongside them so a viewer can tell a quiet period from a period where screening was off.
    """
    counts: dict[str, int] = {}
    latencies: dict[str, list[int]] = {}
    for outcome in outcomes:
        for verdict in outcome.verdicts:
            key = verdict.layer.value
            counts[key] = counts.get(key, 0) + 1
            latencies.setdefault(key, []).append(verdict.latency_ms)
    return {
        "invocations": len(outcomes),
        "incomplete": sum(1 for outcome in outcomes if not outcome.complete),
        "by_layer": {
            layer: {
                "verdicts": counts[layer],
                "mean_latency_ms": (sum(latencies[layer]) / len(latencies[layer])),
            }
            for layer in counts
        },
        "synthetic": True,
    }
