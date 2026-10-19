"""The JSON rung: deterministic, offline, and derived from the same chunks as the index.

This rung exists to answer when nothing else can, so the properties that matter are the ones a
fallback cannot fake: it makes no request of any kind, it produces byte-identical output from
identical input, and it carries the same documents the index holds — so degrading changes where
an answer comes from, not what it is made of.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from referraleasy_rag.chunking import Chunk, corpus_version
from referraleasy_rag.config import Settings
from referraleasy_rag.geo import distance_km, haversine_km
from referraleasy_rag.opensearch_index import DOCUMENT_METADATA_FIELDS
from referraleasy_rag.seed import (
    SEED_FORMAT_VERSION,
    SeedCorpus,
    SeedDocument,
    SeedError,
    build_seed_payload,
    default_seed_corpus,
    seed_documents_from_chunks,
    serialize_seed,
    write_seed,
)

CHICAGO = (41.8781, -87.6298)

#: Bookkeeping fields the index document carries; the seed reproduces all of them but `embedding`.
_INDEX_DOCUMENT_FIELDS = {
    "chunk_id",
    "entity_id",
    "entity_type",
    "source_id",
    "knowledge_version",
    "ordinal",
    "run_id",
    "corpus_version",
    "chunker_version",
    "text",
}


def test_the_committed_seed_is_what_the_fixtures_produce_today(settings: Settings) -> None:
    """A stale seed is a fallback that answers from a directory nobody is serving any more."""
    fresh = build_seed_payload(settings)
    committed = default_seed_corpus()

    assert fresh["knowledge_version"] == committed.knowledge_version
    assert [document["chunk_id"] for document in fresh["documents"]] == [
        document.chunk_id for document in committed.documents
    ]


def test_the_seed_declares_its_own_format_version(settings: Settings) -> None:
    payload = build_seed_payload(settings)
    assert payload["seed_format_version"] == SEED_FORMAT_VERSION
    assert default_seed_corpus().seed_format_version == SEED_FORMAT_VERSION


def test_the_seed_is_byte_identical_when_rebuilt(settings: Settings) -> None:
    """Determinism is what makes this rung testable at all: a wobbling fallback cannot be."""
    first = serialize_seed(build_seed_payload(settings))
    second = serialize_seed(build_seed_payload(settings))
    assert first == second
    assert first.endswith("\n"), "the file is written with a trailing newline so diffs stay clean"


def test_writing_the_seed_twice_produces_the_same_bytes(tmp_path: Path, settings: Settings) -> None:
    target = tmp_path / "seed.json"
    payload = build_seed_payload(settings)
    write_seed(target, payload)
    first = target.read_bytes()
    write_seed(target, payload)
    assert target.read_bytes() == first
    assert len(first) > 0


def test_the_seed_carries_no_vectors(settings: Settings) -> None:
    """Vectors would make this rung depend on the thing it is the alternative to."""
    for document in build_seed_payload(settings)["documents"]:
        assert "embedding" not in document


def test_seed_documents_carry_the_index_document_fields(
    settings: Settings, seed: SeedCorpus
) -> None:
    for document in build_seed_payload(settings)["documents"]:
        missing = sorted(_INDEX_DOCUMENT_FIELDS - set(document))
        assert missing == [], f"seed document is missing index fields: {missing}"
    for document in seed.documents:
        assert document.chunk_id
        assert document.source_id, "every seed document is citable"
        assert document.knowledge_version
        assert document.text
        assert document.entity_type in {"provider", "hospital", "location"}


def test_seed_metadata_is_covered_by_the_index_mapping(seed: SeedCorpus) -> None:
    """The two rungs search the same fields, so a filter cannot mean one thing in each."""
    produced = {key for document in seed.documents for key in document.metadata}
    unknown = sorted(produced - set(DOCUMENT_METADATA_FIELDS))
    assert unknown == [], f"seed metadata the mapping does not carry: {unknown}"


def test_the_seed_is_the_same_corpus_as_the_index(chunks: list[Chunk], seed: SeedCorpus) -> None:
    """Round trip: the seed documents are the chunker's documents, minus the vector."""
    converted = seed_documents_from_chunks(chunks, version=corpus_version(chunks))
    assert [document.chunk_id for document in converted] == [
        document.chunk_id for document in seed.documents
    ]
    for left, right in zip(converted, seed.documents, strict=True):
        assert left.text == right.text
        assert left.entity_id == right.entity_id
        assert left.knowledge_version == right.knowledge_version


