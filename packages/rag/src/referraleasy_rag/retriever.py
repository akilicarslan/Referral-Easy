"""The hybrid retriever.

The pipeline, in order, and each step is here rather than spread across callers:

1. **Both arms take the same filters.** ``Filters.to_clause()`` is called once and the resulting
   mapping object is handed to :func:`build_bm25_query` and :func:`build_knn_query` unchanged.
   There is no second place where filters are assembled, so the arms cannot drift.
2. **BM25 top-20 and kNN top-20**, each deduplicated inside its own arm.
3. **RRF** over the two ranked id lists.
4. **Rerank the top 10**, then return a sourced top 3-5.
5. On any failure, descend the ladder (see :mod:`referraleasy_rag.degradation`) and say so in the
   result. Nothing here silently falls back to a shallower pipeline while reporting success.
6. If nothing is found at all, hand off to a human. There is no branch that synthesises an
   answer: "we could not find a source" is a result.

Distance is never taken from a score. When the caller supplies a coordinate, each result's
distance is computed from stored coordinates by :mod:`referraleasy_rag.geo`, and a result without
coordinates reports ``None`` rather than a number.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from referraleasy_rag.authority import (
    ClaimScan,
    CoverageLookup,
    CoverageResolution,
    TextClaim,
    resolve_coverage,
    strip_availability_claims,
)
from referraleasy_rag.config import Settings
from referraleasy_rag.degradation import CallLedger, LadderState, Rung
from referraleasy_rag.embeddings import EmbeddingProvider, EmbeddingUnavailableError
from referraleasy_rag.geo import Coordinate, Located, distance_km, sort_by_distance
from referraleasy_rag.opensearch_index import (
    IndexUnavailableError,
    SearchClient,
    build_bm25_query,
    build_knn_query,
    hits_from_response,
    is_outage,
)
from referraleasy_rag.rerank import RerankClient, RerankUnavailableError
from referraleasy_rag.rrf import RankedHit, deduplicate, reciprocal_rank_fusion
from referraleasy_rag.seed import SeedCorpus, SeedHit
from referraleasy_rag.text import normalize_text

#: Arm names, used both in the RRF input mapping and in each result's provenance.
ARM_BM25 = "bm25"
ARM_VECTOR = "vector"
ARM_SEED = "seed"


@dataclass(frozen=True, slots=True)
class Filters:
    """What the user narrowed the search to. Applied identically to every arm.

    ``specialty`` and ``languages`` map to the ``specialties`` and ``languages`` keyword arrays,
    which every entity type carries (a hospital's specialties are the specialties of the
    clinicians who practice there, derived at chunk time). One vocabulary for all three entity
    types is what makes a single filter clause correct for both arms *and* for the seed rung.
    """

    specialty: str | None = None
    languages: tuple[str, ...] = ()
    hospital_ids: tuple[str, ...] = ()
    cities: tuple[str, ...] = ()
    entity_types: tuple[str, ...] = ()

    def to_clause(self) -> dict[str, Any]:
        """The one clause both arms receive. Empty means "no filtering"."""
        clause: dict[str, Any] = {}
        if self.specialty:
            clause["specialties"] = [self.specialty]
        if self.languages:
            clause["languages"] = list(self.languages)
        if self.hospital_ids:
            clause["hospital_ids"] = list(self.hospital_ids)
        if self.cities:
            clause["cities"] = list(self.cities)
        if self.entity_types:
            clause["entity_type"] = list(self.entity_types)
        return clause

    @property
    def is_empty(self) -> bool:
        return not self.to_clause()

    def describe(self) -> str:
        clause = self.to_clause()
        if not clause:
            return "none"
        return ", ".join(f"{key}={value}" for key, value in sorted(clause.items()))


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """A retrieved, citable piece of the directory."""

    chunk_id: str
    entity_id: str
    entity_type: str
    source_id: str
    knowledge_version: str
    text: str
    score: float
    rank: int
    arms: tuple[str, ...] = ()
    rrf_score: float | None = None
    rerank_score: float | None = None
    distance_km: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    corpus_version: str = ""

    @property
    def citation(self) -> str:
        """Where this text came from. Every returned chunk has one, by construction."""
        return f"{self.source_id}#{self.chunk_id}"

    @property
    def coordinate(self) -> Coordinate | None:
        latitude = self.metadata.get("geo_lat")
        longitude = self.metadata.get("geo_lon")
        if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            return (float(latitude), float(longitude))
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "chunk_id": self.chunk_id,
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "citation": self.citation,
            "knowledge_version": self.knowledge_version,
            "text": self.text,
            "score": self.score,
            "rrf_score": self.rrf_score,
            "rerank_score": self.rerank_score,
            "arms": list(self.arms),
            "distance_km": self.distance_km,
        }


@dataclass(frozen=True, slots=True)
class GroundedPassage:
    """Text prepared for a model: citable, and free of availability assertions."""

    chunk_id: str
    source_id: str
    entity_id: str
    citation: str
    text: str
    removed_claims: tuple[TextClaim, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "citation": self.citation,
            "entity_id": self.entity_id,
            "text": self.text,
            "removed_claims": [claim.to_dict() for claim in self.removed_claims],
        }


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Everything a caller needs: the sources, how they were found, and what went wrong."""

    query: str
    results: tuple[RetrievedChunk, ...]
    ladder: LadderState
    filters_applied: Mapping[str, Any] = field(default_factory=dict)
    index_name: str = ""
    index_knowledge_version: str | None = None
    seed_knowledge_version: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def rung(self) -> Rung:
        return self.ladder.rung

    @property
    def degraded(self) -> bool:
        return self.ladder.degraded

    @property
    def requires_human(self) -> bool:
        return self.ladder.requires_human

    @property
    def citations(self) -> list[str]:
        return [chunk.citation for chunk in self.results]

    def grounding_context(self) -> list[GroundedPassage]:
        """Sourced passages for a model, with availability assertions removed and reported.

        The removal happens here rather than at index time so that the index stays a faithful
        record of what the directory says. What reaches the model is what the directory is
        allowed to claim.
        """
        passages: list[GroundedPassage] = []
        for chunk in self.results:
            scan: ClaimScan = strip_availability_claims(chunk.text, chunk_id=chunk.chunk_id)
            passages.append(
                GroundedPassage(
                    chunk_id=chunk.chunk_id,
                    source_id=chunk.source_id,
                    entity_id=chunk.entity_id,
                    citation=chunk.citation,
                    text=scan.text,
                    removed_claims=scan.removed,
                )
            )
        return passages

    def stripped_claims(self) -> list[TextClaim]:
        """Every availability or booking assertion that was removed from the grounding text."""
        claims: list[TextClaim] = []
        for passage in self.grounding_context():
            claims.extend(passage.removed_claims)
        return claims

    def coverage(
        self,
        lookup: CoverageLookup,
        *,
        plan_id: str | None,
        provider_id: str | None,
        hospital_id: str | None,
    ) -> CoverageResolution:
        """The authoritative coverage facts, with any retrieved text that contradicts them."""
        record = lookup.get(plan_id=plan_id, provider_id=provider_id, hospital_id=hospital_id)
        return resolve_coverage(
            record,
            [(chunk.chunk_id, chunk.entity_id, chunk.text) for chunk in self.results],
        )

    def sorted_by_distance(self, origin: Coordinate | None) -> tuple[RetrievedChunk, ...]:
        """Reorder by real distance from ``origin``. Results without coordinates sort last."""
        if origin is None:
            return self.results
        ordered = sort_by_distance(
            [Located(value=chunk, coordinate=chunk.coordinate) for chunk in self.results], origin
        )
        return tuple(
            replace(chunk, distance_km=distance, rank=position + 1)
            for position, (chunk, distance) in enumerate(ordered)
        )

    def summary_line(self) -> str:
        return f"{len(self.results)} result(s) for {self.query!r} — {self.ladder.summary_line()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "ladder": self.ladder.to_dict(),
            "filters": dict(self.filters_applied),
            "index_name": self.index_name,
            "index_knowledge_version": self.index_knowledge_version,
            "seed_knowledge_version": self.seed_knowledge_version,
            "citations": self.citations,
            "notes": list(self.notes),
            "results": [chunk.to_dict() for chunk in self.results],
        }


