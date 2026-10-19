# ruff: noqa: RUF002  # §9's Turkish sentences are quoted verbatim
"""The three layers in order, under one of §9's two profiles, with the measurement attached (§9).

§9 asks for two profiles and is explicit about the second one: *"Tam showcase profilinde üç katman
görünür; açık block kalan çağrıları keser. … Daha hızlı conditional profil ayrıca karşılaştırılır"* —
the full profile runs all three and shows them, an explicit block cuts the remaining calls, and the
faster conditional profile is **compared** against it rather than assumed better.

``three_stage``
    Layer 1, then 2, then 3. The first non-``allow`` verdict decides — or a later ``block``, because
    a refusal is strictly stronger than a question — and a ``block`` stops the calls after it, which
    is §9's *"açık block kalan çağrıları keser"*. An ``allow`` never overrides a decision a layer
    already made. This is the same rule the gateway's pipeline implements, and a test runs both
    orchestrators over the same corpus with the same layers and requires identical decisions — the
    comparison is asserted rather than described.

``conditional``
    The same order, minus the calls that cannot change the answer. The only skip is a layer-1 verdict
    that is *decisive by construction*: an explicit attack pattern, an emergency, an oversized or
    empty message, or a message that the anchored short-reply pattern matched **in full** while a
    question was open. Those are the cases where a model call adds cost and no information — "evet" is
    "evet". Everything else runs all three layers, including the profanity-in-a-genuine-request
    branch, which layer 1 deliberately does not treat as decisive: the same message can carry an
    injection, and the corpus contains exactly that shape.

What the pipeline refuses to do:

* **Masking happens once, here.** Every layer above layer 1 is handed the masked text, so a layer
  that forgot to mask cannot leak the patient's identifiers upstream.
* **An unavailable layer is not a permission.** §9's closed-dependency rule: the outcome carries the
  missing layers, ``complete`` is False, the decision is never ``allow``, and
  :meth:`GuardrailPipeline.require_complete` refuses the write with the layer names in the message.
* **Uncertainty and disagreement resolve to ``clarify``.** A layer that asks for clarification, or
  two layers that do not agree about a message inside the classifier's uncertainty band, produce a
  human's attention — which is a real answer — rather than the cheapest one.

The emergency rule crosses all of this: an emergency verdict short-circuits the pipeline and the
outcome carries the approved fixed safety response, so the route §9 describes is available even when
the layers behind layer 1 are closed.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from referraleasy_contracts.generated.common import (
    GuardrailCategory,
    GuardrailDecision,
    GuardrailLayer,
)
from referraleasy_guardrails.config import GuardrailProfile
from referraleasy_guardrails.types import (
    LAYER_ORDER,
    GuardrailLayerCheck,
    LayerVerdict,
    ScreeningRequest,
    is_unavailable,
    mask_pii,
)

__all__ = [
    "Clock",
    "GuardrailOutcome",
    "GuardrailPipeline",
    "InputPathClosed",
    "SystemClock",
    "layer_report",
]


class Clock(Protocol):
    """A monotonic millisecond clock. The gateway's ``Clock`` satisfies this structurally."""

    def monotonic_ms(self) -> float: ...


@dataclass(frozen=True, slots=True)
class SystemClock:
    """The real clock. Injected everywhere else, so a latency assertion is not a race."""

    def monotonic_ms(self) -> float:
        return time.perf_counter() * 1000.0


class InputPathClosed(RuntimeError):
    """A write whose screening was incomplete. §9: the booking write is refused, the card still serves.

    Carries the layers that were missing, because "the guardrail is off" is not something an operator
    can act on and "layer 2 has no model id" is.
    """

    def __init__(self, missing: Sequence[GuardrailLayer], detail: str = "") -> None:
        rendered = ", ".join(layer.value for layer in missing)
        super().__init__(
            f"the guardrail dependency is not configured ({rendered or 'unknown'}), so a write "
            "cannot be authorised (§9). A static help card is still available."
            + (f" {detail}" if detail else "")
        )
        self.missing = tuple(missing)


