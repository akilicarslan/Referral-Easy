"""The versioned seed corpus: the rung that works with the network down.

This is a committed JSON file holding exactly the documents the index holds — same chunker, same
text, same metadata — minus the embedding vectors. Two consequences, both deliberate:

* **It makes no model call.** The scoring is lexical and local. The requirement is that this rung
  answers with no LLM call at all, and a rung that first embeds the query to rank documents
  would violate it while looking like a fallback.
* **It is deterministic.** Same query, same filters, same ordering, every time and on any
  machine. A fallback whose results wobble cannot be tested, and its evaluation numbers would
  describe the wobble rather than the corpus.

The scoring is a small TF-IDF over the seed documents with the crude stemmer from
:mod:`referraleasy_rag.text`. It is weaker than BM25 by a distance, and that is the honest
character of a rung whose job is to answer *something accurate and sourced* instead of nothing.
It never invents: if no document matches, the result is empty and the caller hands off to a human.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from referraleasy_rag.chunking import (
    CHUNKER_VERSION,
    Chunk,
    chunk_directory,
    corpus_version,
    entity_knowledge_versions,
)
from referraleasy_rag.config import Settings, seed_path
from referraleasy_rag.contracts import load_hospitals, load_locations, load_providers
from referraleasy_rag.geo import Coordinate, distance_km
from referraleasy_rag.text import stem, tokenize


class SeedError(RuntimeError):
    """The seed corpus is missing or unreadable. Never silently treated as an empty corpus."""


#: Bumped when the seed file's own layout changes (not when the corpus changes).
SEED_FORMAT_VERSION = 1

SEED_FILENAME = "seed_directory.v1.json"

#: Field weights for the local scorer. The same intuition as the index boosts: an exact name or
#: phone match is worth more than a topical word appearing in prose.
_FIELD_WEIGHTS: Mapping[str, float] = {
    "display_name": 3.0,
    "contact_phone": 5.0,
    "specialties": 2.0,
    "hospital_names": 1.5,
    "cities": 1.5,
    "text": 1.0,
}

#: A query term present in more than this share of the corpus discriminates nothing. "at" appears
#: in every document here, and because the local scorer floors the inverse document frequency at
#: 1.0 such a term still contributes a positive score to every document — so a query of ordinary
#: words would "match" the whole directory and the hand-off rung would never be reached. Terms
#: above the cut-off are dropped from the query, not from the corpus.
MAX_QUERY_TERM_DOCUMENT_FRACTION = 0.5


@dataclass(frozen=True, slots=True)
class SeedDocument:
    """One document of the seed corpus, shaped exactly like an indexed document."""

    chunk_id: str
    entity_id: str
    entity_type: str
    source_id: str
    knowledge_version: str
    text: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def coordinate(self) -> Coordinate | None:
        latitude = self.metadata.get("geo_lat")
        longitude = self.metadata.get("geo_lon")
        if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            return (float(latitude), float(longitude))
        return None

    def filter_values(self, field_name: str) -> list[str]:
        value = self.metadata.get(field_name)
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)]
        return []

    def searchable_text(self) -> str:
        parts = [self.text]
        for name in ("display_name", "contact_phone"):
            value = self.metadata.get(name)
            if isinstance(value, str):
                parts.append(value)
        for name in ("specialties", "subspecialties", "hospital_names", "cities"):
            value = self.metadata.get(name)
            if isinstance(value, list):
                parts.extend(item for item in value if isinstance(item, str))
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class SeedHit:
    """A seed match: the document, its local score, and its coordinate distance if computable."""

    document: SeedDocument
    score: float
    distance_km: float | None = None


def _weight(weight: float) -> int:
    """A field weight as a whole-number term repeat count."""
    # `round` with no ndigits returns an int, so no cast is needed.
    return max(1, round(weight))


def _repeat(term: str, weight: float) -> list[str]:
    return [term] * _weight(weight)


def build_seed_payload(settings: Settings) -> dict[str, Any]:
    """Derive the seed corpus from the fixtures. Deterministic: no clock, no ordering whims.

    The corpus version is computed from the entities' own `knowledge_version` values, so a
    fixture refresh changes it and an unchanged corpus does not.
    """
    chunks = chunk_directory(
        load_providers(settings.fixtures_dir),
        load_hospitals(settings.fixtures_dir),
        load_locations(settings.fixtures_dir),
    )
    version = corpus_version(chunks)
    versions = entity_knowledge_versions(chunks)
    return {
        "seed_format_version": SEED_FORMAT_VERSION,
        "knowledge_version": version,
        # The full component list, so the short digest above stays verifiable against the
        # fixtures without re-running the chunker.
        "entity_knowledge_versions": versions,
        "chunker_version": CHUNKER_VERSION,
        "generated_from": "fixtures/directory/{providers,hospitals,locations}.json",
        "document_count": len(chunks),
        "documents": [
            chunk.to_document(run_id=version, corpus_version=version) for chunk in chunks
        ],
    }


def write_seed(path: Path, payload: Mapping[str, Any]) -> int:
    """Write the seed canonically: sorted keys, stable separators, trailing newline."""
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")
    return len(serialized)


def serialize_seed(payload: Mapping[str, Any]) -> str:
    """The canonical serialization, exposed so a test can assert determinism directly."""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


class SeedCorpus:
    """The loaded seed corpus and its local, deterministic search."""

    def __init__(
        self,
        documents: Sequence[SeedDocument],
        *,
        knowledge_version: str,
        seed_format_version: int = SEED_FORMAT_VERSION,
    ) -> None:
        self.documents = tuple(documents)
        self.knowledge_version = knowledge_version
        self.seed_format_version = seed_format_version
        self._tokens = [Counter(self._document_tokens(document)) for document in self.documents]
        self._document_frequency = self._compute_document_frequency()

    @classmethod
    def load(cls, path: Path | None = None) -> SeedCorpus:
        """Read the committed seed. Raises rather than returning an empty corpus."""
        location = path or seed_path()
        if not location.is_file():
            raise SeedError(f"seed corpus not found: {location}")
        try:
            payload = json.loads(location.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SeedError(f"seed corpus is not valid JSON: {location}: {exc}") from exc
        if not isinstance(payload, dict):
            raise SeedError(f"seed corpus must be a JSON object: {location}")
        raw_documents = payload.get("documents")
        if not isinstance(raw_documents, list):
            raise SeedError(f"seed corpus carries no documents array: {location}")
        documents: list[SeedDocument] = []
        for raw in raw_documents:
            if not isinstance(raw, dict):
                continue
            chunk_id = raw.get("chunk_id")
            entity_id = raw.get("entity_id")
            text = raw.get("text")
            if not all(isinstance(value, str) for value in (chunk_id, entity_id, text)):
                raise SeedError(f"seed document without chunk_id/entity_id/text: {raw!r}")
            metadata = {
                key: value
                for key, value in raw.items()
                if key
                not in {
                    "chunk_id",
                    "entity_id",
                    "entity_type",
                    "source_id",
                    "knowledge_version",
                    "text",
                    "run_id",
                    "corpus_version",
                    "chunker_version",
                    "updated_at",
                    "ordinal",
                }
            }
            documents.append(
                SeedDocument(
                    chunk_id=str(chunk_id),
                    entity_id=str(entity_id),
                    entity_type=str(raw.get("entity_type", "")),
                    source_id=str(raw.get("source_id", "")),
                    knowledge_version=str(raw.get("knowledge_version", "")),
                    text=str(text),
                    metadata=metadata,
                )
            )
        if not documents:
            raise SeedError(f"seed corpus contains no usable documents: {location}")
        version = payload.get("knowledge_version")
        return cls(
            documents,
            knowledge_version=version if isinstance(version, str) else "unknown",
            seed_format_version=int(payload.get("seed_format_version", SEED_FORMAT_VERSION)),
        )

    def _document_tokens(self, document: SeedDocument) -> list[str]:
        """Weighted terms for one document: ``field:token`` repeated by the field's weight."""
        tokens: list[str] = []
        for token in tokenize(document.text):
            tokens.extend(_repeat(f"text:{stem(token)}", _FIELD_WEIGHTS["text"]))
        for name, weight in _FIELD_WEIGHTS.items():
            if name == "text":
                continue
            for value in document.filter_values(name):
                for token in tokenize(value):
                    tokens.extend(_repeat(f"{name}:{stem(token)}", weight))
        return tokens

    def _query_terms(self, query: str) -> Counter[str]:
        """The same weighted vocabulary, from the query side.

        A query token is offered to every field, so "Marquez" matches the name field at the
        name field's weight and "cardiology" matches the specialty field at its own. Without
        this the document-side weights would be inert: terms nobody ever queries.

        Terms carried by more than :data:`MAX_QUERY_TERM_DOCUMENT_FRACTION` of the corpus are
        skipped: they cannot separate one document from another, and keeping them would make
        every directory entry a "match" for a query made of ordinary words.
        """
        terms: Counter[str] = Counter()
        cutoff = max(1, int(len(self.documents) * MAX_QUERY_TERM_DOCUMENT_FRACTION))
        for token in tokenize(query):
            stemmed = stem(token)
            for name, weight in _FIELD_WEIGHTS.items():
                term = f"{name}:{stemmed}"
                if self._document_frequency.get(term, 0) > cutoff:
                    continue
                terms[term] += _weight(weight)
        return terms

    def _compute_document_frequency(self) -> Counter[str]:
        frequency: Counter[str] = Counter()
        for counter in self._tokens:
            frequency.update(counter.keys())
        return frequency

    def _idf(self, term: str) -> float:
        total = len(self.documents)
        return math.log((total + 1) / (self._document_frequency.get(term, 0) + 1)) + 1.0

    def search(
        self,
        query: str,
        *,
        filters: Mapping[str, Any] | None = None,
        top_k: int = 5,
        entity_types: Sequence[str] | None = None,
        near: Coordinate | None = None,
    ) -> list[SeedHit]:
        """Deterministically rank documents for a query.

        Filters are applied *before* scoring, and use the same field names and the same
        all-must-match semantics as the index's filter clause, so a degraded answer cannot
        quietly widen the search the user narrowed.
        """
        terms = self._query_terms(query)
        if not terms:
            return []
        candidates = [
            (index, document)
            for index, document in enumerate(self.documents)
            if self._matches(document, filters, entity_types)
        ]
        scored: list[SeedHit] = []
        for index, document in candidates:
            counter = self._tokens[index]
            score = 0.0
            for term, query_count in terms.items():
                frequency = counter.get(term, 0)
                if frequency:
                    score += query_count * (1.0 + math.log(frequency)) * self._idf(term)
            if score <= 0.0:
                continue
            distance = distance_km(near, document.coordinate) if near is not None else None
            scored.append(SeedHit(document=document, score=score, distance_km=distance))
        # Deterministic ordering: score, then distance when there is one, then the chunk id.
        scored.sort(
            key=lambda hit: (
                -hit.score,
                hit.distance_km if hit.distance_km is not None else float("inf"),
                hit.document.chunk_id,
            )
        )
        return scored[:top_k]

    def _matches(
        self,
        document: SeedDocument,
        filters: Mapping[str, Any] | None,
        entity_types: Sequence[str] | None,
    ) -> bool:
        if entity_types and document.entity_type not in entity_types:
            return False
        if not filters:
            return True
        for name, value in filters.items():
            if value is None or value == "" or value == []:
                continue
            wanted = {
                str(item) for item in (value if isinstance(value, (list, tuple, set)) else [value])
            }
            if name == "entity_type":
                if document.entity_type not in wanted:
                    return False
                continue
            available = set(document.filter_values(name))
            if not available & wanted:
                return False
        return True


def default_seed_corpus() -> SeedCorpus:
    """Load the committed seed corpus from its package-data location."""
    return SeedCorpus.load(seed_path())


def seed_documents_from_chunks(chunks: Iterable[Chunk], *, version: str) -> list[SeedDocument]:
    """Adapt chunks to seed documents — used by the round-trip test between the two rungs."""
    return [
        SeedDocument(
            chunk_id=chunk.chunk_id,
            entity_id=chunk.entity_id,
            entity_type=chunk.entity_type,
            source_id=chunk.source_id,
            knowledge_version=chunk.knowledge_version,
            text=chunk.text,
            metadata=dict(chunk.metadata),
        )
        for chunk in chunks
    ]


__all__ = [
    "SEED_FILENAME",
    "SEED_FORMAT_VERSION",
    "SeedCorpus",
    "SeedDocument",
    "SeedError",
    "SeedHit",
    "build_seed_payload",
    "default_seed_corpus",
    "seed_documents_from_chunks",
    "serialize_seed",
    "write_seed",
]
