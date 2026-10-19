"""Structured records outrank retrieved text, and the disagreement stays visible.

The corpus contains the case on purpose: the Des Plaines Valley General Hospital blurb asserts it
is "in-network with every major plan", while the structured coverage result for that plan and
hospital is `unknown` with `conflict: true` and a *stale* freshness state. A directory blurb is
prose; the coverage record is the platform's answer with evidence ids attached. When they disagree
the record wins — and the disagreement is returned rather than resolved quietly, because "the API
does not know and the directory says yes" is exactly what a human needs to see before telling a
patient to go ahead.

The same file pins the second rule: blurbs promise availability ("confirmed ... the same business
day"), and the directory is not allowed to. The stripping happens on the way to a model, not at
index time, so the index stays a faithful record of what the directory says.
"""

from __future__ import annotations

import pytest

from referraleasy_rag.authority import (
    ClaimScan,
    CoverageLookup,
    resolve_coverage,
    scan_text_claims,
    strip_availability_claims,
)
from referraleasy_rag.config import Settings
from referraleasy_rag.degradation import LadderState
from referraleasy_rag.retriever import RetrievalResult, RetrievedChunk
from referraleasy_rag.seed import SeedCorpus

PLAN_WITH_UNKNOWN_COVERAGE = "60000000-0000-4000-8000-000000000004"
PROVIDER_ELENA = "30000000-0000-4000-8000-000000000001"
HOSPITAL_LAKEVIEW = "20000000-0000-4000-8000-000000000001"
HOSPITAL_DES_PLAINES = "20000000-0000-4000-8000-000000000004"
#: The structured row that contradicts the Des Plaines blurb: unknown, conflict, stale.
COVERAGE_DES_PLAINES = "80000000-0000-4000-8000-000000000009"
#: A hospital + provider row for the same plan, also unknown with a recorded conflict.
COVERAGE_LAKEVIEW_CONFLICT = "80000000-0000-4000-8000-000000000002"
#: A plain in-network row, used as the agreeing control.
COVERAGE_LAKEVIEW_IN_NETWORK = "80000000-0000-4000-8000-000000000001"

IN_NETWORK_SENTENCE = "Des Plaines Valley General Hospital is in-network with every major plan."
OUT_OF_NETWORK_SENTENCE = "Dr. Example is not in-network for this plan."


def _document(seed: SeedCorpus, entity_id: str) -> str:
    for document in seed.documents:
        if document.entity_id == entity_id:
            return document.text
    raise AssertionError(f"the seed corpus has no document for {entity_id}")


def _record(settings: Settings, coverage_id: str) -> dict[str, object]:
    lookup = CoverageLookup.from_fixtures(settings.fixtures_dir)
    for row in lookup.for_provider(PROVIDER_ELENA) + list(_all_rows(settings)):
        if row.get("coverage_id") == coverage_id:
            return dict(row)
    raise AssertionError(f"fixtures carry no coverage result {coverage_id}")


def _all_rows(settings: Settings) -> list[dict[str, object]]:
    from referraleasy_rag.contracts import load_coverage_results

    return [dict(row) for row in load_coverage_results(settings.fixtures_dir)]


# ------------------------------------------------------------- the fixture case exists


def test_the_corpus_still_contains_the_conflict_case(seed: SeedCorpus, settings: Settings) -> None:
    """If the fixture goes, these tests would pass vacuously. So it is asserted first."""
    text = _document(seed, HOSPITAL_DES_PLAINES)
    assert "in-network with every major plan" in text

    record = _record(settings, COVERAGE_DES_PLAINES)
    assert record["status"] == "unknown"
    assert record["conflict"] is True
    assert record["conflicting_evidence_ids"]


def test_the_blurb_also_promises_availability(seed: SeedCorpus) -> None:
    text = _document(seed, HOSPITAL_DES_PLAINES)
    assert "same-day appointments" in text


# ------------------------------------------------------------------ structured beats text


def test_a_structured_unknown_does_not_lose_to_a_confident_blurb(
    seed: SeedCorpus, settings: Settings
) -> None:
    text = _document(seed, HOSPITAL_DES_PLAINES)
    resolution = resolve_coverage(
        _record(settings, COVERAGE_DES_PLAINES),
        [(f"{HOSPITAL_DES_PLAINES}:0:abc", HOSPITAL_DES_PLAINES, text)],
    )

    assert resolution.status == "unknown", "the record's value survives untouched"
    assert resolution.has_conflict is True
    conflict = resolution.conflicts[0]
    assert conflict.field_name == "status"
    assert conflict.structured_value == "unknown"
    assert conflict.claimed_value == "in_network"
    assert "in-network with every major plan" in conflict.claim.text
    assert conflict.to_dict()["resolution"] == "structured record wins; both sides reported"


