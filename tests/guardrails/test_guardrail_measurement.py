"""§9's four numbers per layer, on the labelled corpus — measured, and refusing to substitute (§9).

§9 requires *"Measure false positives/false negatives and added latency/cost per layer"* on a
labelled corpus, and it requires the conditional profile to be **compared** rather than assumed better.
:mod:`referraleasy_guardrails.evaluation` produces those numbers; this file is what keeps them honest,
in four ways.

**The counts are recomputed here.** Layer 1's and layer 3's entries are checked against a tally this
file takes itself, with the layer built here from the committed record and the committed references,
over the same corpus. A measurement whose only witness is its own code is a claim about a function,
not about a layer.

**What could not be measured is asserted as absent.** Layer 2 under the offline profile is reported as
unmeasured — not as zero false positives, and not as a cost of zero — and the run is asserted to force
the offline backend even when the checkout's environment says ``live``, with the provider client
patched to raise if anything constructs one. The ``unmeasured`` list is checked to name layer 2, the
document channel and the output guardrail, because §9's point that *"input screening is not the only
defense against prompt injection"* is a statement that belongs where the numbers are.

**The comparison is computed, not asserted.** The profile comparison's deltas, decision changes and
totals are recomputed from the two profile runs in the same report, and the sentence that describes
the comparison is checked against the numbers it names.

**The calibration run is reproducible without being destructive.** ``--calibrate`` is exercised
against a record path under ``tmp_path``, and the committed ``data/threshold_record.json`` is asserted,
byte for byte, to be the same file afterwards.

Nothing here makes a network call. ``MODEL_BACKEND`` is ``live`` in this checkout's ``.env``, which is
exactly why every run below passes explicit settings and why the live path is a deselected spike.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from guardrail_fixtures import corpus, references

from referraleasy_contracts.generated.common import GuardrailDecision
from referraleasy_guardrails.config import GuardrailProfile, GuardrailSettings
from referraleasy_guardrails.evaluation import (
    BENIGN_KINDS,
    THRESHOLD_RECORD_FILE,
    main,
    measure,
)
from referraleasy_guardrails.layer_classifier import (
    HASHED_METHOD,
    OFFLINE_METHOD_PIN,
    ClassifierScore,
    HashedReferenceEncoder,
    LocalClassifierGuardrail,
    load_threshold_record,
)
from referraleasy_guardrails.layer_regex import METHOD as REGEX_METHOD
from referraleasy_guardrails.layer_regex import RegexGuardrail
from referraleasy_guardrails.pipeline import MEASURED_PER_LAYER

MESSAGE_CAP = 2000

#: The two sides of the corpus's labelling, restated here rather than imported: a test that took its
#: definitions from the code it checks would witness a coincidence. ``BENIGN_KINDS`` is the report's
#: copy of the same rule, and the two are asserted equal so a drift is a failure rather than a silence.
BENIGN = frozenset({"benign", "short_reply_valid", "profanity_genuine"})
MUST_BLOCK = frozenset({"attack", "profanity_abuse"})


def settings(**overrides: Any) -> GuardrailSettings:
    """Explicit settings for a measurement run: offline, nothing substituted, nothing reached."""
    return GuardrailSettings(**overrides)


def offline_rung_settings(**overrides: Any) -> GuardrailSettings:
    """The deployment that names the offline rung and agrees with the recorded threshold."""
    base: dict[str, Any] = {"local_model": OFFLINE_METHOD_PIN, "local_threshold": "0.32"}
    base.update(overrides)
    return GuardrailSettings(**base)


@pytest.fixture(scope="module")
def report() -> Mapping[str, Any]:
    """The offline measurement over the committed corpus, computed once for the whole module."""
    return measure(settings=GuardrailSettings(), layer3="offline")


def layer1_entry(report: Mapping[str, Any]) -> Mapping[str, Any]:
    return report["per_layer"]["regex"]


def layer3_entry(report: Mapping[str, Any]) -> Mapping[str, Any]:
    return report["per_layer"]["local_classifier"]


def tally(layer: Any) -> dict[str, Any]:
    """Every count the report gives, recomputed here from the corpus and the layer's own verdicts.

    The definitions are written out rather than imported: a benign case is one where refusing or
    over-asking costs a patient, a must-block case is one where not refusing is the failure, a
    false negative is one of those that came back ``allow``, a silent allow is a case the corpus
    wanted a question about that got a plain allow.
    """
    cases = corpus().user_messages()
    counts: dict[str, Any] = {
        "false_positives": 0,
        "false_negatives": 0,
        "silent_allows_should_ask": 0,
        "over_asks": 0,
        "attacks_not_blocked": 0,
        "attack_cases": 0,
        "benign_cases": 0,
    }
    confusion: dict[str, int] = {}
    methods: set[str] = set()
    for case in cases:
        verdict = layer.screen(case.request(), case.request().masked_text)
        if verdict.method:
            methods.add(verdict.method)
        key = f"{case.expected.value}->{verdict.decision.value}"
        confusion[key] = confusion.get(key, 0) + 1
        if case.kind in BENIGN:
            counts["benign_cases"] += 1
            if verdict.decision is GuardrailDecision.block:
                counts["false_positives"] += 1
            elif verdict.decision is GuardrailDecision.clarify:
                counts["over_asks"] += 1
        if case.kind in MUST_BLOCK:
            counts["attack_cases"] += 1
            if verdict.decision is not GuardrailDecision.block:
                counts["attacks_not_blocked"] += 1
                if verdict.decision is GuardrailDecision.allow:
                    counts["false_negatives"] += 1
        elif (
            case.expected is GuardrailDecision.block and verdict.decision is GuardrailDecision.allow
        ):
            counts["false_negatives"] += 1
        if (
            case.expected is GuardrailDecision.clarify
            and verdict.decision is GuardrailDecision.allow
        ):
            counts["silent_allows_should_ask"] += 1
    counts["confusion"] = dict(sorted(confusion.items()))
    counts["methods"] = sorted(methods)
    counts["cases_screened"] = len(cases)
    return counts


def independent_layer1() -> RegexGuardrail:
    return RegexGuardrail(max_chars=MESSAGE_CAP)


def independent_layer3() -> LocalClassifierGuardrail:
    """Layer 3 as the deployment would build it for the offline rung, from the committed record."""
    record = load_threshold_record()
    assert record is not None and record.method == HASHED_METHOD
    return LocalClassifierGuardrail.build(
        pin=OFFLINE_METHOD_PIN,
        encoder=HashedReferenceEncoder(),
        references=references(),
        record=record,
        corpus=corpus(),
        env_threshold=str(record.threshold),
        closed_reason="",
    )


# ---------------------------------------------------------------------------
# The run is offline, and it says so rather than substituting
# ---------------------------------------------------------------------------


def test_the_run_forces_the_offline_backend_even_when_the_environment_says_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A measurement must not spend money, so the deployment's `live` wish is replaced — visibly."""

    def explode(*args: object, **kwargs: object) -> object:
        raise AssertionError("a provider client was constructed by a measurement run")

    monkeypatch.setattr("referraleasy_guardrails.build.OpenRouterDecisionModelClient", explode)
    live_settings = GuardrailSettings(
        model_backend="live",
        decision_model="openai/gpt-5-nano",
        openrouter_api_key="not-a-real-key-this-run-must-not-need-it",
    )
    report = measure(settings=live_settings, layer3="offline")

    assert report["configuration"]["model_backend"] == "offline"
    assert report["configuration"]["live_calls_permitted"] is False
    assert report["configuration"]["layers"]["decision_model"]["configured"] is False
    assert "MODEL_BACKEND=offline" in report["configuration"]["layers"]["decision_model"]["reason"]


