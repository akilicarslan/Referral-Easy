"""The layer contract: what a layer is handed, what it answers, and what "closed" means (§9).

This module exists so that the three layers can be built, tested and measured without a running
gateway, **and** so that the verdict they produce can be handed to the gateway's existing
`GuardrailPipeline` unchanged. The seam is structural, not nominal: the gateway's pipeline reads
`layer.name`, calls `layer.screen(request, masked_text)` and copies `layer / decision / category /
reason / latency_ms / response` off whatever comes back. Everything here is named accordingly.

§9's three layers and their order:

1. ``regex`` — patterns, normalisation, a length limit, and the two state-aware exceptions.
2. ``decision_model`` — an OpenRouter decision model run on a **masked** message.
3. ``local_classifier`` — a local small semantic model with a recorded artifact hash and thresholds.

Two rules in this module are the reason it is not just a bag of dataclasses:

* **A layer that cannot answer never answers "allow".** §9's *"Booking writes are prohibited while a
* guardrail dependency is unavailable"* makes the closed state a refusal. Undecided is not a third
* kind of allow, so
  a layer with nothing behind it raises rather than returning a verdict, and the orchestrator reports
  it as unavailable rather than as permission.
* **Uncertainty and disagreement resolve to ``clarify``** — a human's attention, which is a real
  answer — and never to a silent allow. §9 states this for layer 3 and it is applied in
  :mod:`referraleasy_guardrails.pipeline` to the layers collectively.
"""

from __future__ import annotations

import importlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, NoReturn, Protocol, runtime_checkable

# First-party (see [tool.ruff] src): the contract enums, which the gateway's pipeline also uses.
from referraleasy_contracts.generated.common import (
    GuardrailCategory,
    GuardrailDecision,
    GuardrailLayer,
)

__all__ = [
    "LAYER_ORDER",
    "GuardrailLayerCheck",
    "GuardrailUnavailable",
    "LayerVerdict",
    "ScreeningRequest",
    "is_unavailable",
    "mask_pii",
    "raise_unavailable",
]

#: §9's order, as data so an orchestrator can assert it rather than assume it. Same tuple the
#: gateway declares; the two must agree or a layer would be consulted out of order.
LAYER_ORDER: Final[tuple[GuardrailLayer, ...]] = (
    GuardrailLayer.regex,
    GuardrailLayer.decision_model,
    GuardrailLayer.local_classifier,
)

#: The categories a layer may name. Anything else from a model is a response that is not a decision.
CATEGORIES: Final[frozenset[str]] = frozenset(member.value for member in GuardrailCategory)


class GuardrailUnavailable(RuntimeError):
    """A layer has nothing behind it, or answered with something that is not a decision.

    **Not** an allow, and never a fallback to one. The attribute names (``layer``, ``reason``) and
    the constructor signature match the gateway's own exception of the same name, because the
    gateway's pipeline closes the write path with ``except GuardrailUnavailable`` and the subclass
    test is what decides whether this refusal reaches the caller as §9's ``503`` or as a generic
    ``500``. :func:`raise_unavailable` raises whichever of the two the running process knows about.
    """

    def __init__(self, layer: GuardrailLayer, reason: str) -> None:
        super().__init__(f"guardrail layer {layer.value} is not configured: {reason}")
        self.layer = layer
        self.reason = reason


def raise_unavailable(layer: GuardrailLayer, reason: str) -> NoReturn:
    """Raise the unavailable error the calling pipeline actually catches.

    When this package is used standalone, that is :class:`GuardrailUnavailable`. When it is used
    behind the gateway's pipeline, the class that closes the write path is the gateway's — the
    pipeline catches its *own* class, and an equivalent-but-different class would leave the process
    as an unhandled ``500`` instead of §9's refusal.

    The lookup is a runtime import inside the function rather than a module-level import for two
    reasons: a package must not depend on a service (the dependency direction here is
    ``services → packages``), and a checkout that installs the package without the gateway must not
    fail to import. This is the only place in the package that knows the gateway exists, and a test
    asserts that no other module imports it.
    """
    try:
        module = importlib.import_module("referraleasy_gateway.guardrails")
        gateway_error = getattr(module, "GuardrailUnavailable", None)
    except ImportError:  # pragma: no cover - no gateway in this environment
        gateway_error = None
    if isinstance(gateway_error, type) and issubclass(gateway_error, BaseException):
        raise gateway_error(layer, reason)
    raise GuardrailUnavailable(layer, reason)


def is_unavailable(error: BaseException) -> bool:
    """Whether an exception means "this layer has nothing behind it" (§9's closed state).

    Two classes qualify: this package's :class:`GuardrailUnavailable` and the gateway's, which is what
    :func:`raise_unavailable` raises when the package runs behind the gateway's pipeline. Matching on
    the class *name* is deliberate — importing the gateway's class here would make a package depend on
    a service, and matching on ``RuntimeError`` would turn a genuine bug inside a layer into a silent
    "the guardrail is off", which is the one reading §9 forbids.

    Public because two callers need the same rule: the orchestrator (which must not treat an
    unavailable layer as permission) and anything that calls layers directly and has to tell a
    closure from a crash.
    """
    return type(error).__name__ == "GuardrailUnavailable" and hasattr(error, "layer")