def test_both_sides_of_the_conflict_are_returned_not_just_the_winner(
    seed: SeedCorpus, settings: Settings
) -> None:
    text = _document(seed, HOSPITAL_DES_PLAINES)
    resolution = resolve_coverage(
        _record(settings, COVERAGE_DES_PLAINES),
        [(f"{HOSPITAL_DES_PLAINES}:0:abc", HOSPITAL_DES_PLAINES, text)],
    )
    assert resolution.claims, "the retrieved claim is kept, not deleted"
    assert any(claim.text in text for claim in resolution.claims)
    payload = resolution.to_dict()
    assert payload["conflicts"][0]["claim"]["polarity"] == "positive"


def test_the_evidence_ids_of_the_record_survive(settings: Settings) -> None:
    resolution = resolve_coverage(_record(settings, COVERAGE_DES_PLAINES))
    assert resolution.evidence_ids == (
        "50000000-0000-4000-8000-000000000005",
        "50000000-0000-4000-8000-000000000006",
        "50000000-0000-4000-8000-000000000024",
    )
    assert resolution.conflicting_evidence_ids
    assert resolution.freshness_state == "stale"
    assert resolution.disclaimer_key


def test_text_that_agrees_with_the_record_is_not_flagged(settings: Settings) -> None:
    """The negative control: conflicts are reported when they exist, not whenever text mentions
    network status."""
    resolution = resolve_coverage(
        _record(settings, COVERAGE_LAKEVIEW_IN_NETWORK),
        [("chunk-1", HOSPITAL_LAKEVIEW, IN_NETWORK_SENTENCE)],
    )
    assert resolution.status == "in_network"
    assert resolution.has_conflict is False
    assert resolution.claims, "the claim was still seen — it simply agreed"


def test_a_negative_claim_against_an_in_network_record_is_a_conflict(settings: Settings) -> None:
    resolution = resolve_coverage(
        _record(settings, COVERAGE_LAKEVIEW_IN_NETWORK),
        [("chunk-1", HOSPITAL_LAKEVIEW, OUT_OF_NETWORK_SENTENCE)],
    )
    assert resolution.status == "in_network"
    assert resolution.conflicts[0].claimed_value == "out_of_network"
    assert resolution.conflicts[0].structured_value == "in_network"


def test_no_record_resolves_to_no_answer_not_to_a_guess(seed: SeedCorpus) -> None:
    resolution = resolve_coverage(None, [("chunk-1", HOSPITAL_DES_PLAINES, IN_NETWORK_SENTENCE)])
    assert resolution.status == "unknown"
    assert resolution.has_conflict is False
    assert resolution.authoritative == {}
    assert resolution.plan_id is None


def test_the_lookup_never_returns_another_providers_coverage(settings: Settings) -> None:
    lookup = CoverageLookup.from_fixtures(settings.fixtures_dir)
    assert (
        lookup.get(
            plan_id=PLAN_WITH_UNKNOWN_COVERAGE,
            provider_id="30000000-0000-4000-8000-000000000099",
            hospital_id=HOSPITAL_LAKEVIEW,
        )
        is None
    )
    assert lookup.get(plan_id=None, provider_id=None, hospital_id=HOSPITAL_LAKEVIEW) is None


def test_the_lookup_returns_the_exact_triple_when_it_exists(settings: Settings) -> None:
    lookup = CoverageLookup.from_fixtures(settings.fixtures_dir)
    row = lookup.get(
        plan_id=PLAN_WITH_UNKNOWN_COVERAGE,
        provider_id=PROVIDER_ELENA,
        hospital_id=HOSPITAL_LAKEVIEW,
    )
    assert row is not None
    assert row["coverage_id"] == COVERAGE_LAKEVIEW_CONFLICT


def test_the_lookup_relaxes_the_hospital_but_never_the_provider() -> None:
    """A provider-level answer is a real answer; a different provider's answer is not."""
    lookup = CoverageLookup(
        [
            {
                "plan_id": "plan-1",
                "provider_id": "provider-1",
                "hospital_id": None,
                "status": "in_network",
            },
            {
                "plan_id": "plan-1",
                "provider_id": "provider-1",
                "hospital_id": "hospital-1",
                "status": "out_of_network",
            },
        ]
    )
    assert (
        lookup.get(plan_id="plan-1", provider_id="provider-1", hospital_id="hospital-1")["status"]
        == "out_of_network"
    )
    assert (
        lookup.get(plan_id="plan-1", provider_id="provider-1", hospital_id="hospital-2")["status"]
        == "in_network"
    )
    assert lookup.get(plan_id="plan-1", provider_id="provider-2", hospital_id="hospital-1") is None


# ------------------------------------------------------------------ availability stripping


def test_an_availability_promise_is_removed_from_grounding_text(seed: SeedCorpus) -> None:
    text = _document(seed, HOSPITAL_LAKEVIEW)
    scan = strip_availability_claims(text, chunk_id="chunk-1")

    assert scan.had_claims is True
    assert scan.removed[0].kind == "availability_or_booking"
    assert "same business day" in scan.removed[0].text
    assert "same business day" not in scan.text
    assert "Lakeview Regional Medical Center is at 675 North Fairbanks Court" in scan.text