def test_layer_two_offline_has_no_rate_to_report_rather_than_a_zero(
    report: Mapping[str, Any],
) -> None:
    """Not measured is not zero: a rate of zero over no cases would read as a clean layer."""
    entry = report["per_layer"]["decision_model"]
    cases = len(corpus().user_messages())
    assert entry["configured"] is False
    assert entry["cases_screened"] == 0
    assert entry["cases_not_screened"] == cases
    assert entry["confusion"] == {}
    assert entry["false_positives"] is None
    assert entry["false_negatives"] is None
    assert entry["mean_latency_ms"] is None and entry["max_latency_ms"] is None
    assert entry["provider_calls"] == 0
    assert entry["tokens"] is None
    assert entry["cost_usd"] is None
    assert "zero network calls" in entry["not_screened_reason"]
    assert entry["methods"] == [], "a layer that ran nothing produced no verdict and no method"
    # It declares the method it would have run with, which is what the reason beside it qualifies.
    assert entry["method"] == "openrouter"


def test_layer_one_s_numbers_are_recomputed_from_the_corpus_by_this_test(
    report: Mapping[str, Any],
) -> None:
    """The witness is a second implementation: same corpus, same layer, this test's own tally."""
    assert BENIGN_KINDS == BENIGN, "the report's benign vocabulary and this file's have to agree"
    mine = tally(independent_layer1())
    entry = layer1_entry(report)
    for key, value in mine.items():
        assert entry[key] == value, (
            f"regex {key}: the report says {entry[key]!r}, this test {value!r}"
        )
    assert entry["configured"] is True
    assert entry["cases_screened"] == len(corpus().user_messages())
    # The layer declares no method of its own; the method is the one its verdicts carried.
    assert entry["method"] == ""
    assert entry["methods"] == [REGEX_METHOD]
    assert entry["provider_calls"] == 0 and entry["cost_usd"] is None
    assert "no provider" in entry["cost_note"]
    assert entry["mean_latency_ms"] is not None and entry["max_latency_ms"] is not None