@dataclass(frozen=True, slots=True)
class GuardrailOutcome:
    """The pipeline's answer: a decision, the verdicts behind it, and whether it was complete.

    ``unavailable`` is the field that matters most. An outcome with anything in it is *not* a
    decision — it is "we could not decide", and the only things it authorises are a question to the
    patient or a human, or the static help card.
    """

    decision: GuardrailDecision
    category: GuardrailCategory
    reason: str
    verdicts: tuple[LayerVerdict, ...] = ()
    unavailable: tuple[tuple[GuardrailLayer, str], ...] = ()
    response: str | None = None
    profile: GuardrailProfile = GuardrailProfile.THREE_STAGE
    skipped: tuple[GuardrailLayer, ...] = ()
    latency_ms: int = 0

    @property
    def complete(self) -> bool:
        """Whether every layer §9 names actually ran."""
        return not self.unavailable

    @property
    def blocks(self) -> bool:
        return self.decision is GuardrailDecision.block

    @property
    def emergency(self) -> bool:
        """Whether this outcome must be answered with the approved safety response (§9)."""
        return any(
            verdict.category is GuardrailCategory.emergency
            or (verdict.decision is GuardrailDecision.block and verdict.response is not None)
            for verdict in self.verdicts
        )

    @property
    def needs_human(self) -> bool:
        """Whether a human should look at this message rather than the workflow acting on it."""
        return self.decision is GuardrailDecision.clarify or bool(self.unavailable)

    @property
    def missing_layers(self) -> tuple[GuardrailLayer, ...]:
        return tuple(layer for layer, _ in self.unavailable)

    def as_dict(self) -> dict[str, object]:
        """The JSON form. Same keys the gateway's admin view reports, plus the profile."""
        return {
            "decision": self.decision.value,
            "category": self.category.value,
            "reason": self.reason,
            "complete": self.complete,
            "profile": self.profile.value,
            "unavailable_layers": [layer.value for layer in self.missing_layers],
            "skipped_layers": [layer.value for layer in self.skipped],
            "latency_ms": self.latency_ms,
            "layers": [
                {
                    "layer": verdict.layer.value,
                    "decision": verdict.decision.value,
                    "category": verdict.category.value,
                    "reason": verdict.reason,
                    "latency_ms": verdict.latency_ms,
                    "method": verdict.method,
                    "similarity": verdict.similarity,
                }
                for verdict in self.verdicts
            ],
        }


@dataclass
class GuardrailPipeline:
    """§9's three layers in order, under one profile, with the measurement kept.

    ``layers`` must be exactly §9's three, in §9's order: the check is a value comparison in
    ``__post_init__``, so a reordering is an exception at construction rather than a quiet policy
    change nobody notices in a diff.
    """

    layers: tuple[GuardrailLayerCheck, ...]
    clock: Clock = field(default_factory=SystemClock)
    profile: GuardrailProfile = GuardrailProfile.THREE_STAGE
    masker: Callable[[str], str] = mask_pii
    #: Every verdict this pipeline has produced, for the per-layer measurement §9 asks for.
    verdicts_seen: list[LayerVerdict] = field(default_factory=list)
    outcomes_seen: list[GuardrailOutcome] = field(default_factory=list)
    outcome_limit: int = 500

    def __post_init__(self) -> None:
        declared = tuple(layer.name for layer in self.layers)
        if declared != LAYER_ORDER:
            raise ValueError(
                f"the guardrail layers must run in §9's order {LAYER_ORDER}, got {declared}"
            )

    @property
    def configured(self) -> bool:
        """Whether every layer has something behind it, right now, in this process."""
        return all(layer.is_configured() for layer in self.layers)

    def screen(
        self,
        text: str,
        *,
        workflow_state: str,
        short_reply_expected: bool = False,
        synthetic: bool = True,
        open_question: str | None = None,
    ) -> GuardrailOutcome:
        request = ScreeningRequest(
            text=text,
            workflow_state=workflow_state,
            short_reply_expected=short_reply_expected,
            synthetic=synthetic,
            open_question=open_question,
        )
        masked = self.masker(text)
        started = self.clock.monotonic_ms()

        verdicts: list[LayerVerdict] = []
        unavailable: list[tuple[GuardrailLayer, str]] = []
        skipped: list[GuardrailLayer] = []
        decided: LayerVerdict | None = None
        response: str | None = None

        for layer in self.layers:
            if self._skippable(verdicts) and layer.name is not GuardrailLayer.regex:
                # The conditional profile's only skip, and it is decided by a verdict rather than by
                # a heuristic: see this module's docstring for which layer-1 outcomes are decisive.
                # A *configured* layer is skipped and the skip is recorded; a layer with nothing
                # behind it is recorded as unavailable without being called, because §9's rule is
                # about the dependency rather than about the call — skipping the call must never turn
                # a closed layer into a complete screening.
                if layer.is_configured():
                    skipped.append(layer.name)
                else:
                    unavailable.append(
                        (
                            layer.name,
                            str(
                                getattr(
                                    layer,
                                    "reason_if_unconfigured",
                                    "the layer is not configured",
                                )
                            ),
                        )
                    )
                continue
            layer_started = self.clock.monotonic_ms()
            try:
                verdict = layer.screen(request, masked)
            except Exception as error:  # the class is the caller's, the rule is ours
                # A layer that cannot answer closes the input path. Both the gateway's exception and
                # this package's own are recognised by name, because which one is raised depends on
                # where the process is running (`raise_unavailable`) and neither is a permission.
                if _is_unavailable(error):
                    unavailable.append((layer.name, str(error)))
                    continue
                raise
            measured = LayerVerdict(
                layer=verdict.layer,
                decision=verdict.decision,
                category=verdict.category,
                reason=verdict.reason,
                latency_ms=int(self.clock.monotonic_ms() - layer_started),
                response=verdict.response,
                # The six fields above are the seam: every verdict a layer can return has them. These
                # three are this package's additions, and they are read defensively because a verdict
                # *built by the gateway's wrappers* carries the seam and nothing else — and this
                # pipeline is written to run those wrappers as well as its own layers (see
                # `tests/guardrails/test_guardrail_seam.py`). A measurement that was not taken is
                # reported as absent, never as the default that happens to look like a number.
                method=getattr(verdict, "method", ""),
                similarity=getattr(verdict, "similarity", None),
                # Carried, not dropped: the conditional profile reads `decisive` off the *measured*
                # verdict, so a copy that lost the flag would silently disable the only skip there is.
                # A verdict that never claimed to be decisive is not decisive — `False` is the safe
                # default, since the flag only removes work that the profile has already justified.
                decisive=bool(getattr(verdict, "decisive", False)),
            )
            verdicts.append(measured)
            if measured.decision is not GuardrailDecision.allow:
                # The first non-allow verdict decides, and a later *block* replaces it because a
                # block is strictly stronger than a question. The decision, its category, its reason
                # and its response all come from the same verdict — a `clarify` quoting the reason of
                # a layer that answered `allow` would send an operator looking at the wrong layer.
                if decided is None or measured.decision is GuardrailDecision.block:
                    decided = measured
                if measured.decision is GuardrailDecision.block:
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

        reason = (
            decided.reason
            if decided is not None
            else (unavailable[0][1] if unavailable else "no layer produced a verdict")
        )
        outcome = GuardrailOutcome(
            decision=decision,
            category=category,
            reason=reason,
            verdicts=tuple(verdicts),
            unavailable=tuple(unavailable),
            response=response,
            profile=self.profile,
            skipped=tuple(skipped),
            latency_ms=int(self.clock.monotonic_ms() - started),
        )
        self.verdicts_seen.extend(verdicts)
        self.outcomes_seen.append(outcome)
        if len(self.outcomes_seen) > self.outcome_limit:
            del self.outcomes_seen[: len(self.outcomes_seen) - self.outcome_limit]
        return outcome

    def _skippable(self, verdicts: Sequence[LayerVerdict]) -> bool:
        """Whether the verdicts so far make the remaining layers' *answers* unnecessary.

        Only under the conditional profile, and only on a layer-1 verdict that is decisive by
        construction. The distinction that matters: this says a call cannot change the answer, not that
        a missing layer may be ignored — a closed layer is still recorded as unavailable even when the
        conditional profile does not call it.
        """
        if self.profile is not GuardrailProfile.CONDITIONAL:
            return False
        return any(verdict.decisive for verdict in verdicts)

    def require_complete(self, outcome: GuardrailOutcome) -> GuardrailOutcome:
        """Refuse a write whose screening was incomplete (§9)."""
        if not outcome.complete:
            raise InputPathClosed(outcome.missing_layers, detail=outcome.reason)
        return outcome

    def __call__(
        self, text: str, *, workflow_state: str, short_reply_expected: bool = False
    ) -> GuardrailOutcome:
        return self.screen(
            text, workflow_state=workflow_state, short_reply_expected=short_reply_expected
        )