def test_a_blurb_without_availability_claims_is_left_alone(seed: SeedCorpus) -> None:
    text = _document(seed, PROVIDER_ELENA)
    scan = strip_availability_claims(text, chunk_id="chunk-1")
    assert scan.removed == ()
    assert scan.text == text


def test_stripping_takes_the_whole_sentence_not_a_fragment(seed: SeedCorpus) -> None:
    """A half-removed sentence is worse than the original: it reads as a different claim."""
    text = _document(seed, HOSPITAL_DES_PLAINES)
    scan = strip_availability_claims(text, chunk_id="chunk-1")
    assert scan.removed, "the Des Plaines blurb promises a same-day appointment"
    assert "same-day" not in scan.text
    assert scan.text.endswith("."), "the result is still made of whole sentences"
    assert "in-network with every major plan" in scan.text, (
        "a network claim is not an availability claim; it is handled by resolve_coverage instead"
    )


def test_the_index_keeps_the_blurb_as_written(seed: SeedCorpus) -> None:
    """Sanitising the corpus would hide the conflict the previous tests depend on."""
    assert "same business day" in _document(seed, HOSPITAL_LAKEVIEW)


# ------------------------------------------------------------------ through the retriever


def test_grounding_context_reports_what_it_removed(seed: SeedCorpus) -> None:
    text = _document(seed, HOSPITAL_LAKEVIEW)
    result = RetrievalResult(
        query="Lakeview Regional Medical Center appointments",
        results=(_chunk(HOSPITAL_LAKEVIEW, text),),
        ladder=LadderState(),
    )
    passage = result.grounding_context()[0]

    assert passage.removed_claims, "the removal is reported, not silent"
    assert "same business day" not in passage.text
    assert passage.citation == f"directory:{HOSPITAL_LAKEVIEW}#{HOSPITAL_LAKEVIEW}:0:abc"
    assert result.stripped_claims() == list(passage.removed_claims)


def test_a_clean_passage_reports_nothing_removed(seed: SeedCorpus) -> None:
    result = RetrievalResult(
        query="Elena Marquez",
        results=(_chunk(PROVIDER_ELENA, _document(seed, PROVIDER_ELENA)),),
        ladder=LadderState(),
    )
    passage = result.grounding_context()[0]
    assert passage.removed_claims == ()
    assert passage.text == result.results[0].text


def test_the_retriever_resolves_coverage_against_its_own_results(
    seed: SeedCorpus, settings: Settings
) -> None:
    """The whole path: retrieved text in, record out, conflict surfaced."""
    lookup = CoverageLookup.from_fixtures(settings.fixtures_dir)
    result = RetrievalResult(
        query="Des Plaines Valley General Hospital coverage",
        results=(_chunk(HOSPITAL_DES_PLAINES, _document(seed, HOSPITAL_DES_PLAINES)),),
        ladder=LadderState(),
    )
    resolution = result.coverage(
        lookup,
        plan_id=PLAN_WITH_UNKNOWN_COVERAGE,
        provider_id=None,
        hospital_id=HOSPITAL_DES_PLAINES,
    )

    # `CoverageLookup.get` needs a provider to answer at all, so a hospital-only question has no
    # structured answer — and the retriever says so rather than promoting the blurb to one.
    assert resolution.status == "unknown"
    assert resolution.has_conflict is False
    assert resolution.plan_id is None or resolution.authoritative == {}

    conflict = resolve_coverage(
        _record(settings, COVERAGE_DES_PLAINES),
        [(chunk.chunk_id, chunk.entity_id, chunk.text) for chunk in result.results],
    )
    assert conflict.has_conflict is True


def test_a_claim_scan_of_a_clean_text_is_empty(seed: SeedCorpus) -> None:
    assert scan_text_claims(_document(seed, PROVIDER_ELENA)) == []
    scan: ClaimScan = strip_availability_claims(_document(seed, PROVIDER_ELENA))
    assert not scan.had_claims


@pytest.mark.parametrize(
    ("text", "polarity"),
    [
        ("This practice is in-network with every major plan.", "positive"),
        ("This practice is not in-network for that plan.", "negative"),
        ("The clinic is out-of-network for Summit HDHP Saver.", "negative"),
        ("We no longer accept the Meridian HMO Select plan.", "negative"),
    ],
)
def test_network_claims_are_detected_in_both_directions(text: str, polarity: str) -> None:
    claims = scan_text_claims(text, chunk_id="c1")
    assert claims
    assert claims[0].polarity == polarity
    assert claims[0].kind == "network_status"


def _chunk(entity_id: str, text: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"{entity_id}:0:abc",
        entity_id=entity_id,
        entity_type="hospital",
        source_id=f"directory:{entity_id}",
        knowledge_version="kv-test",
        text=text,
        score=1.0,
        rank=1,
    )
