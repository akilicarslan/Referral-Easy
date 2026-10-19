"""Entity-aware chunking: the name <-> phone relation, and what a chunk is allowed to contain."""

from __future__ import annotations

import pytest

from referraleasy_rag.chunking import (
    CHUNKER_VERSION,
    HARD_MAX_CHUNK_TOKENS,
    MAX_CHUNK_TOKENS,
    RESERVED_DOCUMENT_FIELDS,
    Chunk,
    ChunkError,
    _chunk_id,
    chunk_directory,
    chunk_provider,
    corpus_version,
    entity_knowledge_versions,
    validate_name_phone_pairing,
)
from referraleasy_rag.opensearch_index import DOCUMENT_METADATA_FIELDS
from referraleasy_rag.text import estimate_tokens


def _long_profile(name: str, sentences: int) -> str:
    """A profile long enough to force splitting, with the name only in the first sentence."""
    repeats = [
        "The clinic runs on Tuesdays and Thursdays only.",
        "Referrals are reviewed by the intake physician before a slot is offered.",
        "Interpretation can be arranged with two business days notice.",
        "The practice keeps a waiting list for cancellations.",
        "Follow-up appointments are booked at the front desk after the visit.",
    ]
    body = " ".join(repeats[index % len(repeats)] for index in range(sentences))
    return f"{name} practices general cardiology at the downtown site. {body}"


def test_every_fixture_chunk_satisfies_the_name_phone_invariant(chunks: list[Chunk]) -> None:
    """The corpus the index is built from must not separate a name from its phone number."""
    assert validate_name_phone_pairing(chunks) == []


def test_name_and_phone_are_in_the_same_chunk(chunks: list[Chunk]) -> None:
    with_phone = [chunk for chunk in chunks if chunk.contact_phone]
    assert with_phone, "the fixture corpus should contain providers with phone numbers"
    for chunk in with_phone:
        name = chunk.metadata["display_name"]
        assert isinstance(name, str)
        assert name in chunk.text
        assert chunk.contact_phone in chunk.text


def test_a_long_profile_splits_and_keeps_the_contact_block_intact() -> None:
    """The failure mode this guards against: a split that leaves a phone number orphaned.

    The corpus is small enough that most providers fit in one chunk; this uses an inflated
    profile to drive the packer over its budget on purpose.
    """
    provider = {
        "provider_id": "30000000-0000-4000-8000-000000000099",
        "display_name": "Nadia Okafor",
        "credentials": "MD",
        "specialty": "cardiology",
        "subspecialties": ["heart failure"],
        "languages": [{"language": "en", "fluency": "native"}],
        "accepts_new_patients": True,
        "accessibility_features": ["wheelchair_accessible"],
        "first_visit_duration_minutes": 60,
        "return_visit_duration_minutes": 20,
        "hospital_ids": [],
        "accepts_insurance": ["Northbridge Health"],
        "profile_text": _long_profile("Nadia Okafor", 40),
        "contact_phone": "+13125559999",
        "contact_source": {"source_id": "provider_directory:test", "field_path": "contact_phone"},
        "knowledge_version": "kv-prov-test",
        "updated_at": "2026-09-01T09:00:00Z",
        "synthetic": True,
    }
    produced = chunk_provider(provider, hospitals={}, locations={})

    assert len(produced) > 1, "a long profile should be split, not emitted as one oversized chunk"
    assert validate_name_phone_pairing(produced) == []
    for chunk in produced:
        assert "Nadia Okafor" in chunk.text, "no chunk may be anonymous"
        assert estimate_tokens(chunk.text) <= MAX_CHUNK_TOKENS + 40
    carrying = [chunk for chunk in produced if "+13125559999" in chunk.text]
    assert len(carrying) == 1
    assert "Nadia Okafor" in carrying[0].text


def test_an_atom_larger_than_the_model_limit_is_refused() -> None:
    """Better to fail than to truncate: half a fact is a wrong fact."""
    huge = "a " * (HARD_MAX_CHUNK_TOKENS * 8)
    provider = {
        "provider_id": "30000000-0000-4000-8000-000000000098",
        "display_name": "Huge Record",
        "specialty": "cardiology",
        "profile_text": huge,
        "knowledge_version": "kv-prov-test",
    }
    with pytest.raises(ChunkError, match="maximum sequence length"):
        chunk_provider(provider, hospitals={}, locations={})


def test_chunks_are_deterministic_and_ids_depend_on_the_text() -> None:
    assert chunk_directory([], [], []) == []
    # Ids are a hash of entity, ordinal, version and text: re-indexing overwrites rather than
    # duplicating, and a chunk whose text changed gets a new id (and the old one is pruned).
    assert _chunk_id("provider", "p1", 0, "kv1", "text one") == _chunk_id(
        "provider", "p1", 0, "kv1", "text one"
    )
    assert _chunk_id("provider", "p1", 0, "kv1", "text one") != _chunk_id(
        "provider", "p1", 0, "kv1", "text two"
    )


def test_metadata_may_not_override_document_fields() -> None:
    chunk = Chunk(
        chunk_id="c1",
        entity_id="e1",
        entity_type="provider",
        source_id="s1",
        knowledge_version="kv1",
        updated_at="2026-09-01T09:00:00Z",
        ordinal=0,
        text="text",
        metadata={"source_id": "spoofed"},
    )
    with pytest.raises(ChunkError, match="may not override"):
        chunk.to_document(run_id="r", corpus_version="c")
    assert "source_id" in RESERVED_DOCUMENT_FIELDS


def test_documents_omit_an_absent_updated_at() -> None:
    """`updated_at` is mapped as a date; an empty string is not one."""
    chunk = Chunk(
        chunk_id="c1",
        entity_id="e1",
        entity_type="location",
        source_id="s1",
        knowledge_version="kv1",
        updated_at="",
        ordinal=0,
        text="text",
    )
    assert "updated_at" not in chunk.to_document(run_id="r", corpus_version="c")


def test_chunk_metadata_is_covered_by_the_index_mapping(chunks: list[Chunk]) -> None:
    """A metadata key nobody added to the mapping would be silently unsearchable."""
    produced = {key for chunk in chunks for key in chunk.metadata}
    unknown = sorted(produced - set(DOCUMENT_METADATA_FIELDS))
    assert unknown == [], f"chunk metadata not present in the index mapping: {unknown}"


def test_corpus_version_is_deterministic_and_describes_its_inputs(chunks: list[Chunk]) -> None:
    version = corpus_version(chunks)
    assert version == corpus_version(chunks)
    assert version.startswith(CHUNKER_VERSION)
    assert f"{len(entity_knowledge_versions(chunks))}v" in version


def test_slots_are_not_chunked(chunks: list[Chunk]) -> None:
    """Availability is not a property retrieval may assert, so slots are not indexable text."""
    assert {chunk.entity_type for chunk in chunks} == {"provider", "hospital", "location"}