@dataclass(frozen=True, slots=True)
class ScreeningRequest:
    """What a layer is allowed to see, including the context §9 says it needs.

    ``short_reply_expected`` is state-awareness as a *fact* rather than a guess: whether a question
    is open is something the workflow knows, and a layer that inferred it from the text would be
    reconstructing the workflow from a sentence. ``open_question`` carries the question when there
    is one, because "no" is a different answer depending on what was asked.

    The first four fields match the gateway's ``ScreeningRequest``; ``open_question`` is additive and
    read defensively by the layers, so an object built by the gateway can be passed to them as-is.
    """

    text: str
    workflow_state: str
    short_reply_expected: bool = False
    synthetic: bool = True
    open_question: str | None = None

    @property
    def masked_text(self) -> str:
        """The message as the model-backed layers see it. Masking is idempotent."""
        return mask_pii(self.text)


@dataclass(frozen=True, slots=True)
class LayerVerdict:
    """One layer's answer.

    The first six fields are the seam: the gateway's pipeline copies exactly these into its own
    verdict. ``method`` and ``similarity`` are the measurement §9 asks for (*"measure false
    positives/false negatives and added latency/cost per layer"*), and they are additive — a caller
    that only knows the seam ignores them.

    ``similarity`` is a **cosine similarity to the closest reference utterance**. It is not a
    probability, it is not calibrated, and nothing may present it as one; see
    :mod:`referraleasy_guardrails.layer_classifier`.
    """

    layer: GuardrailLayer
    decision: GuardrailDecision
    category: GuardrailCategory
    reason: str
    latency_ms: int = 0
    #: The approved fixed text for an emergency verdict, and None otherwise.
    response: str | None = None
    #: Which implementation produced this verdict — ``regex-v1``, ``openrouter:<model>``,
    #: ``hashed-char-ngram-v1``, ``minilm-onnx:<sha256 prefix>``. Reported per layer, because a
    #: number attached to "layer 3" without its method would attribute a lexical matcher's
    #: operating point to the pinned encoder.
    method: str = ""
    #: The raw score behind a classifier verdict, when there is one.
    similarity: float | None = None
    #: Whether this verdict makes the layers after it unnecessary *by construction* — an anchored
    #: pattern that matched the whole message, an explicit attack, an emergency, an empty or oversized
    #: message. The conditional profile skips on this flag and on nothing else, so "faster" is a
    #: property of the message rather than a bet about the layer that would have run. Only layer 1
    #: sets it, and only for outcomes where a model call could not change the answer.
    decisive: bool = False


@runtime_checkable
class GuardrailLayerCheck(Protocol):
    """A layer, as the orchestrator sees it. ``screen`` raises when it has nothing behind it."""

    name: GuardrailLayer

    def is_configured(self) -> bool: ...

    def screen(self, request: ScreeningRequest, masked_text: str) -> LayerVerdict: ...


# ---------------------------------------------------------------------------
# Masking (§9: the decision model is given a masked message)
# ---------------------------------------------------------------------------

_PII_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("phone", re.compile(r"(?<!\d)(?:\+?\d[\d \-().]{7,}\d)(?!\d)")),
    # Seven, not nine. The member ids the directory hands out and the corpus carries are seven digits
    # ("my member id is 2205518"), and a nine-digit floor sent exactly the identifier the masking
    # exists to remove. The gap was found by a test, not by reading: `unmasked_identifiers` shares
    # this floor, so the layer's "is the masking holding?" check agreed with the masker's mistake.
    ("long_digits", re.compile(r"(?<!\d)\d{7,}(?!\d)")),
)


def mask_pii(text: str) -> str:
    """Replace the identifiers a provider must not receive, keeping the shape of the message.

    Placeholders keep the token count and rough length, so a layer that reasons about the message
    still sees roughly the same message: the masking removes the *value*, not the sentence.

    Ported from the gateway's ``mask_pii`` (same three patterns, same placeholders, and now one
    deliberate widening) so that a verdict produced here is the verdict the gateway's pipeline would
    have produced. Masking is idempotent, which is what lets a caller mask a message twice without the
    second pass changing it.
    """
    masked = text
    for label, pattern in _PII_PATTERNS:
        masked = pattern.sub(f"[{label}]", masked)
    return masked


# ---------------------------------------------------------------------------
# Normalisation (shared by layer 1 and the layer-3 encoder, so the two agree on what a character is)
# ---------------------------------------------------------------------------


def normalise(text: str) -> str:
    """NFKC, control characters removed, whitespace collapsed.

    Turkish is the reason NFKC rather than NFC: the dotted/dotless i and the combining marks in
    ``"göğüs"`` survive NFKC, and NFKC folds the compatibility forms a homoglyph-laden message might
    use. Control characters are dropped (they are invisible, so a pattern that matched only because
    of one would be unreviewable) except newline, which is collapsed by the whitespace rule.
    """
    normalised = unicodedata.normalize("NFKC", text)
    normalised = "".join(
        ch for ch in normalised if ch == "\n" or not unicodedata.category(ch).startswith("C")
    )
    return re.sub(r"\s+", " ", normalised).strip()


def fold(text: str) -> str:
    """The comparison form used by the layer-3 encoder: normalised, case-folded, Turkish-aware.

    ``str.casefold`` rather than ``lower`` because casefold is the form meant for comparison, and
    the dotted/dotless-i mapping is applied explicitly so that ``"İSTİYORUM"`` and ``"istiyorum"``
    reach the same feature vector.
    """
    return normalise(text).casefold().replace("i̇", "i")


def as_sequence(values: Sequence[str] | str) -> tuple[str, ...]:
    """One text or many, as a tuple — so a layer accepts either without a branch at every call."""
    if isinstance(values, str):
        return (values,)
    return tuple(values)