def test_layer_three_s_numbers_are_recomputed_from_the_recorded_operating_point(
    report: Mapping[str, Any],
) -> None:
    mine = tally(independent_layer3())
    entry = layer3_entry(report)
    for key, value in mine.items():
        assert entry[key] == value, (
            f"layer 3 {key}: the report says {entry[key]!r}, this test {value!r}"
        )
    assert entry["configured"] is True
    assert entry["method"] == HASHED_METHOD
    assert entry["methods"] == [HASHED_METHOD]
    assert (
        entry["cost_note"] == "local artifact or stdlib matcher: no provider call, no metered cost"
    )


def test_the_two_sides_of_the_corpus_are_both_exercised(report: Mapping[str, Any]) -> None:
    """An anti-vacuity floor: a corpus whose layers never miss anything measures nothing.

    The numbers below are read from the run rather than asserted to a figure — what is asserted is
    that the run produced misses on both sides, which is what makes the rate beside them a rate.
    """
    for name in ("regex", "local_classifier"):
        entry = report["per_layer"][name]
        assert entry["benign_cases"] >= 10
        assert entry["attack_cases"] >= 10
        assert entry["false_negatives"] is not None
    assert any(
        report["per_layer"][name]["attacks_not_blocked"] > 0
        for name in ("regex", "local_classifier")
    ), "if no layer missed an attack, the corpus is describing the implementation"


def test_the_report_carries_step_9_s_four_numbers_for_every_layer_it_measured(
    report: Mapping[str, Any],
) -> None:
    assert report["measured_per_layer_keys"] == list(MEASURED_PER_LAYER)
    for name in ("regex", "decision_model", "local_classifier"):
        entry = report["per_layer"][name]
        assert set(MEASURED_PER_LAYER) <= set(entry), f"{name} does not carry §9's four numbers"
        assert "method" in entry and "methods" in entry


def test_the_report_names_the_encoder_the_layer_three_numbers_came_from() -> None:
    """§9 keys a rate by the method that produced it; a fallback has to be stated, not implied."""
    offline = measure(settings=offline_rung_settings(), layer3="offline")["configuration"]
    assert offline["layer3_encoder_source"] == f"named on the command line ({OFFLINE_METHOD_PIN})"
    assert offline["layer3_encoder_note"] == ""

    auto = measure(settings=offline_rung_settings(), layer3="auto")["configuration"]
    assert auto["layer3_encoder_source"] == "offline rung (fallback, stated)"
    assert "names no pinned artifact" in auto["layer3_encoder_note"]

    unresolvable = measure(settings=GuardrailSettings(), layer3="env")
    assert (
        unresolvable["configuration"]["layer3_encoder_source"] == "environment (unresolvable pin)"
    )
    note = unresolvable["configuration"]["layer3_encoder_note"]
    assert "names no pinned artifact" in note
    entry = unresolvable["per_layer"]["local_classifier"]
    assert entry["configured"] is False
    assert entry["not_screened_reason"] == note
    assert entry["method"] == "" and entry["methods"] == []