def test_search_is_deterministic(seed: SeedCorpus) -> None:
    first = [hit.document.chunk_id for hit in seed.search("cardiologist Chicago", top_k=10)]
    second = [hit.document.chunk_id for hit in seed.search("cardiologist Chicago", top_k=10)]
    assert first == second
    assert first, "the corpus has cardiologists in Chicago"


def test_search_returns_nothing_for_a_query_the_corpus_cannot_satisfy(seed: SeedCorpus) -> None:
    """A fallback that always returns something is not a fallback, it is an answer generator."""
    assert seed.search("zzqqxx vvwwyy") == []


def test_a_term_present_in_every_document_cannot_constitute_a_match(seed: SeedCorpus) -> None:
    """Otherwise the local scorer matches the whole directory for any sentence of ordinary words.

    "at" occurs in all 28 fixture documents' text, so its document frequency is 28/28. Scored
    normally it would contribute a positive value to *every* document — the inverse document
    frequency is floored at 1.0 — and a query of ordinary words would "find" the entire
    directory instead of handing off.
    """
    assert seed._document_frequency["text:at"] == len(seed.documents)
    assert seed.search("at") == []


def test_distance_comes_from_the_location_coordinates(seed: SeedCorpus) -> None:
    hits = seed.search("Chicago", filters={"entity_type": ["location"]}, top_k=10, near=CHICAGO)
    assert hits
    for hit in hits:
        coordinate = hit.document.coordinate
        assert coordinate is not None
        expected = haversine_km(CHICAGO, coordinate)
        assert hit.distance_km == pytest.approx(expected)
        assert 0.0 < hit.distance_km < 100.0, "metro-area distances, not similarity scores"


def test_the_fixture_corpus_is_geolocated(seed: SeedCorpus) -> None:
    """Why distance works at all here: every entity resolves to a coordinate."""
    assert all(document.coordinate is not None for document in seed.documents)


def test_a_document_without_coordinates_reports_no_distance(seed: SeedCorpus) -> None:
    """Every fixture document has a coordinate, so the case is built by removing them."""
    stripped = SeedCorpus(
        [
            SeedDocument(
                chunk_id=document.chunk_id,
                entity_id=document.entity_id,
                entity_type=document.entity_type,
                source_id=document.source_id,
                knowledge_version=document.knowledge_version,
                text=document.text,
                metadata={
                    key: value
                    for key, value in document.metadata.items()
                    if key not in {"geo_lat", "geo_lon"}
                },
            )
            for document in seed.documents
        ],
        knowledge_version=seed.knowledge_version,
    )
    hits = stripped.search("cardiologist", top_k=5, near=CHICAGO)
    assert hits
    assert all(hit.distance_km is None for hit in hits)
    assert all(hit.document.coordinate is None for hit in hits)


def test_no_origin_means_no_distance_is_computed(seed: SeedCorpus) -> None:
    hits = seed.search("Chicago", filters={"entity_type": ["location"]}, top_k=5)
    assert hits
    assert all(hit.distance_km is None for hit in hits)
    assert distance_km(None, hits[0].document.coordinate) is None


def test_the_seed_rung_makes_no_call_even_to_load_itself(blocked_sockets: None) -> None:
    """Sockets are blocked outright: not "no call observed" but "no call possible"."""
    corpus = default_seed_corpus()
    hits = corpus.search("Elena Marquez cardiologist phone number", top_k=5)
    assert hits
    assert "Elena Marquez" in hits[0].document.text


def test_loading_a_missing_corpus_is_an_error_not_an_empty_corpus(tmp_path: Path) -> None:
    missing = tmp_path / "nothing-here.json"
    with pytest.raises(SeedError, match="not found"):
        SeedCorpus.load(missing)


def test_a_corrupt_corpus_is_an_error_not_an_empty_corpus(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(SeedError, match="not valid JSON"):
        SeedCorpus.load(broken)


def test_a_corpus_without_documents_is_an_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"knowledge_version": "kv", "documents": []}), encoding="utf-8")
    # Raw string: `match=` is a regex, so the alternation is deliberate metacharacter use. Ruff's
    # RUF043 is what caught it being written without one.
    with pytest.raises(SeedError, match=r"no usable documents|no documents array"):
        SeedCorpus.load(empty)