def _is_unavailable(error: BaseException) -> bool:
    """Kept as a local name for the shared rule in :mod:`referraleasy_guardrails.types`.

    The rule itself lives there because this module is not its only caller: anything that calls a layer
    directly has to tell a closure from a crash, and two copies of a name-matching rule would drift.
    """
    return is_unavailable(error)


# ---------------------------------------------------------------------------
# Measurement (§9: false positives, false negatives, added latency and cost, per layer)
# ---------------------------------------------------------------------------


def layer_report(outcomes: Sequence[GuardrailOutcome]) -> Mapping[str, object]:
    """Per-layer counts and mean latency over the outcomes it was actually given.

    Measured, not illustrative: it counts the outcomes passed in and reports nothing for a layer that
    never ran. A layer that did not run must be **absent** rather than present with a zero, because a
    zero latency reads as "instant" and would be a measurement of nothing.
    """
    counts: dict[str, int] = {}
    latencies: dict[str, list[int]] = {}
    methods: dict[str, set[str]] = {}
    for outcome in outcomes:
        for verdict in outcome.verdicts:
            key = verdict.layer.value
            counts[key] = counts.get(key, 0) + 1
            latencies.setdefault(key, []).append(verdict.latency_ms)
            if verdict.method:
                methods.setdefault(key, set()).add(verdict.method)
    return {
        "invocations": len(outcomes),
        "by_layer": {
            layer: {
                "verdicts": counts[layer],
                "mean_latency_ms": sum(latencies[layer]) / len(latencies[layer]),
                "max_latency_ms": max(latencies[layer]),
                "methods": sorted(methods.get(layer, set())),
            }
            for layer in sorted(counts)
        },
        "incomplete": sum(1 for outcome in outcomes if not outcome.complete),
        "synthetic": True,
    }


#: Everything §9 measures about a layer, in the order the report presents it. Kept as data so a
#: report that forgot one of them would be visibly incomplete rather than quietly shorter.
MEASURED_PER_LAYER: Final[tuple[str, ...]] = (
    "false_positives",
    "false_negatives",
    "mean_latency_ms",
    "provider_calls",
    "tokens",
    "cost_usd",
)
