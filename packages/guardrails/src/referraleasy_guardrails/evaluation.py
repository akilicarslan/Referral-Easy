# ruff: noqa: RUF001, RUF002  # §9's Turkish sentences and the Turkish warm-up message are verbatim
"""The measurement §9 asks for, as a CLI over the labelled corpus (§9).

§9 requires four numbers per layer — *"Katman başına false-positive/false-negative ve ek
latency/maliyet ölçülür"* — on a labelled corpus, and it requires the fast profile to be **compared**
rather than assumed better: *"Daha hızlı conditional profil ayrıca karşılaştırılır"*. This module is
where those numbers are produced, and its most important output is the list of numbers it could *not*
produce.

Three rules it follows, because a measurement that breaks them is worse than no measurement:

* **Every number is keyed by method.** Layer 3 is not "layer 3" — it is ``hashed-char-ngram-v1`` or
  ``minilm-onnx:<sha prefix>``. A rate reported under the first name and read as the second would
  attribute a lexical matcher's operating point to the ADR-0005 encoder.
* **Nothing is substituted quietly.** Layer 2 is measured with the client the environment builds; if
  there is none (the offline profile), the layer is reported as unmeasured with its reason, and no
  scripted stand-in takes its place. Layer 3's encoder is named in ``layer3_encoder_source`` on every
  run, and ``--layer3 env`` refuses to fall back at all.
* **The score is a similarity.** Layer 3's numbers are cosine similarities to reference utterances,
  and the classifier's block rule also requires the closest reference to be an attack reference. The
  threshold is not a calibrated probability and this module never calls it one (§9).

Run it from the repository root::

    PYTHONPATH=packages/guardrails/src uv run --no-sync python -m referraleasy_guardrails.evaluation
    PYTHONPATH=packages/guardrails/src uv run --no-sync python -m referraleasy_guardrails.evaluation \\
        --layer3 offline --calibrate

The second form is the one that writes ``data/threshold_record.json``: it selects layer 3's operating
point on the corpus's ``calibration`` partition inside a false-positive budget and records what that
point produced on the ``held_out`` partition, together with the corpus and reference hashes it was
measured against (ADR-0005's intake rule). It **never** makes a live model call unless ``--live`` is
passed, and it never edits the repository's ``.env``.

Finally, the honest caveat §9 states and this module repeats in its output: *"Input screening prompt
injection için tek savunma değildir"*. The document-channel case in the corpus is screened by nothing
here, on purpose — the tool allowlist, the minimum context, treating sources as data and server-side
policy are what carry that, and they are not measurable from this package.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from referraleasy_contracts.generated.common import (
    GuardrailDecision,
)
from referraleasy_guardrails.build import LayerSet, build_layers
from referraleasy_guardrails.config import (
    DEFAULT_ENV_FILE,
    ConfigError,
    GuardrailProfile,
    GuardrailSettings,
    parse_env_file,
)
from referraleasy_guardrails.corpus import (
    ArtifactPin,
    CorpusCase,
    LabelledCorpus,
    ReferenceSet,
    data_dir,
)
from referraleasy_guardrails.layer_classifier import (
    DEFAULT_MARGIN,
    HASHED_METHOD,
    OFFLINE_METHOD_PIN,
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
from referraleasy_guardrails.layer_decision_model import DecisionModelGuardrail
from referraleasy_guardrails.pipeline import (
    MEASURED_PER_LAYER,
    GuardrailOutcome,
    GuardrailPipeline,
    SystemClock,
)
from referraleasy_guardrails.types import ScreeningRequest, is_unavailable

__all__ = [
    "THRESHOLD_RECORD_FILE",
    "main",
    "measure",
    "write_threshold_record",
]

#: Where a measurement of layer 3 is recorded so that the layer may run at all (ADR-0005: a threshold
#: without its measurement is not a threshold).
THRESHOLD_RECORD_FILE: Final = data_dir() / "threshold_record.json"

#: The false-positive budget the calibration partition selects its operating point inside. Five per
#: cent of *legitimate* messages refused is the number this project is willing to pay for sensitivity,
#: and it is a policy choice rather than a measurement, which is why it is a named constant here and
#: written into the record.
DEFAULT_TARGET_FALSE_POSITIVE_RATE: Final = 0.05

#: Cases where refusing, or asking, costs a real patient something.
BENIGN_KINDS: Final[frozenset[str]] = frozenset(
    {"benign", "short_reply_valid", "profanity_genuine"}
)
#: Cases the layers are supposed to refuse outright, by kind.
MUST_BLOCK_KINDS: Final[frozenset[str]] = frozenset({"attack", "profanity_abuse"})
#: The kinds layer 3's threshold may be selected and reported on: the two sides it decides between.
SELECTION_KINDS: Final[frozenset[str]] = BENIGN_KINDS | {"attack"}


@dataclass(frozen=True, slots=True)
class CaseRun:
    """One case, the outcome the pipeline produced for it, and the wall clock around the call."""

    case: CorpusCase
    outcome: GuardrailOutcome
    elapsed_ms: float


def measure(
    *,
    settings: GuardrailSettings | None = None,
    corpus: LabelledCorpus | None = None,
    references: ReferenceSet | None = None,
    layer3: str = "auto",
    live: bool = False,
    calibrate: bool = False,
    record_path: Path | None = None,
) -> Mapping[str, Any]:
    """Produce the report. Pure measurement: it writes nothing unless ``calibrate`` is set."""
    resolved = settings or GuardrailSettings.from_env()
    if not live and resolved.live_model_backend:
        # The default, and the reason `--live` exists: a measurement run must not spend money or send
        # anything anywhere. MODEL_BACKEND=live is the *deployment's* wish; this flag is the run's.
        resolved = replace(resolved, model_backend="offline")

    labelled = corpus or LabelledCorpus.load()
    reference_set = references or ReferenceSet.load()
    clock = SystemClock()

    # The deployment's own state, checked against the corpus *this run* measured on: a synthetic
    # corpus passed by a test would otherwise be reported as configured against the committed one.
    deployment = build_layers(resolved, corpus=labelled)
    layers, encoder_note, encoder_source, encoder = _measurement_layers(
        deployment, resolved, reference_set, labelled, layer3=layer3
    )
    warmup = _warm_up(layers, clock)

    per_layer = _per_layer(layers, labelled, clock)
    profiles = {
        profile.value: _pipeline_profile(layers, labelled, profile, clock)
        for profile in (GuardrailProfile.THREE_STAGE, GuardrailProfile.CONDITIONAL)
    }
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "corpus": _corpus_summary(labelled),
        "configuration": {
            "profile_env": resolved.profile.value,
            "model_backend": resolved.model_backend,
            "live_calls_permitted": live,
            "layers": _layer_state(deployment),
            "env_threshold": resolved.local_threshold,
            "layer3_record": _record_state(labelled, reference_set),
            "layer3_encoder_source": encoder_source,
            "layer3_encoder_note": encoder_note,
        },
        "per_layer": per_layer,
        "warmup": warmup,
        "profiles": profiles,
        "profile_comparison": _compare_profiles(profiles),
        "measured_per_layer_keys": list(MEASURED_PER_LAYER),
        "unmeasured": _unmeasured(live=live),
        "notes": [
            "Every rate is keyed by method: a number for 'layer 3' alone would attribute a lexical "
            "matcher's operating point to the pinned encoder.",
            "Layer 3's score is a cosine similarity to the closest reference utterance, not a "
            "calibrated probability (§9).",
            "Input screening is not the only defence against prompt injection (§9): the "
            "document-channel case is screened by none of these layers, by design — the tool "
            "allowlist, minimum context, treating sources as data and server-side policy carry it.",
            "Latency is in-process wall clock around the call, on this machine, with synthetic "
            "messages; it is not a production latency figure.",
        ],
    }
    if calibrate:
        report["threshold_record"] = write_threshold_record(
            reference_set=reference_set,
            corpus=labelled,
            layers=layers,
            encoder=encoder,
            target_false_positive_rate=DEFAULT_TARGET_FALSE_POSITIVE_RATE,
            path=record_path,
        )
    return report


# ---------------------------------------------------------------------------
# Which layers the measurement runs with, and what the environment says about them
# ---------------------------------------------------------------------------


def _measurement_layers(
    deployment: LayerSet,
    settings: GuardrailSettings,
    references: ReferenceSet,
    corpus: LabelledCorpus,
    *,
    layer3: str,
) -> tuple[LayerSet, str, str, Encoder | None]:
    """Build the layers used for measurement: the deployment's choice, or the one named on the CLI.

    Layer 2 is taken exactly as the environment gives it. Layer 3 differs from the deployment in one
    respect that is always stated: the **environment threshold is not applied here**, because the
    measurement is of the recorded operating point, and whether the environment would accept that
    record is a separate fact the report carries under ``configuration``. Nothing else is substituted,
    and ``encoder_source`` names which encoder produced every layer-3 number.
    """
    encoder: Encoder | None = None
    note = ""
    source = "environment"
    env_pin = settings.local_model.strip()

    if layer3 == "offline":
        encoder = HashedReferenceEncoder()
        source = f"named on the command line ({OFFLINE_METHOD_PIN})"
    elif layer3 in {"env", "auto", "onnx"}:
        pin = ArtifactPin.find(env_pin)
        if layer3 == "onnx" and pin is None:
            candidates = ArtifactPin.load_all()
            pin = candidates[0] if candidates else None
        if pin is None:
            note = (
                f"GUARDRAIL_LOCAL_MODEL={env_pin!r} names no pinned artifact in "
                "data/artifacts.json, so the environment's own layer 3 has no encoder"
            )
            if layer3 == "env":
                source = "environment (unresolvable pin)"
            else:
                encoder, source = HashedReferenceEncoder(), "offline rung (fallback, stated)"
        else:
            try:
                encoder = OnnxEncoder.load(pin, settings.artifact_dir)
                source = f"environment ({pin.pin})"
            except EncoderUnavailable as error:
                note = str(error)
                if layer3 == "env":
                    source = f"environment ({pin.pin}); the pinned artifact could not be loaded"
                else:
                    encoder, source = HashedReferenceEncoder(), "offline rung (fallback, stated)"
    else:  # pragma: no cover - argparse restricts the choice
        raise AssertionError(f"unknown --layer3 value {layer3!r}")

    record = load_threshold_record()
    classifier = LocalClassifierGuardrail.build(
        pin=env_pin,
        encoder=encoder,
        references=references,
        record=record,
        corpus=corpus,
        # Deliberately empty: the measurement is of the recorded operating point. `configuration`
        # reports what the environment's own threshold would do to that layer instead.
        env_threshold="",
        closed_reason=note if encoder is None else "",
    )
    return (
        LayerSet(
            regex=deployment.regex,
            decision_model=deployment.decision_model,
            local_classifier=classifier,
            settings=settings,
        ),
        note,
        source,
        encoder,
    )


def _layer_state(layers: LayerSet) -> Mapping[str, Any]:
    """Whether each layer is configured, and why not when it is not. Facts, not assurances.

    Read from the **deployment** layer set, so this is what the environment would do — including the
    case where a record exists, is usable, and is nevertheless refused because
    ``GUARDRAIL_LOCAL_THRESHOLD`` disagrees with it.
    """
    state: dict[str, Any] = {}
    for layer in layers.ordered:
        entry: dict[str, Any] = {"configured": layer.is_configured()}
        if layer.is_configured():
            method = getattr(layer, "method", "")
            if method:
                entry["method"] = str(method)
        else:
            entry["reason"] = str(getattr(layer, "reason_if_unconfigured", ""))
        state[layer.name.value] = entry
    return state


def _record_state(corpus: LabelledCorpus, references: ReferenceSet) -> Mapping[str, Any]:
    """Whether a measurement exists for layer 3, and whether it still speaks for this tree."""
    record = load_threshold_record()
    if record is None:
        return {
            "present": False,
            "path": str(THRESHOLD_RECORD_FILE),
            "reason": (
                "no threshold measurement is recorded. §9 requires the method, the artifact hash and "
                "the thresholds to be recorded; until then layer 3 is inert and says so."
            ),
        }
    mismatch = record.mismatch(method=record.method, corpus=corpus, references=references)
    return {
        "present": True,
        "path": str(THRESHOLD_RECORD_FILE),
        "method": record.method,
        "encoder": record.encoder,
        "artifact_hash": record.artifact_hash,
        "threshold": record.threshold,
        "margin": record.margin,
        "corpus_hash": record.corpus_hash,
        "references_hash": record.references_hash,
        "speaks_for_this_tree": mismatch is None,
        "mismatch": mismatch,
    }


# ---------------------------------------------------------------------------
# Per-layer measurement (§9's four numbers, each keyed by method)
# ---------------------------------------------------------------------------


def _warm_up(layers: LayerSet, clock: SystemClock) -> Mapping[str, Any]:
    """Call the closed layers once, discard the answer, and report what that first call cost.

    Without this, the one-off cost of resolving the unavailable-layer exception class — an import of
    the gateway's module when the package is used behind it, ~150 ms on this machine — lands on
    whichever case happened to be screened first and reads in the report as per-case latency. It is a
    real cost and it is still reported: as a startup cost, which is what it is.

    Only layers with nothing behind them are warmed, because their call is free and their closure is
    the expensive path. A configured layer is not called here: that would spend a provider call (or a
    token's worth of one) to measure a latency the per-layer table measures anyway.
    """
    request = ScreeningRequest(
        text="merhaba, yarın için bir randevu almak istiyorum",
        workflow_state="INTAKE",
        synthetic=True,
    )
    first_call: dict[str, float] = {}
    for layer in layers.ordered:
        if layer.is_configured():
            continue
        started = clock.monotonic_ms()
        try:
            layer.screen(request, request.masked_text)
        except RuntimeError as error:
            # The class of a closure depends on whether the gateway is importable in this process, so
            # the *rule* decides, not the class (`is_unavailable`). Anything else is a real failure and
            # must not be swallowed here.
            if not is_unavailable(error):
                raise
        first_call[layer.name.value] = round(clock.monotonic_ms() - started, 3)
    return {
        "first_call_ms": first_call,
        "note": (
            "one discarded call to each closed layer before any timed run, so a first-touch or "
            "import cost is not reported as per-case latency"
        ),
    }


def _per_layer(
    layers: LayerSet,
    corpus: LabelledCorpus,
    clock: SystemClock,
) -> Mapping[str, Any]:
    """Call every layer on every user-channel case, and count what came back.

    Each layer is exercised **directly**, not only where an orchestrator would reach it: a layer whose
    false negatives are only visible after an earlier layer has already blocked would otherwise look
    perfect. The corpus's retrieved-document case is excluded, and the exclusion is stated in the
    entry rather than left implicit.

    Two method fields, because they answer two different questions. ``method`` is what the layer
    *declares* (``openrouter:<model>`` for layer 2, the encoder's method for layer 3), and it is
    reported even for a closed layer, which is what the reason beside it is for. ``methods`` is what
    the verdicts this run actually produced carried — layer 1 declares nothing but every verdict it
    answers with says ``regex-v1``, and a table that printed ``-`` beside the one layer that ran on
    every case would be the misattribution this module is keyed by method to avoid.
    """
    cases = corpus.user_messages()
    measured: dict[str, Any] = {}
    # The three concrete layers rather than the seam's protocol: this module reads each layer's own
    # measurement surface (`usage_totals`, `reason_if_unconfigured`), which §9's seam does not carry.
    for layer in (layers.regex, layers.decision_model, layers.local_classifier):
        entry: dict[str, Any] = {
            "method": getattr(layer, "method", ""),
            "methods": [],
            "configured": layer.is_configured(),
            "cases_screened": 0,
            "cases_not_screened": 0,
            "not_screened_reason": "",
            "confusion": {},
            "false_positives": None,
            "false_negatives": None,
            "silent_allows_should_ask": None,
            "over_asks": None,
            "attacks_not_blocked": None,
            "attack_cases": 0,
            "benign_cases": 0,
            "mean_latency_ms": None,
            "max_latency_ms": None,
            "provider_calls": 0,
            "tokens": None,
            "cost_usd": None,
            "cost_note": "local: no provider is called, so there is no per-call cost to report",
        }
        if not layer.is_configured():
            entry["cases_not_screened"] = len(cases)
            entry["not_screened_reason"] = str(getattr(layer, "reason_if_unconfigured", ""))
            measured[layer.name.value] = entry
            continue

        latencies: list[float] = []
        confusion: dict[str, int] = {}
        methods: set[str] = set()
        false_positives = false_negatives = silent_allows = over_asks = 0
        attacks = attacks_not_blocked = benign_cases = 0
        for case in cases:
            request = case.request()
            started = clock.monotonic_ms()
            try:
                verdict = layer.screen(request, request.masked_text)
            except RuntimeError as error:
                # A layer that closes mid-run (a provider failure, a credential that expired) stops
                # its own measurement and says why; a genuine bug inside a layer is re-raised.
                if not is_unavailable(error):
                    raise
                entry["cases_not_screened"] = len(cases)
                entry["not_screened_reason"] = str(error)
                break
            latencies.append(clock.monotonic_ms() - started)
            if verdict.method:
                methods.add(verdict.method)
            decision = verdict.decision
            key = f"{case.expected.value}->{decision.value}"
            confusion[key] = confusion.get(key, 0) + 1
            if case.kind in BENIGN_KINDS:
                benign_cases += 1
                if decision is GuardrailDecision.block:
                    false_positives += 1
                elif decision is GuardrailDecision.clarify:
                    over_asks += 1
            if case.kind in MUST_BLOCK_KINDS:
                attacks += 1
                if decision is not GuardrailDecision.block:
                    attacks_not_blocked += 1
                    if decision is GuardrailDecision.allow:
                        false_negatives += 1
            elif (
                case.expected is GuardrailDecision.block and decision is not GuardrailDecision.block
            ):
                # An oversized input, or an emergency: the corpus says this must be refused, and
                # neither is a "maybe".
                if decision is GuardrailDecision.allow:
                    false_negatives += 1
            if case.expected is GuardrailDecision.clarify and decision is GuardrailDecision.allow:
                # Not a refusal the layer owed a patient but a question it owed them: the paraphrased
                # emergency and the unanswered short reply are both in here.
                silent_allows += 1

        entry.update(
            {
                "cases_screened": len(latencies),
                "cases_not_screened": len(cases) - len(latencies),
                "confusion": dict(sorted(confusion.items())),
                "methods": sorted(methods),
                "false_positives": false_positives,
                "false_negatives": false_negatives,
                "silent_allows_should_ask": silent_allows,
                "over_asks": over_asks,
                "attacks_not_blocked": attacks_not_blocked,
                "attack_cases": attacks,
                "benign_cases": benign_cases,
                "mean_latency_ms": round(statistics.fmean(latencies), 4) if latencies else None,
                "max_latency_ms": round(max(latencies), 4) if latencies else None,
            }
        )
        if isinstance(layer, DecisionModelGuardrail):
            totals = layer.usage_totals()
            entry["provider_calls"] = totals.get("provider_calls")
            entry["tokens"] = {
                "prompt": totals.get("prompt_tokens"),
                "completion": totals.get("completion_tokens"),
            }
            entry["cost_usd"] = totals.get("cost_usd")
            entry["cost_note"] = str(totals.get("reason", ""))
        elif isinstance(layer, LocalClassifierGuardrail):
            entry["cost_note"] = (
                "local artifact or stdlib matcher: no provider call, no metered cost"
            )
        measured[layer.name.value] = entry
    measured["retrieved_document_cases_not_screened"] = len(corpus.cases) - len(cases)
    return measured


# ---------------------------------------------------------------------------
# The pipeline under both profiles, and their comparison (§9: compared, not assumed)
# ---------------------------------------------------------------------------


def _pipeline_profile(
    layers: LayerSet,
    corpus: LabelledCorpus,
    profile: GuardrailProfile,
    clock: SystemClock,
) -> Mapping[str, Any]:
    pipeline = layers.pipeline(clock=clock, profile=profile)
    runs = [_run_case(pipeline, case, clock) for case in corpus.user_messages()]

    decisions: dict[str, int] = {}
    per_layer_calls: dict[str, int] = {}
    per_layer_latency: dict[str, list[float]] = {}
    unavailable: dict[str, int] = {}
    skipped: dict[str, int] = {}
    refusals_on_benign = 0
    attacks_allowed = 0
    for run in runs:
        decisions[run.outcome.decision.value] = decisions.get(run.outcome.decision.value, 0) + 1
        for verdict in run.outcome.verdicts:
            key = verdict.layer.value
            per_layer_calls[key] = per_layer_calls.get(key, 0) + 1
            per_layer_latency.setdefault(key, []).append(verdict.latency_ms)
        for layer, _ in run.outcome.unavailable:
            unavailable[layer.value] = unavailable.get(layer.value, 0) + 1
        for layer in run.outcome.skipped:
            skipped[layer.value] = skipped.get(layer.value, 0) + 1
        if run.case.kind in BENIGN_KINDS and run.outcome.blocks:
            refusals_on_benign += 1
        if run.case.kind in MUST_BLOCK_KINDS and run.outcome.decision is GuardrailDecision.allow:
            attacks_allowed += 1

    return {
        "profile": profile.value,
        "cases": len(runs),
        "decisions": dict(sorted(decisions.items())),
        "complete": sum(1 for run in runs if run.outcome.complete),
        "writes_refused": sum(1 for run in runs if not run.outcome.complete),
        "layer_calls": per_layer_calls,
        "layer_skipped": dict(sorted(skipped.items())),
        "layer_unavailable": dict(sorted(unavailable.items())),
        "mean_layer_latency_ms": {
            key: round(statistics.fmean(values), 4)
            for key, values in sorted(per_layer_latency.items())
            if values
        },
        "mean_total_latency_ms": (
            round(statistics.fmean([run.elapsed_ms for run in runs]), 4) if runs else None
        ),
        "false_positives": refusals_on_benign,
        "attacks_allowed": attacks_allowed,
    }


def _compare_profiles(profiles: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
    """The conditional profile against the three-stage one, on the same cases and the same layers."""
    full = profiles[GuardrailProfile.THREE_STAGE.value]
    fast = profiles[GuardrailProfile.CONDITIONAL.value]
    layer_delta = {
        layer: fast["layer_calls"].get(layer, 0) - full["layer_calls"].get(layer, 0)
        for layer in sorted(set(full["layer_calls"]) | set(fast["layer_calls"]))
    }
    decision_changes = {
        key: (full["decisions"].get(key, 0), fast["decisions"].get(key, 0))
        for key in sorted(set(full["decisions"]) | set(fast["decisions"]))
        if full["decisions"].get(key, 0) != fast["decisions"].get(key, 0)
    }
    skipped = fast["layer_skipped"]
    calls_full = sum(full["layer_calls"].values())
    changed = sum(abs(a - b) for a, b in decision_changes.values())
    # Computed, not asserted: the sentences below say what these two runs did, and if the next corpus
    # changes the answer they change with it.
    verdict = (
        f"the conditional profile skipped {sum(skipped.values())} of the {calls_full} layer calls "
        f"the three-stage profile made ({skipped or 'none'}) and "
        + ("changed no case's decision" if not changed else f"changed {changed} cases' decisions")
        + f", on a mean wall clock of {full['mean_total_latency_ms']} ms against "
        f"{fast['mean_total_latency_ms']} ms per case. "
        "The skip applies only to layers that are configured: a layer with nothing behind it is "
        "recorded as unavailable, and the write is refused, in both profiles."
    )
    if not skipped:
        verdict += (
            " Nothing was skipped in this run, which means layer 1 decided no message decisively: "
            "the skip rule is exercised with configured layers by the test suite."
        )
    return {
        "cases": full["cases"],
        "layer_calls_three_stage": full["layer_calls"],
        "layer_calls_conditional": fast["layer_calls"],
        "layer_call_delta": layer_delta,
        "decision_changes": decision_changes,
        "decisions_changed": changed,
        "mean_total_latency_ms": {
            full["profile"]: full["mean_total_latency_ms"],
            fast["profile"]: fast["mean_total_latency_ms"],
        },
        "complete": {"three_stage": full["complete"], "conditional": fast["complete"]},
        "writes_refused": {
            "three_stage": full["writes_refused"],
            "conditional": fast["writes_refused"],
        },
        "verdict": verdict,
    }


def _run_case(pipeline: GuardrailPipeline, case: CorpusCase, clock: SystemClock) -> CaseRun:
    request = case.request()
    started = clock.monotonic_ms()
    outcome = pipeline.screen(
        request.text,
        workflow_state=request.workflow_state,
        short_reply_expected=request.short_reply_expected,
        synthetic=request.synthetic,
        open_question=request.open_question,
    )
    return CaseRun(case=case, outcome=outcome, elapsed_ms=clock.monotonic_ms() - started)


# ---------------------------------------------------------------------------
# The threshold record (ADR-0005's intake rule, written down)
# ---------------------------------------------------------------------------


def write_threshold_record(
    *,
    reference_set: ReferenceSet,
    corpus: LabelledCorpus,
    layers: LayerSet,
    encoder: Encoder | None = None,
    target_false_positive_rate: float,
    path: Path | None = None,
) -> Mapping[str, Any]:
    """Select layer 3's operating point on the calibration partition and record the measurement.

    What the record is *about* is fixed by the two hashes: this corpus revision and this reference set.
    Editing either invalidates the record rather than silently redefining what was measured, which is
    what makes the threshold applicable at all (ADR-0005).

    This is the **one** place a threshold is produced, and it has to run exactly when the layer is
    closed for want of a record — which is why the guard here is on the encoder rather than on
    ``is_configured()``. Scoring works on a closed layer (that is how the measurement is taken); it is
    the *decision* that a closed layer refuses to make.
    """
    layer = layers.local_classifier
    target = path or THRESHOLD_RECORD_FILE
    resolved_encoder = encoder if encoder is not None else layer.encoder
    if encoder is None and not layer.is_configured():
        # No encoder at all: the environment's value names nothing this package can run, and there is
        # nothing to score with. Writing a threshold from a different method's numbers would be the
        # exact failure the intake rule exists to prevent.
        return {
            "written": False,
            "reason": str(layer.reason_if_unconfigured)
            or "no encoder is available to score with, so no threshold can be selected",
            "record_path": str(target),
        }

    calibration = [
        case for case in corpus.in_partition("calibration") if case.kind in SELECTION_KINDS
    ]
    if not calibration or not any(case.kind == "attack" for case in calibration):
        return {
            "written": False,
            "reason": (
                "the calibration partition needs both sides: a threshold selected without attack "
                "cases would be a threshold selected against nothing"
            ),
            "record_path": str(target),
        }

    clock = SystemClock()
    latencies: list[float] = []
    scores: list[tuple[float, bool]] = []
    for case in calibration:
        started = clock.monotonic_ms()
        similarity = layer.score(case.text).similarity
        latencies.append(clock.monotonic_ms() - started)
        scores.append((similarity, case.kind == "attack"))
    threshold = select_threshold(
        scores, target_false_positive_rate=target_false_positive_rate, margin=DEFAULT_MARGIN
    )

    measured = _classifier_confusion(layer, corpus, threshold=threshold)
    all_latencies: list[float] = []
    for case in corpus.user_messages():
        started = clock.monotonic_ms()
        layer.score(case.text)
        all_latencies.append(clock.monotonic_ms() - started)
    record = ThresholdRecord(
        method=resolved_encoder.method,
        encoder=f"{type(resolved_encoder).__name__}(dimensions={resolved_encoder.dimensions})",
        artifact_hash=resolved_encoder.artifact_hash or "",
        threshold=threshold,
        margin=DEFAULT_MARGIN,
        corpus_version=corpus.version,
        corpus_hash=corpus.content_hash,
        references_version=reference_set.version,
        references_hash=reference_set.content_hash,
        selected_on_partition="calibration",
        target_false_positive_rate=target_false_positive_rate,
        calibration=measured["calibration"],
        held_out=measured["held_out"],
        notes=(
            "Selected by referraleasy_guardrails.evaluation: the lowest threshold whose benign-block "
            f"rate on the calibration partition ({len(calibration)} cases) stays inside the "
            f"{target_false_positive_rate:.0%} budget. The score is a cosine similarity to the closest "
            "attack reference, not a calibrated probability, and the layer's block rule additionally "
            "requires that reference to be the closest one of all. "
            + (
                "This record is for the offline lexical rung; the pinned ADR-0005 encoder's threshold "
                "remains UNMEASURED in this environment (the 23 MB artifact is not present), and "
                "`mismatch()` refuses to apply a record to a different method."
                if resolved_encoder.method == HASHED_METHOD
                else "This record is for the pinned artifact named above."
            )
        ),
        measured_at=datetime.now(UTC).isoformat(timespec="seconds"),
        measured_latency_ms={
            "method": "per-call wall clock around score() on this machine",
            "calibration_mean": round(statistics.fmean(latencies), 4),
            "corpus_mean": round(statistics.fmean(all_latencies), 4),
            "corpus_max": round(max(all_latencies), 4),
            "corpus_cases": len(all_latencies),
        },
    )
    target.write_text(
        json.dumps(record.as_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {
        "written": True,
        "record_path": str(target),
        "method": record.method,
        "encoder": record.encoder,
        "threshold": record.threshold,
        "margin": record.margin,
        "corpus_hash": record.corpus_hash,
        "references_hash": record.references_hash,
        "calibration_false_positive_rate": measured["calibration_false_positive_rate"],
        "held_out_false_positive_rate": measured["held_out_false_positive_rate"],
        "held_out": measured["held_out"],
        "calibration_cases": len(calibration),
    }


def _classifier_confusion(
    layer: LocalClassifierGuardrail, corpus: LabelledCorpus, *, threshold: float
) -> Mapping[str, Any]:
    """Layer 3's block/ask counts per partition at a candidate threshold."""
    result: dict[str, Any] = {}
    for partition in ("calibration", "held_out"):
        cases = [case for case in corpus.in_partition(partition) if case.kind in SELECTION_KINDS]
        benign = [case for case in cases if case.kind in BENIGN_KINDS]
        attacks = [case for case in cases if case.kind == "attack"]
        flagged_benign = 0
        blocked_attacks = 0
        asked = 0
        for case in cases:
            decision, _, _ = _score_at(layer, case.text, threshold).decide()
            if decision is GuardrailDecision.block:
                if case.kind == "attack":
                    blocked_attacks += 1
                else:
                    flagged_benign += 1
            elif decision is GuardrailDecision.clarify:
                asked += 1
        result[partition] = {
            "cases": len(cases),
            "benign_cases": len(benign),
            "attack_cases": len(attacks),
            "benign_blocked": flagged_benign,
            "attacks_blocked": blocked_attacks,
            "asked": asked,
            "threshold": threshold,
        }
        result[f"{partition}_false_positive_rate"] = (
            round(flagged_benign / len(benign), 4) if benign else None
        )
    return result


def _score_at(layer: LocalClassifierGuardrail, text: str, threshold: float) -> ClassifierScore:
    """The layer's score with a candidate threshold applied, so selection and reporting agree."""
    return replace(layer.score(text), threshold=threshold, margin=DEFAULT_MARGIN)


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------


def _corpus_summary(corpus: LabelledCorpus) -> Mapping[str, Any]:
    """What was measured on, plus the check that it is not a corpus with one answer in it."""
    expectations = {case.expected.value for case in corpus.cases}
    partitions = corpus.partitions()
    return {
        "version": corpus.version,
        "content_hash": corpus.content_hash,
        "cases": len(corpus.cases),
        "by_kind": dict(sorted(corpus.counts().items())),
        "distinct_expected": sorted(expectations),
        "partitions": {partition: len(corpus.in_partition(partition)) for partition in partitions},
        "document_channel_cases": len(corpus.cases) - len(corpus.user_messages()),
        "vacuity_check": len(expectations) < 2 or len(partitions) < 2,
    }


def _unmeasured(*, live: bool) -> Sequence[Mapping[str, str]]:
    """What this run could not measure, and what would have to change for it to be measurable."""
    entries: list[Mapping[str, str]] = [
        {
            "item": "prompt injection through retrieved content",
            "reason": (
                "§9: input screening is not the only defence. The document-channel corpus case is "
                "screened by nothing in this package; the tool allowlist, minimum context, treating "
                "sources as data and server-side policy are what carry it."
            ),
        },
        {
            "item": "output guardrail rates",
            "reason": (
                "the output guardrail is a rule set over components, not a classifier over a corpus; "
                "it is covered by tests, one failing example per rule, rather than by a rate."
            ),
        },
    ]
    if not live:
        entries.insert(
            0,
            {
                "item": "layer 2 (openrouter decision model) verdict rates, latency and cost",
                "reason": (
                    "this run did not permit live calls (MODEL_BACKEND was forced offline), so no "
                    "client is built, no call is made and no provider tokens or cost exist to report. "
                    "The layer's rules are exercised by the test suite through an injected client; a "
                    "live call is a separate, deselected spike."
                ),
            },
        )
    return entries


def _render(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    corpus = report["corpus"]
    config = report["configuration"]
    lines.append(
        f"corpus {corpus['version']} hash {str(corpus['content_hash'])[:12]} ({corpus['cases']} cases, "
        f"{corpus['document_channel_cases']} of them document channel)"
    )
    lines.append(f"  by kind: {corpus['by_kind']}")
    lines.append(
        f"  vacuous: {corpus['vacuity_check']} (a corpus with one answer measures nothing)"
    )
    lines.append(
        f"model backend for this run: {config['model_backend']} "
        f"(live calls permitted: {config['live_calls_permitted']})"
    )
    for error in config.get("errors", []):
        lines.append(f"CONFIGURATION ERROR: {error}")
    lines.append("layers as the environment configures them:")
    for name, state in config["layers"].items():
        marker = "configured" if state["configured"] else "CLOSED"
        detail = state.get("method") or state.get("reason", "")
        lines.append(f"  {name:18} {marker:10} {detail}")
    record = config["layer3_record"]
    lines.append(
        "layer-3 measurement record: "
        + (
            f"method {record['method']} threshold {record['threshold']} "
            f"(speaks for this tree: {record['speaks_for_this_tree']})"
            if record["present"]
            else f"absent — {record['reason']}"
        )
    )
    lines.append(f"layer 3 encoder used for measurement below: {config['layer3_encoder_source']}")
    if config["layer3_encoder_note"]:
        lines.append(f"  note: {config['layer3_encoder_note']}")
    warmup = report["warmup"]
    if warmup["first_call_ms"]:
        lines.append(
            f"warm-up (discarded first call to each closed layer): {warmup['first_call_ms']} ms"
        )

    lines.append("")
    lines.append("per layer (each called directly on every user-channel case):")
    for name, entry in report["per_layer"].items():
        if name.endswith("_not_screened"):
            continue
        # The layer's own declaration first, then the methods its verdicts carried: layer 1 declares
        # none and answers with `regex-v1` on every case, and a table that printed "-" beside it would
        # read as "this layer has no method" rather than as "this layer's method is in its verdicts".
        label = entry["method"] or ", ".join(entry.get("methods") or []) or "-"
        lines.append(f"  {name} [{label}]")
        if entry["false_positives"] is None:
            lines.append(
                f"    not measured: {entry['not_screened_reason'] or 'no reason recorded'}"
            )
            continue
        lines.append(
            f"    screened {entry['cases_screened']}/{entry['cases_screened'] + entry['cases_not_screened']}"
            f", false positives {entry['false_positives']}/{entry['benign_cases']}"
            f", false negatives {entry['false_negatives']}"
            f", attacks not blocked {entry['attacks_not_blocked']}/{entry['attack_cases']}"
            f", over-asks {entry['over_asks']}"
            f", silent allows where a question was owed {entry['silent_allows_should_ask']}"
        )
        lines.append(
            f"    latency mean {entry['mean_latency_ms']} ms, max {entry['max_latency_ms']} ms, "
            f"provider calls {entry['provider_calls']}, tokens {entry['tokens']}, "
            f"cost {entry['cost_usd']} ({entry['cost_note']})"
        )
        lines.append(f"    confusion {entry['confusion']}")

    lines.append("")
    for profile, data in report["profiles"].items():
        lines.append(
            f"pipeline[{profile}]: decisions {data['decisions']}, complete {data['complete']}/{data['cases']}"
            f", writes refused {data['writes_refused']}, layer calls {data['layer_calls']}"
            f", skipped {data['layer_skipped']}, unavailable {data['layer_unavailable']}"
            f", mean total latency {data['mean_total_latency_ms']} ms"
        )
    comparison = report["profile_comparison"]
    lines.append(
        f"profile comparison: call delta {comparison['layer_call_delta']}, "
        f"decision changes {comparison['decision_changes']}, "
        f"complete {comparison['complete']}, writes refused {comparison['writes_refused']}"
    )
    lines.append(f"  {comparison['verdict']}")

    lines.append("")
    lines.append("NOT measured:")
    for entry in report["unmeasured"]:
        lines.append(f"  - {entry['item']}: {entry['reason']}")
    if "threshold_record" in report:
        written = report["threshold_record"]
        lines.append("")
        if written["written"]:
            lines.append(
                f"threshold record written to {written['record_path']}: method {written['method']}, "
                f"threshold {written['threshold']}, calibration FP rate "
                f"{written['calibration_false_positive_rate']} over {written['calibration_cases']} cases, "
                f"held-out FP rate {written['held_out_false_positive_rate']} over "
                f"{written['held_out']['benign_cases']} benign cases and "
                f"{written['held_out']['attacks_blocked']}/{written['held_out']['attack_cases']} "
                "attack cases blocked"
            )
        else:
            lines.append(f"threshold record NOT written: {written['reason']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m referraleasy_guardrails.evaluation",
        description="Measure §9's guardrail layers on the labelled corpus.",
    )
    parser.add_argument(
        "--layer3",
        choices=("auto", "env", "offline", "onnx"),
        default="auto",
        help="which layer-3 encoder to measure with (default: auto — the environment's choice, "
        "falling back to the offline rung and saying so in the report)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="permit real OpenRouter calls (spends money); without it the model backend is forced "
        "offline for this run",
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="select layer 3's threshold on the calibration partition and write "
        "data/threshold_record.json",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="repository root to read .env from (default: the repository this file is installed in)",
    )
    args = parser.parse_args(argv)

    settings: GuardrailSettings
    problems: list[str] = []
    try:
        settings = (
            GuardrailSettings.from_env(repo_root=args.repo_root)
            if args.repo_root
            else GuardrailSettings.from_env()
        )
    except ConfigError as error:
        # The configuration is not runnable. Report it as the finding it is, and still measure what
        # can be measured — a broken pin must not hide the numbers that do exist.
        problems.append(str(error))
        raw = parse_env_file((args.repo_root or _default_root()) / DEFAULT_ENV_FILE)
        settings = GuardrailSettings(
            local_model=raw.get("GUARDRAIL_LOCAL_MODEL", "").strip(),
            local_threshold=raw.get("GUARDRAIL_LOCAL_THRESHOLD", "").strip(),
            decision_model=raw.get("GUARDRAIL_DECISION_MODEL", "").strip(),
            model_backend="offline",
            env=raw,
        )

    report = measure(
        settings=settings, layer3=args.layer3, live=args.live, calibrate=args.calibrate
    )
    if problems:
        report = dict(report)
        report["configuration"] = dict(report["configuration"]) | {"errors": problems}
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        if problems:
            print("the guardrail configuration is not runnable:")
            for problem in problems:
                print(f"  - {problem}")
        print(_render(report))
    if report["corpus"]["vacuity_check"]:
        print("\nREFUSING to present these numbers: the corpus is vacuous.", file=sys.stderr)
        return 2
    return 0


def _default_root() -> Path:
    return Path(__file__).resolve().parents[4]


if __name__ == "__main__":  # pragma: no cover - the CLI entry point
    raise SystemExit(main())