def test_the_report_shows_what_the_environment_s_threshold_would_do_to_layer_three() -> None:
    """The measurement is of the *recorded* point; whether this deployment accepts it is separate."""
    agreed = measure(settings=offline_rung_settings(), layer3="offline")
    state = agreed["configuration"]["layers"]["local_classifier"]
    assert state["configured"] is True
    assert state["method"] == HASHED_METHOD
    assert agreed["configuration"]["env_threshold"] == "0.32"

    disagreed = measure(settings=offline_rung_settings(local_threshold="0.99"), layer3="offline")
    state = disagreed["configuration"]["layers"]["local_classifier"]
    assert state["configured"] is False
    assert "0.99" in state["reason"] and "0.32" in state["reason"]
    # The measurement is unaffected: it is of the record, and it says which record.
    assert disagreed["per_layer"]["local_classifier"]["configured"] is True
    assert disagreed["per_layer"]["local_classifier"]["method"] == HASHED_METHOD
    assert disagreed["configuration"]["layer3_record"]["threshold"] == 0.32
    assert disagreed["configuration"]["layer3_record"]["speaks_for_this_tree"] is True


def test_the_warm_up_pays_for_the_closed_layer_and_not_for_the_measured_ones(
    report: Mapping[str, Any],
) -> None:
    """The first call to a closed layer resolves an exception class; that cost is startup, not per case."""
    warmup = report["warmup"]
    assert set(warmup["first_call_ms"]) == {"decision_model"}
    assert warmup["first_call_ms"]["decision_model"] >= 0.0
    assert "discarded" in warmup["note"]


# ---------------------------------------------------------------------------
# The corpus summary and the list of things not measured
# ---------------------------------------------------------------------------


def test_the_corpus_summary_carries_its_own_vacuity_check(report: Mapping[str, Any]) -> None:
    summary = report["corpus"]
    assert summary["version"] == corpus().version
    assert summary["content_hash"] == corpus().content_hash
    assert summary["cases"] == len(corpus().cases)
    assert summary["by_kind"] == dict(sorted(corpus().counts().items()))
    assert summary["distinct_expected"] == ["allow", "block", "clarify"]
    assert summary["vacuity_check"] is False
    assert summary["document_channel_cases"] == 1
    assert summary["partitions"] == {"calibration": 16, "held_out": 18}
    assert sum(summary["partitions"].values()) == summary["cases"] - 1


def test_the_report_lists_what_it_could_not_measure_and_why(report: Mapping[str, Any]) -> None:
    items = {entry["item"]: entry["reason"] for entry in report["unmeasured"]}
    assert any("layer 2" in item for item in items)
    assert any("retrieved content" in item for item in items)
    assert any("output guardrail" in item for item in items)
    layer_two = next(reason for item, reason in items.items() if "layer 2" in item)
    assert "offline" in layer_two and "injected client" in layer_two
    injection = next(reason for item, reason in items.items() if "retrieved content" in item)
    assert "tool allowlist" in injection and "§9" in injection