class HybridRetriever:
    """The pipeline as an object with injectable dependencies.

    Every collaborator is a constructor argument because the ladder has to be *provable*: a test
    makes the index unreachable, the embedder refuse and the reranker 404, and then asserts which
    rung the answer came from. A retriever that builds its own clients cannot be tested that way,
    and an untestable fallback is an unverified fallback.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: SearchClient | None,
        embedder: EmbeddingProvider,
        reranker: RerankClient,
        seed: SeedCorpus,
        index_name: str | None = None,
        rerank_enabled: bool = True,
    ) -> None:
        self._settings = settings
        self._client = client
        self._embedder = embedder
        self._reranker = reranker
        self._seed = seed
        self._index_name = index_name or settings.index_name
        self._rerank_enabled = rerank_enabled

    # ---------------------------------------------------------------- public API

    def search(
        self,
        query: str,
        *,
        filters: Filters | None = None,
        top_k: int | None = None,
        near: Coordinate | None = None,
    ) -> RetrievalResult:
        normalized = normalize_text(query)
        applied = filters or Filters()
        clause = applied.to_clause()
        limit = top_k or self._settings.result_top_k
        ledger = CallLedger()
        ladder = LadderState(ledger=ledger)
        notes: list[str] = []
        documents: dict[str, Mapping[str, Any]] = {}

        if not normalized:
            ladder.hand_off("empty_query", "the query was empty after normalization")
            return self._result(
                query=normalized, results=(), ladder=ladder, clause=clause, notes=tuple(notes)
            )

        # ---- lexical arm -----------------------------------------------------
        try:
            bm25_hits = self._lexical_arm(normalized, clause, ledger)
        except Exception as exc:
            if not _is_index_failure(exc):
                raise
            ladder.degrade(Rung.SEED_FALLBACK, "opensearch_unavailable", str(exc))
            return self._seed_result(
                query=normalized,
                clause=clause,
                limit=limit,
                near=near,
                ladder=ladder,
                notes=tuple(notes),
            )
        for hit in bm25_hits:
            documents[hit.item_id] = _payload(hit)

        # ---- vector arm ------------------------------------------------------
        vector_hits: list[RankedHit] = []
        try:
            query_vector = self._embedder.embed_query(normalized)
            # The offline embedder is a deterministic hash, not a model call, and it makes no
            # request. Recording it as a model call would make the ledger lie in the one place it
            # exists to be trusted: the claim that the seed rung costs no model call at all.
            is_model = not self._embedder.synthetic
            ledger.record(
                kind="embedding.query",
                detail=self._embedder.model_id,
                network=is_model,
                model=is_model,
            )
        except EmbeddingUnavailableError as exc:
            ladder.degrade(Rung.BM25_ONLY, "embeddings_unavailable", str(exc))
            notes.append("vector arm skipped: the embedding provider is unavailable")
        else:
            try:
                vector_hits = self._vector_arm(query_vector, clause, ledger)
            except Exception as exc:
                if not _is_index_failure(exc):
                    raise
                # The index died after a model call was spent. The seed rung must cost none, so
                # it is not available here: answer from the lexical arm, or hand off.
                if not bm25_hits:
                    ladder.hand_off("index_lost_mid_retrieval", str(exc))
                    return self._result(
                        query=normalized,
                        results=(),
                        ladder=ladder,
                        clause=clause,
                        notes=tuple(notes),
                    )
                ladder.degrade(Rung.BM25_ONLY, "index_lost_mid_retrieval", str(exc))
                notes.append("vector arm lost the index mid-retrieval")
            for hit in vector_hits:
                documents[hit.item_id] = _payload(hit)

        # ---- dedup, then RRF -------------------------------------------------
        arms = {
            ARM_BM25: [hit.item_id for hit in deduplicate(bm25_hits)],
            ARM_VECTOR: [hit.item_id for hit in deduplicate(vector_hits)],
        }
        fused = reciprocal_rank_fusion(arms, k=self._settings.rrf_k)
        rrf_scores = dict(fused)
        candidates = [chunk_id for chunk_id, _ in fused][: self._settings.rerank_candidates]
        if len(fused) > len(candidates):
            notes.append(
                f"{len(fused)} fused candidates, the top {len(candidates)} were carried forward"
            )

        # ---- rerank ----------------------------------------------------------
        rerank_scores: dict[str, float] = {}
        ordered = list(candidates)
        if ladder.rung is Rung.HYBRID_RERANK:
            if not self._rerank_enabled:
                ladder.degrade(Rung.HYBRID_RRF, "rerank_disabled", "disabled by the caller")
            else:
                try:
                    ordered, rerank_scores = self._rerank(
                        normalized, candidates, documents, limit, ledger
                    )
                except RerankUnavailableError as exc:
                    ladder.degrade(Rung.HYBRID_RRF, "reranker_unavailable", str(exc))
                    notes.append("RRF ordering kept: the reranker is unavailable")

        # ---- assemble --------------------------------------------------------
        results = self._assemble(
            ordered=ordered,
            documents=documents,
            arms=arms,
            rrf_scores=rrf_scores,
            rerank_scores=rerank_scores,
            near=near,
            limit=limit,
        )
        if not results:
            ladder.hand_off(
                "no_source_found",
                f"no directory chunk matched {normalized!r} with filters {applied.describe()}",
            )
        elif len(results) < self._settings.min_result_top_k:
            notes.append(
                f"thin evidence: {len(results)} source(s), fewer than the "
                f"{self._settings.min_result_top_k} expected"
            )
        return self._result(
            query=normalized, results=results, ladder=ladder, clause=clause, notes=tuple(notes)
        )

    # ---------------------------------------------------------------- arms

    def _lexical_arm(
        self, query: str, clause: Mapping[str, Any], ledger: CallLedger
    ) -> list[RankedHit]:
        if self._client is None:
            raise IndexUnavailableError("no search client was configured")
        body = build_bm25_query(query, clause, self._settings.arm_top_k)
        ledger.record(kind="opensearch.bm25", detail=self._index_name)
        response = self._client.search(index=self._index_name, body=body)
        return _ranked_hits(response, arm=ARM_BM25)

    def _vector_arm(
        self, vector: Sequence[float], clause: Mapping[str, Any], ledger: CallLedger
    ) -> list[RankedHit]:
        if self._client is None:
            raise IndexUnavailableError("no search client was configured")
        body = build_knn_query(vector, clause, self._settings.arm_top_k)
        ledger.record(kind="opensearch.knn", detail=self._index_name)
        response = self._client.search(index=self._index_name, body=body)
        return _ranked_hits(response, arm=ARM_VECTOR)

    def _rerank(
        self,
        query: str,
        candidates: Sequence[str],
        documents: Mapping[str, Mapping[str, Any]],
        limit: int,
        ledger: CallLedger,
    ) -> tuple[list[str], dict[str, float]]:
        texts = [_text_of(documents.get(chunk_id, {})) for chunk_id in candidates]
        ledger.record(kind="rerank", detail=self._reranker.model_id, model=True)
        hits = self._reranker.rerank(query, texts, top_n=limit)
        ordered: list[str] = []
        scores: dict[str, float] = {}
        for hit in hits:
            if 0 <= hit.index < len(candidates):
                chunk_id = candidates[hit.index]
                if chunk_id not in scores:
                    ordered.append(chunk_id)
                    scores[chunk_id] = hit.score
        # Anything the reranker did not return keeps its RRF position behind the reranked ones:
        # dropping it would silently discard a document both arms agreed on.
        ordered.extend(chunk_id for chunk_id in candidates if chunk_id not in scores)
        return ordered, scores

    # ---------------------------------------------------------------- seed rung

    def _seed_result(
        self,
        *,
        query: str,
        clause: Mapping[str, Any],
        limit: int,
        near: Coordinate | None,
        ladder: LadderState,
        notes: tuple[str, ...],
    ) -> RetrievalResult:
        seed_ledger = CallLedger()
        hits = self._seed.search(query, filters=clause, top_k=limit, near=near)
        ladder.attach_seed_ledger(seed_ledger)
        notes = (*notes, "answered from the committed seed corpus")
        results = tuple(
            _retrieved_from_seed(hit, rank=position + 1, near=near)
            for position, hit in enumerate(hits)
        )
        if not results:
            ladder.hand_off(
                "no_source_found_in_seed",
                f"the seed corpus holds no document matching {query!r}",
            )
        return self._result(query=query, results=results, ladder=ladder, clause=clause, notes=notes)

    # ---------------------------------------------------------------- assembly

    def _assemble(
        self,
        *,
        ordered: Sequence[str],
        documents: Mapping[str, Mapping[str, Any]],
        arms: Mapping[str, Sequence[str]],
        rrf_scores: Mapping[str, float],
        rerank_scores: Mapping[str, float],
        near: Coordinate | None,
        limit: int,
    ) -> tuple[RetrievedChunk, ...]:
        arm_sets = {arm: set(ids) for arm, ids in arms.items()}
        chunks: list[RetrievedChunk] = []
        for position, chunk_id in enumerate(ordered[:limit]):
            document = documents.get(chunk_id)
            if document is None:
                continue
            metadata = _metadata_of(document)
            coordinate = _coordinate_of(metadata)
            chunks.append(
                RetrievedChunk(
                    chunk_id=chunk_id,
                    entity_id=_text_of(document, "entity_id"),
                    entity_type=_text_of(document, "entity_type"),
                    source_id=_text_of(document, "source_id"),
                    knowledge_version=_text_of(document, "knowledge_version"),
                    text=_text_of(document, "text"),
                    score=rerank_scores.get(chunk_id, rrf_scores.get(chunk_id, 0.0)),
                    rank=position + 1,
                    arms=tuple(arm for arm, ids in arm_sets.items() if chunk_id in ids),
                    rrf_score=rrf_scores.get(chunk_id),
                    rerank_score=rerank_scores.get(chunk_id),
                    # Distance comes from coordinates, or it is not reported at all.
                    distance_km=distance_km(near, coordinate) if near is not None else None,
                    metadata=metadata,
                    corpus_version=_text_of(document, "corpus_version"),
                )
            )
        return tuple(chunks)

    def _result(
        self,
        *,
        query: str,
        results: Sequence[RetrievedChunk],
        ladder: LadderState,
        clause: Mapping[str, Any],
        notes: tuple[str, ...],
    ) -> RetrievalResult:
        from_seed = ladder.rung is Rung.SEED_FALLBACK
        return RetrievalResult(
            query=query,
            results=tuple(results),
            ladder=ladder,
            filters_applied=dict(clause),
            index_name=self._index_name,
            index_knowledge_version=(
                None if from_seed or not results else (results[0].corpus_version or None)
            ),
            seed_knowledge_version=self._seed.knowledge_version,
            notes=notes,
        )


# -------------------------------------------------------------------- helpers


def _is_index_failure(exc: BaseException) -> bool:
    """An outage, not a bug. Only outages are allowed to move the ladder."""
    return isinstance(exc, IndexUnavailableError) or is_outage(exc)


def _ranked_hits(response: Mapping[str, Any], *, arm: str) -> list[RankedHit]:
    hits: list[RankedHit] = []
    for position, entry in enumerate(hits_from_response(response)):
        chunk_id = entry.get("_id")
        if not isinstance(chunk_id, str):
            continue
        document = entry.get("chunk")
        hits.append(
            RankedHit(
                item_id=chunk_id,
                arm=arm,
                rank=position + 1,
                text=_text_of(document if isinstance(document, Mapping) else {}, "text"),
                payload=document if isinstance(document, Mapping) else None,
            )
        )
    return hits


def _payload(hit: RankedHit) -> Mapping[str, Any]:
    return hit.payload or {}


def _text_of(document: Mapping[str, Any], key: str = "text") -> str:
    value = document.get(key)
    return value if isinstance(value, str) else ""


def _metadata_of(document: Mapping[str, Any]) -> Mapping[str, Any]:
    """The document's metadata fields, i.e. everything that is not bookkeeping."""
    reserved = {
        "chunk_id",
        "entity_id",
        "entity_type",
        "source_id",
        "knowledge_version",
        "updated_at",
        "ordinal",
        "run_id",
        "corpus_version",
        "chunker_version",
        "text",
        "embedding",
    }
    return {key: value for key, value in document.items() if key not in reserved}


def _coordinate_of(metadata: Mapping[str, Any]) -> Coordinate | None:
    latitude = metadata.get("geo_lat")
    longitude = metadata.get("geo_lon")
    if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
        return (float(latitude), float(longitude))
    return None


def _retrieved_from_seed(hit: SeedHit, *, rank: int, near: Coordinate | None) -> RetrievedChunk:
    document = hit.document
    return RetrievedChunk(
        chunk_id=document.chunk_id,
        entity_id=document.entity_id,
        entity_type=document.entity_type,
        source_id=document.source_id,
        knowledge_version=document.knowledge_version,
        text=document.text,
        score=hit.score,
        rank=rank,
        arms=(ARM_SEED,),
        rrf_score=None,
        rerank_score=None,
        distance_km=hit.distance_km if near is not None else None,
        metadata=document.metadata,
    )


__all__ = [
    "ARM_BM25",
    "ARM_SEED",
    "ARM_VECTOR",
    "Filters",
    "GroundedPassage",
    "HybridRetriever",
    "RetrievalResult",
    "RetrievedChunk",
]
