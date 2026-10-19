"""ADR-0005's intake rule, implemented: a threshold is only usable together with its measurement (§9).

§9's requirement for layer 3 is a list of things that must be *recorded* — the method, the artifact
hash, the thresholds — and a recorded number is only worth recording if something refuses to use it
without its record. This file is about that refusal. It tests four of them separately:

* **no record** — an unmeasured default is what ADR-0005 forbids, so a layer with no measurement is
  closed rather than tuned by hand;
* **the wrong record** — a measurement taken with another method, against another corpus, against
  another reference set or from another artifact does not speak for this layer, and
  :meth:`ThresholdRecord.mismatch` says which of the four it is;
* **the environment's own number** — ``GUARDRAIL_LOCAL_THRESHOLD`` must agree with the recorded
  threshold. Written as a test because it is the loophole the intake rule exists to close: an operator
  could otherwise raise the threshold to 0.99 (turning the layer off) or lower it to 0.10 (turning
  every patient message into a block) without touching the measurement;
* **the artifact's bytes** — verified against the pin before anything is loaded, and a mismatch
  refuses.

Two tests here read the *committed* data rather than a fixture, on purpose: the record shipped with
the package must speak for the corpus shipped with it, and its sample sizes must be the sizes of that
corpus's labelled cases. A test with its own numbers cannot notice those two files drifting apart.

The encoder rung's threshold is a separate matter, and it is not tested as if it existed. ADR-0005's
23 MB artifact is not in this checkout and ``onnxruntime`` is not installed, so the encoder's operating
point is UNMEASURED here; what is tested is the *closed* rung, including that a missing runtime closes
the layer with a reason a build can carry rather than raising through the assembly path.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest
from guardrail_fixtures import REPO_ROOT, corpus, references, threshold_record

from referraleasy_guardrails.build import build_layers
from referraleasy_guardrails.config import GuardrailProfile, GuardrailSettings, parse_env_file
from referraleasy_guardrails.corpus import ArtifactPin, sha256_file
from referraleasy_guardrails.layer_classifier import (
    OFFLINE_METHOD_PIN,
    ArtifactHashMismatch,
    EncoderUnavailable,
    HashedReferenceEncoder,
    LocalClassifierGuardrail,
    OnnxEncoder,
    load_threshold_record,
    select_threshold,
)
from referraleasy_guardrails.types import is_unavailable

#: The method the committed record was measured with. The lexical rung's name, never the encoder's.
LEXICAL = "hashed-char-ngram-v1"
#: The ONNX rung's method string, in the form an encoder reports it (``minilm-onnx:<sha prefix>``).
ENCODER_METHOD = "minilm-onnx:4278337fd0ff"

#: The encoder's artifact hash, as ADR-0005 transcribes it. A test constant, not a credential.
ENCODER_ARTIFACT = "4278337fd0ff3c68bfb6291042cad8ab363e1d9fbc43dcb499fe91c871902474"

_MISSING: Any = object()


def build(
    *,
    pin: str = OFFLINE_METHOD_PIN,
    encoder: Any = _MISSING,
    record: Any = _MISSING,
    env_threshold: str = "",
    closed_reason: str = "",
) -> LocalClassifierGuardrail:
    """``LocalClassifierGuardrail.build`` with everything present unless a test takes it away.

    A sentinel rather than a default value, because half of these tests are about passing ``None``:
    ``record=None`` has to mean "no measurement exists", and a ``None`` default could not tell that
    apart from "the caller did not care".
    """
    return LocalClassifierGuardrail.build(
        pin=pin,
        encoder=HashedReferenceEncoder() if encoder is _MISSING else encoder,
        references=references(),
        record=threshold_record() if record is _MISSING else record,
        corpus=corpus(),
        env_threshold=env_threshold,
        closed_reason=closed_reason,
    )


def fake_pin(
    directory: Path, *, payload: bytes = b"not really a model", sha256: str = ""
) -> ArtifactPin:
    """A pin over bytes a test wrote, so only the loader can refuse it.

    ``sha256`` defaults to the digest of ``payload``; passing another value is how a test produces the
    wrong-bytes case without touching the file.
    """
    graph = directory / "model.onnx"
    graph.write_bytes(payload)
    (directory / "tokenizer.json").write_bytes(b"{}")
    return ArtifactPin(
        model="org/test-model",
        revision="0" * 40,
        file=graph.name,
        sha256=sha256 or sha256_file(graph),
        bytes=len(payload),
        tokenizer_file="tokenizer.json",
        tokenizer_git_blob_sha1="0" * 40,
        adr="ADR-0005",
    )


# ---------------------------------------------------------------------------
# The record shipped with the package, against the data shipped with it
# ---------------------------------------------------------------------------


def test_the_committed_record_speaks_for_the_committed_data() -> None:
    """The two committed files must describe the same measurement, and this is the only place that can say so.

    A record whose hashes no longer match its corpus is the exact failure ADR-0005's intake rule is
    about: the number would still look measured, and it would be about a revision that no longer exists.
    """
    record = load_threshold_record()
    assert record is not None, (
        "the package ships a recorded threshold; this file is where it is used"
    )
    assert record.method == LEXICAL
    assert record.mismatch(method=LEXICAL, corpus=corpus(), references=references()) is None
    assert 0.0 < record.threshold < 1.0
    assert record.selected_on_partition == "calibration"
    assert record.target_false_positive_rate == pytest.approx(0.05)
    assert record.measured_at
    # The stdlib rung has no artifact, and the record says that rather than leaving the field to be
    # read as "an artifact we forgot to name".
    assert record.artifact_hash == ""
    assert "lexical" in record.notes


@pytest.mark.parametrize("partition", ["calibration", "held_out"])
def test_the_record_s_sample_sizes_are_the_labelled_cases_of_those_partitions(
    partition: str,
) -> None:
    """Counts checked against the corpus rather than restated, so the two files cannot drift.

    A threshold is selected on the calibration partition and reported on the held-out one, so the
    record's ``cases`` / ``benign_cases`` / ``attack_cases`` have to be the sizes of *those* sets — and
    the sets are the cases carrying a binary label, because a threshold separates attacks from benign
    messages and an emergency is neither.
    """
    record = load_threshold_record()
    assert record is not None
    measured = record.calibration if partition == "calibration" else record.held_out
    labelled = [
        case for case in corpus().in_partition(partition) if case.is_attack or case.is_benign
    ]
    assert measured["cases"] == len(labelled)
    assert measured["attack_cases"] == sum(1 for case in labelled if case.is_attack)
    assert measured["benign_cases"] == sum(1 for case in labelled if case.is_benign)


def test_the_recorded_latency_says_how_many_cases_it_was_taken_over() -> None:
    """A mean latency is only a number about something. It is stated, and it is the user messages."""
    record = load_threshold_record()
    assert record is not None
    assert record.measured_latency_ms["corpus_cases"] == len(corpus().user_messages())
    assert record.measured_latency_ms["corpus_mean"] > 0.0


def test_a_placeholder_or_absent_record_reads_as_no_record(tmp_path: Path) -> None:
    """``CHANGEME_MEASURED_THRESHOLD`` and a missing file both mean *not measured*, never *use a default*."""
    placeholder = tmp_path / "placeholder.json"
    placeholder.write_text(
        json.dumps({"method": LEXICAL, "threshold": "CHANGEME_MEASURED_THRESHOLD"})
    )
    assert load_threshold_record(placeholder) is None
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"method": LEXICAL}))
    assert load_threshold_record(empty) is None
    assert load_threshold_record(tmp_path / "no-such-file.json") is None


# ---------------------------------------------------------------------------
# The four ways a record can be the wrong record
# ---------------------------------------------------------------------------


def test_a_record_for_another_method_is_refused_and_the_layer_names_both() -> None:
    """A threshold measured for one method does not transfer to another, whatever the number is."""
    record = threshold_record(method="some-other-method-v2")
    mismatch = record.mismatch(method=LEXICAL, corpus=corpus(), references=references())
    assert mismatch is not None
    assert "some-other-method-v2" in mismatch and LEXICAL in mismatch
    layer = build(record=record)
    assert layer.is_configured() is False
    assert "some-other-method-v2" in layer.reason_if_unconfigured


def test_a_record_from_another_corpus_is_refused_with_both_hashes() -> None:
    """Editing a corpus case invalidates every threshold measured on it; the reason carries both hashes."""
    edited = replace(threshold_record(), corpus_hash="0" * 64)
    mismatch = edited.mismatch(method=LEXICAL, corpus=corpus(), references=references())
    assert mismatch is not None
    assert "0" * 12 in mismatch and corpus().content_hash[:12] in mismatch
    layer = build(record=edited)
    assert layer.is_configured() is False
    assert "re-measure rather than re-interpret" in layer.reason_if_unconfigured


def test_a_record_from_another_reference_set_is_refused() -> None:
    """The reference set is half of what a similarity means, so it is hashed and compared too."""
    edited = replace(threshold_record(), references_hash="f" * 64)
    mismatch = edited.mismatch(method=LEXICAL, corpus=corpus(), references=references())
    assert mismatch is not None
    assert references().content_hash[:12] in mismatch


def test_a_record_from_another_artifact_is_refused() -> None:
    """A threshold measured against one set of bytes and applied to another is a number about neither."""
    record = threshold_record(method=ENCODER_METHOD, artifact_hash=ENCODER_ARTIFACT)
    same = record.mismatch(
        method=ENCODER_METHOD,
        corpus=corpus(),
        references=references(),
        artifact_hash=ENCODER_ARTIFACT,
    )
    assert same is None
    mismatch = record.mismatch(
        method=ENCODER_METHOD,
        corpus=corpus(),
        references=references(),
        artifact_hash="abcd" + "1" * 60,
    )
    assert mismatch is not None
    assert "4278337f" in mismatch and "abcd" in mismatch


def test_a_record_that_measured_no_artifact_does_not_claim_one() -> None:
    """The empty artifact hash is a statement, and the method is what keeps it honest.

    A record with ``artifact_hash == ""`` is a record of the stdlib rung. It is refused by the method
    check the moment it is offered to the ONNX rung (the two method strings can never be equal), and
    when it is offered to the rung it *is* about there is no artifact to compare — so the comparison is
    skipped rather than satisfied by an invented empty hash.
    """
    lexical = threshold_record(artifact_hash="")
    assert (
        lexical.mismatch(
            method=LEXICAL, corpus=corpus(), references=references(), artifact_hash=None
        )
        is None
    )
    offered = lexical.mismatch(
        method=ENCODER_METHOD,
        corpus=corpus(),
        references=references(),
        artifact_hash=ENCODER_ARTIFACT,
    )
    assert offered is not None
    assert "does not transfer" in offered


# ---------------------------------------------------------------------------
# The environment's number must be the recorded number
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env_threshold", ["0.5", "0.50", ""])
def test_an_environment_threshold_equal_to_the_record_leaves_the_layer_open(
    env_threshold: str,
) -> None:
    """Equal is equal: ``0.5``, ``0.50`` and unset all describe the recorded operating point."""
    layer = build(env_threshold=env_threshold)
    assert layer.is_configured() is True
    assert layer.threshold == pytest.approx(0.5)


def test_an_environment_threshold_that_disagrees_closes_the_layer() -> None:
    """The loophole the intake rule closes, tested as a closure rather than as a warning.

    An operator who wants a different operating point has to re-measure; the environment can only
    restate the measurement. Both numbers appear in the reason, because an operator acting on it needs
    to know which value to change.
    """
    layer = build(env_threshold="0.99")
    assert layer.is_configured() is False
    assert layer.method == ""
    reason = layer.reason_if_unconfigured
    assert "GUARDRAIL_LOCAL_THRESHOLD=0.99" in reason
    assert "0.5" in reason
    with pytest.raises(RuntimeError) as raised:
        layer.screen(corpus().user_messages()[0].request(), "hello")
    assert is_unavailable(raised.value)
    assert "GUARDRAIL_LOCAL_THRESHOLD" in str(raised.value)


def test_an_environment_threshold_that_is_not_a_number_closes_the_layer() -> None:
    """A value that cannot be compared is not a threshold, and it is not ignored either."""
    layer = build(env_threshold="high")
    assert layer.is_configured() is False
    assert "GUARDRAIL_LOCAL_THRESHOLD=high" in layer.reason_if_unconfigured


def test_the_checkout_s_own_environment_agrees_with_the_record() -> None:
    """The deployed configuration, read from ``.env``, is asserted rather than described.

    This test has been through both states and the history is the point. It was written to pin a
    *mismatch*: this checkout set ``GUARDRAIL_LOCAL_THRESHOLD=0.50`` while the committed record
    measured ``0.32`` for the rung the environment names, so under the intake rule layer 3 was closed
    in this very deployment. Its docstring said that when the two were reconciled it would fail first
    and "should then be updated to assert the agreement rather than deleted" — and it did, on the
    first full-suite run after the guardrail wiring reconciled them.

    So it now asserts the agreement, in the direction that matters: the layer is **open**, and it is
    open for the reason it should be — the environment names the same method the record was measured
    for, at the same threshold. A future edit to either number fails here, which is what keeps a
    deployment from silently losing its layer 3 to a typo in an env file nothing else reads.
    """
    values = parse_env_file(REPO_ROOT / ".env")
    threshold = values.get("GUARDRAIL_LOCAL_THRESHOLD", "").strip()
    record = load_threshold_record()
    assert record is not None
    if not threshold:
        pytest.skip(
            "this checkout sets no GUARDRAIL_LOCAL_THRESHOLD, so there is nothing to compare"
        )
    assert float(threshold) == record.threshold, (
        f"the .env threshold {threshold} and the record's {record.threshold} disagree; under the "
        "intake rule that closes layer 3 in this deployment rather than falling back to the measured "
        "value"
    )
    settings = GuardrailSettings(
        profile=GuardrailProfile.THREE_STAGE,
        decision_model=values.get("GUARDRAIL_DECISION_MODEL", ""),
        local_model=values.get("GUARDRAIL_LOCAL_MODEL", ""),
        local_threshold=threshold,
        model_backend=values.get("MODEL_BACKEND", "offline"),
    )
    layers = build_layers(
        settings,
        encoder=HashedReferenceEncoder(),
        record=record,
        references=references(),
        corpus=corpus(),
    )
    assert layers.local_classifier.is_configured() is True, (
        f"layer 3 is closed in this checkout: {layers.local_classifier.reason_if_unconfigured}"
    )
    # Layer 3's method is the record's, which is what makes the threshold applicable rather than an
    # unmeasured default (§9/ADR-0005).
    assert layers.local_classifier.method == record.method
    # Layer 2 is deliberately *not* asserted here: this test reads `.env` for layer 3's two values,
    # and whether the decision model opens also depends on a key this settings object is not given.
    # `test_the_checkout_s_own_environment_is_checked_against_the_record`'s sibling coverage for that
    # is the live check — `scripts/health.py` and `/healthz`, which reported both layers configured
    # once this reconciliation landed.


# ---------------------------------------------------------------------------
# Every closed layer says which layer it is and why
# ---------------------------------------------------------------------------


def test_every_closed_layer_reports_the_reason_and_never_a_substitute_method() -> None:
    """One table of closures, asserted together, because "layer 3 is off" is not actionable.

    Each entry is a different cause and a different sentence. A layer reporting ``""`` as its method
    while holding an encoder is the quiet failure this guards against: a report would print the
    encoder's name beside a layer that ran nothing.
    """
    cases: dict[str, LocalClassifierGuardrail] = {
        "no encoder for the pin": build(
            encoder=None, pin="sentence-transformers/all-MiniLM-L6-v2@abc"
        ),
        "no measurement": build(record=None),
        "another method": build(record=threshold_record(method="other-v1")),
        "another corpus": build(record=replace(threshold_record(), corpus_hash="0" * 64)),
        "another artifact": build(
            encoder=_EncoderWithArtifact(),
            record=threshold_record(method=ENCODER_METHOD, artifact_hash="a" * 64),
        ),
        "the environment disagrees": build(env_threshold="0.1"),
        "the operator said so": build(closed_reason="the operator disabled layer 3 for this run"),
    }
    for label, layer in cases.items():
        assert layer.is_configured() is False, label
        assert layer.method == "", label
        assert layer.reason_if_unconfigured, label
        with pytest.raises(RuntimeError) as raised:
            layer.screen(corpus().user_messages()[0].request(), "hello")
        assert is_unavailable(raised.value), label
        assert layer.reason_if_unconfigured in str(raised.value), label
    assert (
        "no local classifier is available" in cases["no encoder for the pin"].reason_if_unconfigured
    )
    assert "no threshold measurement is recorded" in cases["no measurement"].reason_if_unconfigured
    assert cases["the operator said so"].reason_if_unconfigured.startswith("the operator disabled")


def test_a_stated_refusal_wins_over_a_derived_one() -> None:
    """An explicit reason is the operator's, and it is not overwritten by the diagnosis underneath."""
    layer = build(closed_reason="maintenance: layer 3 is off until the new record is measured")
    assert (
        layer.reason_if_unconfigured
        == "maintenance: layer 3 is off until the new record is measured"
    )
    # The record stays visible on the closed layer: a report can say what it *would* have run with,
    # without that implying it ran.
    assert layer.record is not None
    assert layer.record.threshold == pytest.approx(0.5)


