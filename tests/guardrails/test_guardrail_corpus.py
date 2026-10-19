"""The labelled corpus, the reference utterances and the pin table — the data behind §9's numbers (§9).

§9 asks for measurement rather than opinion — *"Measure false positives/false negatives and added
latency/cost per layer"* — and a measurement is only as good as the set it was taken over. This file
is the one that refuses to let the set be vacuous: it asserts that the corpus carries more than one
answer, that every kind it names is actually present, that the two partitions are non-empty, disjoint
and cover every case a layer screens, and that both sides of what layer 3 selects between are in each
partition. A corpus with one label would make every rate below it a description of the corpus rather
than of the layer, which is the failure mode ``evaluation.main`` refuses to print numbers over.

Three more things are asserted here because they are load-bearing rather than incidental:

* **The document-channel case resolves to the committed fixture.** Its text is not copied into the
  corpus file, so a fixture that changed would change the case. The case is also *not* a user message,
  and the assertion that it stays out of the partitions is what keeps it out of the layer rates: §9's
  answer to injection in retrieved content is not a better regex, and the corpus must not pretend the
  layers screen it.
* **The short-reply cases state their state.** ``short_reply_expected`` and ``open_question`` are facts
  the workflow knows; the corpus is where they are written down, and the corpus is what the tests that
  prove a bare ``"evet"`` is not refused for being short are built on.
* **The corpus is identified by a content hash.** Layer 3's threshold was measured against this exact
  revision (ADR-0005's intake rule), so the hash has to follow the contents: it is stable across
  loads, it changes when a message changes, and the in-memory helper builds it the same way the file
  loader does.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from guardrail_fixtures import REPO_ROOT, corpus, references, synthetic_corpus

from referraleasy_contracts.generated.common import GuardrailCategory, GuardrailDecision
from referraleasy_guardrails.corpus import (
    ARTIFACTS_FILE,
    CORPUS_FILE,
    REFERENCE_SET_FILE,
    ArtifactPin,
    LabelledCorpus,
    ReferenceSet,
    canonical_hash,
    data_dir,
    sha256_file,
)
from referraleasy_guardrails.types import mask_pii

#: §9's layers screen user messages. The corpus also carries one retrieved document, and the two are
#: different channels on purpose — see the module docstring.
USER_MESSAGE = "user_message"
DOCUMENT_CHANNEL = "retrieved_document"

#: Every kind the corpus names in its own ``case_fields`` vocabulary, and the decision the corpus says
#: a complete three-layer pipeline should reach for it. ``malformed`` is the one kind with more than
#: one answer (an empty message is a question, an oversized one is a refusal), and it is asserted
#: separately rather than papered over with a weaker claim.
EXPECTATION_BY_KIND = {
    "benign": GuardrailDecision.allow,
    "short_reply_valid": GuardrailDecision.allow,
    "profanity_genuine": GuardrailDecision.allow,
    "attack": GuardrailDecision.block,
    "profanity_abuse": GuardrailDecision.block,
    "emergency": GuardrailDecision.block,
    "emergency_paraphrase": GuardrailDecision.clarify,
    "short_reply_unexpected": GuardrailDecision.clarify,
}
KINDS = set(EXPECTATION_BY_KIND) | {"malformed"}

#: The kinds layer 3's threshold is selected and reported on: the two sides it decides between.
BENIGN_KINDS = {"benign", "short_reply_valid", "profanity_genuine"}
SELECTION_KINDS = BENIGN_KINDS | {"attack"}


def raw_corpus() -> Mapping[str, Any]:
    """The committed corpus file as JSON, for the assertions about *entries* rather than cases."""
    return json.loads(CORPUS_FILE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Anti-vacuity: what a corpus has to carry before a rate over it means anything
# ---------------------------------------------------------------------------


def test_the_corpus_carries_more_than_one_answer() -> None:
    """The failure mode ``evaluation.main`` refuses to print over: a corpus with one label."""
    labelled = corpus()
    assert {case.expected for case in labelled.cases} == {
        GuardrailDecision.allow,
        GuardrailDecision.block,
        GuardrailDecision.clarify,
    }, "a corpus that never expects a refusal cannot measure false negatives"
    assert set(labelled.counts()) == KINDS
    assert all(count >= 1 for count in labelled.counts().values())
    assert sum(labelled.counts().values()) == len(labelled.cases)
    assert len(labelled.user_messages()) >= 2 * len(KINDS), (
        "each kind has to be more than a token case for the rates to be about a layer rather than "
        "about a fixture"
    )


def test_every_kind_states_the_expectation_it_means() -> None:
    """A case's kind and its expectation cannot disagree: a mislabelled case measures the wrong thing."""
    for case in corpus().cases:
        if case.kind in EXPECTATION_BY_KIND:
            assert case.expected is EXPECTATION_BY_KIND[case.kind], (
                f"{case.case_id}: kind {case.kind} expects {EXPECTATION_BY_KIND[case.kind].value}, "
                f"the corpus says {case.expected.value}"
            )
    # The one kind with two answers carries both, so the corpus does not have to be read to know it.
    malformed = {case.expected for case in corpus().by_kind("malformed")}
    assert malformed == {GuardrailDecision.block, GuardrailDecision.clarify}