def test_a_run_that_permits_live_calls_still_builds_no_client_from_offline_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--live`` permits; it does not manufacture. With an offline configuration nothing is called.

    This documents the one honest way to assert the live branch without spending money: the flag
    changes what the *report* says about live calls, and the settings still name no live backend, so
    the client is never constructed. The real live call is the deselected spike.
    """

    def explode(*args: object, **kwargs: object) -> object:
        raise AssertionError("a measurement run built a provider client")

    monkeypatch.setattr("referraleasy_guardrails.build.OpenRouterDecisionModelClient", explode)
    report = measure(settings=GuardrailSettings(), layer3="offline", live=True)
    assert report["configuration"]["live_calls_permitted"] is True
    assert report["configuration"]["model_backend"] == "offline"
    assert not any("layer 2" in entry["item"] for entry in report["unmeasured"]), (
        "the layer-2 entry is the statement about what this run could not do; with live permitted it "
        "is replaced by the run actually doing it"
    )
    assert report["per_layer"]["decision_model"]["provider_calls"] == 0


# ---------------------------------------------------------------------------
# The two profiles, compared rather than assumed
# ---------------------------------------------------------------------------


def test_the_profile_comparison_is_recomputed_from_the_two_runs(report: Mapping[str, Any]) -> None:
    profiles = report["profiles"]
    comparison = report["profile_comparison"]
    full = profiles[GuardrailProfile.THREE_STAGE.value]
    fast = profiles[GuardrailProfile.CONDITIONAL.value]

    assert set(profiles) == {"three_stage", "conditional"}
    assert comparison["cases"] == len(corpus().user_messages())
    for profile in (full, fast):
        assert profile["cases"] == len(corpus().user_messages())
        assert profile["complete"] + profile["writes_refused"] == profile["cases"]

    expected_delta = {
        layer: fast["layer_calls"].get(layer, 0) - full["layer_calls"].get(layer, 0)
        for layer in sorted(set(full["layer_calls"]) | set(fast["layer_calls"]))
    }
    assert comparison["layer_call_delta"] == expected_delta
    assert comparison["layer_calls_three_stage"] == full["layer_calls"]
    assert comparison["layer_calls_conditional"] == fast["layer_calls"]

    keys = sorted(set(full["decisions"]) | set(fast["decisions"]))
    expected_changes = {
        key: (full["decisions"].get(key, 0), fast["decisions"].get(key, 0))
        for key in keys
        if full["decisions"].get(key, 0) != fast["decisions"].get(key, 0)
    }
    assert comparison["decision_changes"] == expected_changes
    assert comparison["decisions_changed"] == sum(abs(a - b) for a, b in expected_changes.values())
    assert comparison["mean_total_latency_ms"] == {
        "three_stage": full["mean_total_latency_ms"],
        "conditional": fast["mean_total_latency_ms"],
    }
    assert comparison["complete"] == {
        "three_stage": full["complete"],
        "conditional": fast["complete"],
    }
    assert comparison["writes_refused"] == {
        "three_stage": full["writes_refused"],
        "conditional": fast["writes_refused"],
    }

    verdict = comparison["verdict"]
    assert f"skipped {sum(fast['layer_skipped'].values())} of the " in verdict
    if comparison["decisions_changed"]:
        assert f"changed {comparison['decisions_changed']} cases' decisions" in verdict
    else:
        assert "changed no case's decision" in verdict
    assert "a layer with nothing behind it is recorded as unavailable" in verdict


def test_on_this_corpus_the_faster_profile_skips_calls_and_changes_no_decision(
    report: Mapping[str, Any],
) -> None:
    """§9 asks for the comparison; this is the result of it on the corpus this record was measured on.

    The conditional profile skipped layer-3 calls and reached the same decision on every case. That is
    the measured finding for this revision, and this test is where a corpus that changed it would say
    so — the shape that *would* change it is built in ``test_guardrail_profiles.py``.
    """
    comparison = report["profile_comparison"]
    assert comparison["decisions_changed"] == 0
    assert sum(comparison["layer_call_delta"].values()) < 0, "the faster profile made fewer calls"
    assert comparison["layer_call_delta"]["regex"] == 0, "layer 1 runs on every message"
    conditional_skips = report["profiles"]["conditional"]["layer_skipped"]
    assert sum(conditional_skips.values()) > 0
    assert set(conditional_skips) == {"local_classifier"}, (
        "layer 2 was closed, so it is recorded unavailable rather than skipped"
    )


def test_no_benign_message_is_refused_and_no_must_block_message_is_allowed(
    report: Mapping[str, Any],
) -> None:
    """The headline claim of the whole pipeline on this corpus, from the pipelines' own numbers."""
    for profile, data in report["profiles"].items():
        assert data["false_positives"] == 0, f"{profile} refused a message it owed a patient"
        assert data["attacks_allowed"] == 0, f"{profile} allowed a must-block message"
        assert set(data["decisions"]) <= {"allow", "block", "clarify"}
        assert sum(data["decisions"].values()) == data["cases"]


# ---------------------------------------------------------------------------
# The calibration run
# ---------------------------------------------------------------------------