def test_a_threshold_record_is_frozen_so_it_cannot_be_retuned_in_place() -> None:
    """The record is data, not configuration: a field that could be assigned would be tunable.

    A mutable record would let a caller widen the band by writing to the object the layer is already
    holding — which is the same loophole as a hand-set environment threshold, arriving through a
    different door.
    """
    record = threshold_record()
    with pytest.raises(FrozenInstanceError):
        record.threshold = 0.99  # type: ignore[misc]


class _EncoderWithArtifact:
    """An encoder claiming an ONNX artifact hash, so a record's own hash has something to disagree with."""

    method = ENCODER_METHOD
    dimensions = 384
    artifact_hash = ENCODER_ARTIFACT

    def encode(self, texts: Any) -> tuple[tuple[float, ...], ...]:
        return tuple((1.0,) for _text in texts)


# ---------------------------------------------------------------------------
# Selecting a threshold on the calibration partition
# ---------------------------------------------------------------------------


#: Five benign cases and one attack, chosen so the arithmetic is exact: one benign case sits at 0.90
#: and the attack at 0.95, and everything else is far below any candidate's band.
SCORES: tuple[tuple[float, bool], ...] = (
    (0.90, False),
    (0.20, False),
    (0.10, False),
    (0.05, False),
    (0.00, False),
    (0.95, True),
)