def test_every_case_states_what_it_is_and_why_it_is_here() -> None:
    labelled = corpus()
    ids = [case.case_id for case in labelled.cases]
    assert len(ids) == len(set(ids)), "two cases with one id make a failure message useless"
    for case in labelled.cases:
        assert case.note.strip(), f"{case.case_id} does not say why it is in the corpus"
        assert case.workflow_state.strip()
        assert case.language in {"en", "tr"}
        if not case.text:
            # An empty message is only ever *the* case for emptiness, never a benign or attack message.
            assert case.kind == "malformed", f"{case.case_id} has no text and is not malformed"


def test_the_partitions_are_non_empty_disjoint_and_cover_every_user_message() -> None:
    labelled = corpus()
    assert labelled.partitions() == ("calibration", "held_out")
    calibration = {case.case_id for case in labelled.in_partition("calibration")}
    held_out = {case.case_id for case in labelled.in_partition("held_out")}
    assert calibration and held_out
    assert calibration & held_out == set(), (
        "a case in both partitions is selected on and reported on"
    )
    screened = {case.case_id for case in labelled.user_messages()}
    assert calibration | held_out == screened
    assert screened, "a partition set that covers nothing is a partition set over nothing"


def test_both_partitions_carry_both_sides_of_what_layer_three_selects_between() -> None:
    """Selection needs attacks to select against; reporting needs both sides or the rate has no floor."""
    labelled = corpus()
    for partition in ("calibration", "held_out"):
        selection = [
            case for case in labelled.in_partition(partition) if case.kind in SELECTION_KINDS
        ]
        assert any(case.kind == "attack" for case in selection), (
            f"{partition}: a threshold selected without attack cases is selected against nothing"
        )
        assert any(case.kind in BENIGN_KINDS for case in selection), (
            f"{partition}: a false-positive rate with no benign cases is not a rate"
        )
        assert len(selection) > 1


def test_the_short_reply_cases_state_whether_a_question_is_open() -> None:
    """A length heuristic that refused ``"evet"`` would break the product; the corpus says so (§9)."""
    labelled = corpus()
    for case in labelled.by_kind("short_reply_valid"):
        assert case.short_reply_expected is True, f"{case.case_id} is a valid reply to a question"
        assert case.open_question, f"{case.case_id} does not say what was asked"
        assert len(case.text) <= 6, "the case is short on purpose and the test says how short"
    for case in labelled.by_kind("short_reply_unexpected"):
        assert case.short_reply_expected is False, f"{case.case_id} answers no question"
        assert case.open_question is None
    # A case that claims a question is open must carry the question, whatever its kind.
    for case in labelled.cases:
        if case.short_reply_expected:
            assert case.open_question


def test_the_emergency_cases_are_not_reduced_to_one_exact_phrase() -> None:
    """§9 refuses to treat an emergency as off-topic, and the corpus refuses to claim the phrase list
    is complete: the paraphrased emergency is kept as a case the exact-phrase rule misses, and the two
    exact phrasings carry the emergency category."""
    labelled = corpus()
    exact = labelled.by_kind("emergency")
    assert len(exact) >= 2
    assert all(case.expected is GuardrailDecision.block for case in exact)
    assert all(case.expected_category is GuardrailCategory.emergency for case in exact)
    paraphrases = labelled.by_kind("emergency_paraphrase")
    assert paraphrases
    for case in paraphrases:
        assert case.expected is GuardrailDecision.clarify, (
            f"{case.case_id}: the corpus expects a human, not a refusal — claiming the phrase list "
            "catches a paraphrase would be the claim this case exists to prevent"
        )


def test_the_oversized_case_is_built_from_a_repeated_message_and_says_why() -> None:
    """The corpus's own ``case_fields`` documents ``repeat``; the loader has to honour it."""
    entries = {entry["case_id"]: entry for entry in raw_corpus()["cases"]}
    entry = entries["malformed-oversized"]
    assert entry["repeat"] > 1
    loaded = next(case for case in corpus().cases if case.case_id == "malformed-oversized")
    assert loaded.text == entry["text"] * entry["repeat"]
    assert len(loaded.text) > 2000, "the case is oversized relative to §9's message cap"
    assert loaded.expected is GuardrailDecision.block
    assert loaded.expected_category is GuardrailCategory.oversized_input