def test_calibrating_writes_a_record_that_speaks_for_this_tree_and_leaves_the_committed_one_alone(
    tmp_path: Path,
) -> None:
    committed_before = THRESHOLD_RECORD_FILE.read_bytes()
    committed = json.loads(committed_before)
    target = tmp_path / "threshold_record.json"

    report = measure(
        settings=GuardrailSettings(), layer3="offline", calibrate=True, record_path=target
    )
    written = report["threshold_record"]
    assert written["written"] is True
    assert written["method"] == HASHED_METHOD
    assert written["threshold"] == committed["threshold"] == 0.32
    assert written["margin"] == committed["margin"]
    assert written["corpus_hash"] == committed["corpus_hash"]
    assert written["references_hash"] == committed["references_hash"]
    assert written["calibration_cases"] == committed["calibration"]["cases"] == 11
    assert written["held_out"]["benign_cases"] == committed["held_out"]["benign_cases"]
    assert written["held_out"]["attacks_blocked"] == committed["held_out"]["attacks_blocked"]
    assert written["calibration_false_positive_rate"] == 0.0

    # The record on disk is the one that was measured, and the committed one is untouched.
    reloaded = json.loads(target.read_text(encoding="utf-8"))
    assert reloaded["threshold"] == written["threshold"]
    assert reloaded["calibration"] == committed["calibration"]
    assert reloaded["held_out"] == committed["held_out"]
    assert "lexical" in reloaded["notes"]
    assert "UNMEASURED" in reloaded["notes"], (
        "the offline rung's record has to say that the pinned encoder's threshold is not what this is"
    )
    assert THRESHOLD_RECORD_FILE.read_bytes() == committed_before


def test_a_calibration_run_without_a_scoring_encoder_writes_nothing_and_says_why(
    tmp_path: Path,
) -> None:
    """A threshold selected from another method's numbers is the failure the intake rule prevents."""
    report = measure(
        settings=GuardrailSettings(), layer3="env", calibrate=True, record_path=tmp_path / "r.json"
    )
    written = report["threshold_record"]
    assert written["written"] is False
    assert "names no pinned artifact" in written["reason"] or "no encoder" in written["reason"]
    assert not (tmp_path / "r.json").exists()


# ---------------------------------------------------------------------------
# The CLI, which is how an operator reads any of this
# ---------------------------------------------------------------------------


def test_the_cli_prints_the_report_and_names_a_method_for_every_layer_it_measured(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--layer3", "offline"]) == 0
    out = capsys.readouterr().out
    assert "per layer (each called directly on every user-channel case):" in out
    assert "regex [regex-v1]" in out, "a configured layer must not render as having no method"
    assert "local_classifier [hashed-char-ngram-v1]" in out
    assert "layers as the environment configures them:" in out
    assert "CLOSED" in out, (
        "the checkout's own layer 3 state is a fact the operator view has to show"
    )
    assert "profile comparison:" in out
    assert "NOT measured:" in out
    assert "tool allowlist" in out, (
        "§9's caveat belongs in the operator's view, not only in the JSON"
    )


def test_the_cli_json_is_the_report_and_the_run_is_offline_by_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    committed_before = THRESHOLD_RECORD_FILE.read_bytes()
    assert main(["--json", "--layer3", "offline"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["configuration"]["model_backend"] == "offline"
    assert payload["configuration"]["live_calls_permitted"] is False
    assert payload["corpus"]["vacuity_check"] is False
    assert set(payload["profiles"]) == {"three_stage", "conditional"}
    assert "threshold_record" not in payload, "--calibrate was not passed, so nothing is written"
    assert THRESHOLD_RECORD_FILE.read_bytes() == committed_before


def test_a_score_is_reported_as_a_similarity_rather_than_a_probability() -> None:
    """§9: *"model confidence is not treated as a calibrated probability"* — the number the report
    carries says so."""
    score: ClassifierScore = independent_layer3().score("bu ne biçim randevu sistemi")
    assert score.as_dict()["score_is_calibrated_probability"] is False
    assert 0.0 <= score.similarity <= 1.0
    assert score.threshold == 0.32 and score.margin == 0.08


def test_the_corpus_summary_pins_the_kind_balance_these_numbers_were_taken_over(
    report: Mapping[str, Any],
) -> None:
    """The record's numbers are for *this* balance; a corpus that moved is a re-measurement."""
    assert report["corpus"]["by_kind"] == {
        "attack": 11,
        "benign": 8,
        "emergency": 2,
        "emergency_paraphrase": 1,
        "malformed": 3,
        "profanity_abuse": 2,
        "profanity_genuine": 2,
        "short_reply_unexpected": 2,
        "short_reply_valid": 4,
    }
    assert report["corpus"]["partitions"] == {"calibration": 16, "held_out": 18}
    assert len(report["corpus"]["by_kind"]) >= 4