def test_the_selected_threshold_is_the_lowest_one_inside_the_budget() -> None:
    """Within the budget the rule keeps lowering the threshold, and the near-miss benign case stops it.

    At 0.86 the benign case at 0.90 stays below ``0.86 + 0.05``; one step lower it is flagged and the
    budget is exceeded. Removing that case lets the selection drop to 0.16, which is how the test shows
    the *benign* score — not the attack's — is what binds.
    """
    assert select_threshold(SCORES, target_false_positive_rate=0.0, margin=0.05) == pytest.approx(
        0.86
    )
    without_near_miss = tuple(entry for entry in SCORES if entry != (0.90, False))
    assert select_threshold(
        without_near_miss, target_false_positive_rate=0.0, margin=0.05
    ) == pytest.approx(0.16)


def test_a_looser_false_positive_budget_selects_a_lower_threshold() -> None:
    """Monotone in the budget, which is what makes the number a *policy* rather than a preference."""
    strict = select_threshold(SCORES, target_false_positive_rate=0.0, margin=0.05)
    looser = select_threshold(SCORES, target_false_positive_rate=1 / 3, margin=0.05)
    assert looser < strict
    assert looser == pytest.approx(0.16)


def test_the_attack_scores_do_not_move_the_threshold() -> None:
    """The selection spends the false-positive budget and nothing else — the misses are the report's job.

    Two runs with identical benign cases and opposite attacks select the same number: the rule is not
    maximising anything, and saying so is what keeps a *selected* threshold from being read as an
    optimised one.
    """
    invisible_attacks = tuple(
        (0.0, True) if is_attack else (score, is_attack) for score, is_attack in SCORES
    )
    assert select_threshold(
        SCORES, target_false_positive_rate=0.0, margin=0.05
    ) == select_threshold(invisible_attacks, target_false_positive_rate=0.0, margin=0.05)