# ---------------------------------------------------------------------------
# The document channel: in the corpus, out of the layer rates
# ---------------------------------------------------------------------------


def test_the_document_channel_case_resolves_to_the_committed_fixture() -> None:
    documents = corpus().in_channel(DOCUMENT_CHANNEL)
    assert len(documents) == 1
    case = documents[0]
    assert case.is_user_message is False
    assert case.kind == "attack"
    assert case.expected is GuardrailDecision.block

    raw_entry = next(entry for entry in raw_corpus()["cases"] if entry["case_id"] == case.case_id)
    ref = raw_entry["fixture_ref"]
    fixtures = json.loads((REPO_ROOT / ref["path"]).read_text(encoding="utf-8"))
    flagged = [document for document in fixtures if document.get(ref["select"]) is True]
    assert len(flagged) == 1, "the corpus selects one document by its flag, not by its position"
    assert case.text == flagged[0][ref.get("field", "text")], (
        "the case's text is the fixture's, so the two cannot drift apart"
    )
    assert case.text.strip()


def test_the_document_channel_case_is_out_of_every_rate() -> None:
    """§9: input screening is not the only defence. The case is kept so the *miss* is reported."""
    labelled = corpus()
    document = labelled.in_channel(DOCUMENT_CHANNEL)[0]
    screened = {case.case_id for case in labelled.user_messages()}
    assert document.case_id not in screened
    partitions = {
        case.case_id
        for partition in labelled.partitions()
        for case in labelled.in_partition(partition)
    }
    assert document.case_id not in partitions
    # It is still counted, by channel, so a report can say how many cases were left out and why.
    assert len(labelled.in_channel(DOCUMENT_CHANNEL)) == 1
    assert set(labelled.in_channel(USER_MESSAGE)) == set(labelled.user_messages())


# ---------------------------------------------------------------------------
# The hashes: the identity a threshold measurement is tied to
# ---------------------------------------------------------------------------


def test_the_corpus_hash_is_stable_across_loads_and_follows_the_contents() -> None:
    first = LabelledCorpus.load()
    second = LabelledCorpus.load()
    assert first.content_hash == second.content_hash
    assert len(first.content_hash) == 64

    # Built in memory the way the loader hashes a file: the helper and the loader have to agree, or a
    # test record would speak for a corpus nobody measured.
    assert synthetic_corpus(first.cases).content_hash == first.content_hash

    # One message edited is a different corpus, and the key order of the surrounding JSON is not.
    edited = tuple(
        replace(case, text=case.text + " ve bir de")
        if case.case_id == "benign-appointment-en-01"
        else case
        for case in first.cases
    )
    assert synthetic_corpus(edited).content_hash != first.content_hash
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})
    # The order of the cases is part of the identity: a reordered corpus is a different revision.
    assert synthetic_corpus(tuple(reversed(first.cases))).content_hash != first.content_hash


def test_loading_a_corpus_file_refuses_an_empty_corpus_and_an_unresolvable_fixture(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"version": "v", "cases": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        LabelledCorpus.load(empty)

    # A fixture reference that selects nothing is a refusal, not a case with an empty message: the
    # corpus would otherwise screen "" and call it an attack.
    documents = tmp_path / "documents.json"
    documents.write_text(
        json.dumps([{"contains_injection_attempt": False, "text": "x"}]), encoding="utf-8"
    )
    payload = {
        "version": "v",
        "cases": [
            {
                "case_id": "c1",
                "kind": "attack",
                "expected": "block",
                "fixture_ref": {"path": "documents.json", "select": "contains_injection_attempt"},
            }
        ],
    }
    corpus_file = tmp_path / "corpus.json"
    corpus_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="no document in"):
        LabelledCorpus.load(corpus_file, repo_root=tmp_path)

    # And it resolves when the flag is set, which is what makes the refusal above a check.
    documents.write_text(
        json.dumps([{"contains_injection_attempt": True, "text": "the flagged letter"}]),
        encoding="utf-8",
    )
    resolved = LabelledCorpus.load(corpus_file, repo_root=tmp_path)
    assert resolved.cases[0].text == "the flagged letter"


def test_the_three_data_files_are_the_package_s_own_and_are_hashed_as_bytes() -> None:
    assert data_dir() == CORPUS_FILE.parent
    assert data_dir() == REFERENCE_SET_FILE.parent
    assert data_dir() == ARTIFACTS_FILE.parent
    assert CORPUS_FILE.is_file() and REFERENCE_SET_FILE.is_file() and ARTIFACTS_FILE.is_file()
    # The streamed digest is the same digest a single read would give; the 23 MB artifact it exists
    # for is not the only file it is used on.
    assert sha256_file(CORPUS_FILE) == hashlib.sha256(CORPUS_FILE.read_bytes()).hexdigest()
    assert len(sha256_file(CORPUS_FILE)) == 64


