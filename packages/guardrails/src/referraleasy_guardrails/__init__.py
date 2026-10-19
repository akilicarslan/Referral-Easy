# ruff: noqa: RUF002  # §9 is quoted in Turkish verbatim
"""ReferralEasy's input and output guardrails, as a package (§9).

§9 gives the system three input layers — *"regex → OpenRouter karar modeli (maskelenmiş) → yerel küçük
semantik sınıflandırıcı"* — and one output guardrail, and states the rules that make them more than a
filter chain:

* each layer is **closed** rather than permissive when it has nothing behind it, and while any layer is
  closed a booking write is refused *"Guardrail dependency kapalıyken booking yazılamaz"* while a
  static guide card stays available;
* the decision model is never called with an unmasked message;
* layer 3's method, artifact hash and thresholds are **recorded**, and its score is a cosine similarity
  to a reference utterance — *not* a calibrated probability;
* a fast ``conditional`` profile is **compared** against the three-stage one rather than assumed better;
* per-layer false positives, false negatives, added latency and cost are **measured** on a labelled
  corpus, and the layers that could not be measured say so instead of reporting a number.

The one thing this package is not is the whole defence. §9 again: *"Input screening prompt injection
için tek savunma değildir"* — the tool allowlist, the minimum context, treating retrieved sources as
data rather than instructions, and server-side policy are what carry that, and they live in the
services. A regex list is a filter, and a filter that is mistaken for a boundary is worse than no
filter at all.

Import surface, by intent:

``build_layers`` / ``LayerSet``
    Assemble the three layers from the environment, each configured or closed with its reason.
``GuardrailPipeline``
    Run them in §9's order under one profile, with the measurement attached.
``RegexGuardrail``, ``DecisionModelGuardrail``, ``LocalClassifierGuardrail``
    The layers themselves, usable without an orchestrator.
``OutputGuardrail``
    §9's output guardrail: schema, real entity ids, verified times, sourced contacts, real booking
    state, no coverage guarantee, no clinical language.
``referraleasy_guardrails.evaluation``
    The measurement CLI: per-layer rates, latency and cost on the labelled corpus, and the
    three-stage/conditional comparison.
"""

from __future__ import annotations

from referraleasy_guardrails.build import (
    CLOSED_STATE_COMPONENT,
    OFFLINE_LAYER_2_REASON,
    LayerSet,
    build_layers,
    closed_state_card,
    layer_names,
)
from referraleasy_guardrails.config import (
    ConfigError,
    GuardrailProfile,
    GuardrailSettings,
    load_settings,
    validate_pins,
)
from referraleasy_guardrails.corpus import (
    ArtifactPin,
    CorpusCase,
    LabelledCorpus,
    Reference,
    ReferenceSet,
    data_dir,
)
from referraleasy_guardrails.emergency import APPROVED_EMERGENCY_RESPONSE, matches_emergency
from referraleasy_guardrails.layer_classifier import (
    DEFAULT_MARGIN,
    HASHED_METHOD,
    OFFLINE_METHOD_PIN,
    ArtifactHashMismatch,
    ClassifierScore,
    Encoder,
    EncoderUnavailable,
    HashedReferenceEncoder,
    LocalClassifierGuardrail,
    OnnxEncoder,
    ThresholdRecord,
    load_threshold_record,
    select_threshold,
)
from referraleasy_guardrails.layer_decision_model import (
    DecisionModelClient,
    DecisionModelError,
    DecisionModelGuardrail,
    ModelResponse,
    OpenRouterDecisionModelClient,
    parse_decision,
    unmasked_identifiers,
)
from referraleasy_guardrails.layer_regex import RegexGuardrail
from referraleasy_guardrails.output import (
    OutputCheckResult,
    OutputGuardrail,
    OutputRejected,
    OutputViolation,
    SourceOracle,
)
from referraleasy_guardrails.pipeline import (
    MEASURED_PER_LAYER,
    Clock,
    GuardrailOutcome,
    GuardrailPipeline,
    InputPathClosed,
    SystemClock,
    layer_report,
)
from referraleasy_guardrails.types import (
    LAYER_ORDER,
    GuardrailLayerCheck,
    GuardrailUnavailable,
    LayerVerdict,
    ScreeningRequest,
    fold,
    mask_pii,
    normalise,
    raise_unavailable,
)

__all__ = [
    "APPROVED_EMERGENCY_RESPONSE",
    "CLOSED_STATE_COMPONENT",
    "DEFAULT_MARGIN",
    "HASHED_METHOD",
    "LAYER_ORDER",
    "MEASURED_PER_LAYER",
    "OFFLINE_LAYER_2_REASON",
    "OFFLINE_METHOD_PIN",
    "ArtifactHashMismatch",
    "ArtifactPin",
    "ClassifierScore",
    "Clock",
    "ConfigError",
    "CorpusCase",
    "DecisionModelClient",
    "DecisionModelError",
    "DecisionModelGuardrail",
    "Encoder",
    "EncoderUnavailable",
    "GuardrailLayerCheck",
    "GuardrailOutcome",
    "GuardrailPipeline",
    "GuardrailProfile",
    "GuardrailSettings",
    "GuardrailUnavailable",
    "HashedReferenceEncoder",
    "InputPathClosed",
    "LabelledCorpus",
    "LayerSet",
    "LayerVerdict",
    "LocalClassifierGuardrail",
    "ModelResponse",
    "OnnxEncoder",
    "OpenRouterDecisionModelClient",
    "OutputCheckResult",
    "OutputGuardrail",
    "OutputRejected",
    "OutputViolation",
    "Reference",
    "ReferenceSet",
    "RegexGuardrail",
    "ScreeningRequest",
    "SourceOracle",
    "SystemClock",
    "ThresholdRecord",
    "build_layers",
    "closed_state_card",
    "data_dir",
    "fold",
    "layer_names",
    "layer_report",
    "load_settings",
    "load_threshold_record",
    "mask_pii",
    "matches_emergency",
    "normalise",
    "parse_decision",
    "raise_unavailable",
    "select_threshold",
    "unmasked_identifiers",
    "validate_pins",
]