def test_with_no_benign_cases_the_most_conservative_threshold_is_returned() -> None:
    """A degenerate measurement is answered with the conservative end of the range, not with confidence.

    With no benign case to spend a budget on, nothing was measured; the returned threshold is the top
    of the scanned range, which asks a human about everything rather than blocking on an empty sample.
    """
    assert select_threshold([(0.95, True)], target_false_positive_rate=0.5) == pytest.approx(0.99)


# ---------------------------------------------------------------------------
# The artifact's bytes, verified before anything is loaded
# ---------------------------------------------------------------------------


def test_a_missing_artifact_is_a_refusal_that_names_what_is_missing(tmp_path: Path) -> None:
    """Models are never committed, so absence is the normal case and it has to read like one."""
    for name in ("model.onnx", "tokenizer.json"):
        pin = fake_pin(tmp_path)  # both files back, then take one away
        (tmp_path / name).unlink()
        with pytest.raises(EncoderUnavailable) as raised:
            OnnxEncoder.load(pin, tmp_path)
        assert "not present" in str(raised.value)
        assert pin.pin in str(raised.value)
        assert "never committed" in str(raised.value)


def test_bytes_that_are_not_the_pinned_bytes_are_refused_with_both_hashes(tmp_path: Path) -> None:
    """The hash is the contract (ADR-0005): a file that is not the pinned file is not loaded at all."""
    expected = "0" * 64
    pin = fake_pin(tmp_path, payload=b"a model that is not the pinned one", sha256=expected)
    with pytest.raises(ArtifactHashMismatch) as raised:
        OnnxEncoder.load(pin, tmp_path)
    error = raised.value
    assert isinstance(error, EncoderUnavailable), "a mismatch is a way a layer is closed"
    assert error.expected == expected
    assert error.actual == sha256_file(tmp_path / "model.onnx")
    assert expected[:12] in str(error) and error.actual[:12] in str(error)
    assert str(error).endswith("refusing to load it")