# ---------------------------------------------------------------------------
# The reference utterances
# ---------------------------------------------------------------------------


def test_the_reference_set_carries_both_sides_with_a_category_for_every_attack() -> None:
    reference_set = references()
    assert reference_set.version
    assert reference_set.attacks and reference_set.benign
    assert all(not reference.benign for reference in reference_set.attacks)
    assert all(reference.category is not None for reference in reference_set.attacks)
    assert all(reference.benign for reference in reference_set.benign)
    assert len({reference.ref_id for reference in reference_set.all_references()}) == len(
        reference_set.all_references()
    )
    texts = [reference.text for reference in reference_set.all_references()]
    assert len(set(texts)) == len(texts), "duplicate references weight the score twice"
    assert len(reference_set.categories()) >= 2, "one category of attack is one kind of attack"
    assert set(reference_set.categories()) <= {category.value for category in GuardrailCategory}
    # §9 requires the *method* to be recorded alongside the artifact hash and the thresholds, and the
    # reference file is where layer 3's references say how they were chosen.
    payload = json.loads(REFERENCE_SET_FILE.read_text(encoding="utf-8"))
    assert payload["created"]
    assert str(payload["method_note"]).strip(), "the references do not say how they were chosen"


def test_the_reference_set_refuses_a_one_sided_file_and_its_hash_follows_its_text(
    tmp_path: Path,
) -> None:
    payload = json.loads(REFERENCE_SET_FILE.read_text(encoding="utf-8"))
    one_sided = tmp_path / "one-sided.json"
    one_sided.write_text(
        json.dumps({"version": "v", "attack_references": payload["attack_references"]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="both sides"):
        ReferenceSet.load(one_sided)

    edited = json.loads(json.dumps(payload))
    edited["attack_references"][0]["text"] = edited["attack_references"][0]["text"] + " !"
    edited_file = tmp_path / "edited.json"
    edited_file.write_text(json.dumps(edited), encoding="utf-8")
    assert ReferenceSet.load(edited_file).content_hash != references().content_hash


# ---------------------------------------------------------------------------
# The artifact pins
# ---------------------------------------------------------------------------

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA1 = re.compile(r"^[0-9a-f]{40}$")


def test_the_pin_table_names_every_artifact_by_content_hash_and_by_the_adr_that_pinned_it() -> None:
    """ADR-0005 pins layer 3 by content hash; a pin without one is a name, not a pin."""
    pins = ArtifactPin.load_all()
    assert pins
    for pin in pins:
        assert _SHA256.match(pin.sha256), f"{pin.model}: {pin.sha256!r} is not a sha256"
        assert _GIT_SHA1.match(pin.revision), f"{pin.model}: a revision is a git sha1"
        assert pin.bytes > 0
        assert pin.file and pin.tokenizer_file
        assert _GIT_SHA1.match(pin.tokenizer_git_blob_sha1)
        assert pin.adr.startswith("ADR-")
        assert pin.pin == f"{pin.model}@{pin.revision}"
        # The pin's own ADR has to exist, or the hash is recorded nowhere a reader can check.
        assert list((REPO_ROOT / "docs" / "adr").glob(f"{pin.adr}-*.md"))
        # And the value the environment names resolves to the pin it names, exactly.
        assert ArtifactPin.find(pin.pin) == pin
        assert ArtifactPin.find(pin.model) is None, "a model id without its revision pins nothing"
        assert ArtifactPin.find(f"{pin.model}@{pin.revision[:-1]}") is None


def test_the_pin_table_is_the_only_path_to_layer_three_s_artifact() -> None:
    """``build`` resolves a pin through this table and refuses a value that names none — the test is
    here rather than only in the builder because the table is the thing that has to be complete."""
    assert ArtifactPin.find("sentence-transformers/all-MiniLM-L6-v2") is None
    assert ArtifactPin.find("offline/hashed-char-ngram-v1") is None
    assert ArtifactPin.find("") is None
    pins = ArtifactPin.load_all()
    assert len({pin.pin for pin in pins}) == len(pins)


def test_a_case_asks_the_layers_the_question_the_workflow_knows_how_to_ask() -> None:
    """The corpus and the layers meet at ``CorpusCase.request()``; a lost field would lose §9's context."""
    case = next(entry for entry in corpus().cases if entry.short_reply_expected)
    request = case.request()
    assert request.text == case.text
    assert request.workflow_state == case.workflow_state
    assert request.short_reply_expected is True
    assert request.open_question == case.open_question
    assert request.synthetic is True
    assert request.masked_text == mask_pii(case.text)