def test_a_hash_that_matches_still_refuses_when_the_artifact_cannot_run(tmp_path: Path) -> None:
    """The rung is closed rather than substituted, and closed *at load time* rather than at the first message.

    The bytes here hash to the pin, so verification passes and the loader proceeds. What it finds is
    not a model, and this checkout has no ``onnxruntime`` — either way the layer must be closed with a
    reason the assembly path can carry, rather than raising a traceback through the build.
    """
    pin = fake_pin(tmp_path)
    with pytest.raises(EncoderUnavailable) as raised:
        OnnxEncoder.load(pin, tmp_path)
    assert not isinstance(raised.value, ArtifactHashMismatch)
    message = str(raised.value)
    assert "runtime" in message or "would not load" in message


def test_the_pinned_rung_without_its_artifact_closes_the_layer_instead_of_the_build(
    tmp_path: Path,
) -> None:
    """The end-to-end consequence, on the path this checkout actually takes.

    The environment names ADR-0005's pin and the artifact directory is empty, which is the state of
    every checkout that has not downloaded 23 MB. ``build_layers`` must return three layers and a
    reason — a closed layer the health endpoint can report — and not raise through the assembly path.
    """
    settings = GuardrailSettings(
        local_model="sentence-transformers/all-MiniLM-L6-v2@1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
        local_threshold="",
        model_backend="offline",
    )
    layers = build_layers(settings, artifact_dir=tmp_path)
    assert layers.local_classifier.is_configured() is False
    reason = layers.local_classifier.reason_if_unconfigured
    assert "not present" in reason
    assert "1110a243" in reason


def test_the_shipped_pin_is_adr_0005_s_transcription() -> None:
    """The pin table is a transcription of the ADR, so the two numbers are asserted against it.

    If the table and the ADR disagree, every artifact verified through this table is verified against
    the wrong bytes — and the disagreement would be invisible until a model behaved differently.
    """
    pin = ArtifactPin.find(
        "sentence-transformers/all-MiniLM-L6-v2@1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
    )
    assert pin is not None
    assert pin.sha256 == ENCODER_ARTIFACT
    assert pin.bytes == 23026053
    assert pin.file == "onnx/model_qint8_avx512_vnni.onnx"
    assert pin.tokenizer_file == "tokenizer.json"
    assert pin.adr == "ADR-0005"
    assert ArtifactPin.find("openai/gpt-5-nano") is None
    # The offline rung is a name for an implementation, not an artifact: nothing to download.
    assert ArtifactPin.find(OFFLINE_METHOD_PIN) is None
