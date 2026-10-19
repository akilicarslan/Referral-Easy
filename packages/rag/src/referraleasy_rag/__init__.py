"""Hybrid retrieval over the synthetic directory.

The pieces, in the order the pipeline uses them:

``chunking``   entities are chunked so that a name never loses its phone number
``embeddings`` one adapter interface, so the corpus and the queries cannot drift apart
``opensearch_index`` one index holding text *and* a 768-dimension kNN field, so the hybrid
               query is a single request
``retriever``  BM25 top-20 + kNN top-20 with the same filters, dedup, RRF, rerank, sourced top-k
``degradation`` the ladder, as an explicit state machine rather than a chain of try/except
``seed``       the versioned JSON rung: deterministic, filtered, and free of model calls
``authority``  structured records outrank retrieved text, and the conflict stays visible
``geo``        distance from coordinates, never from a similarity score
"""

from __future__ import annotations

from referraleasy_rag.chunking import CHUNKER_VERSION, Chunk, chunk_directory, corpus_version
from referraleasy_rag.config import Settings, load_settings
from referraleasy_rag.degradation import Degradation, LadderState, Rung
from referraleasy_rag.embeddings import (
    EmbeddingProvider,
    EmbeddingUnavailableError,
    OpenRouterEmbeddingProvider,
    build_embedding_provider,
)
from referraleasy_rag.retriever import Filters, HybridRetriever, RetrievalResult, RetrievedChunk
from referraleasy_rag.rrf import reciprocal_rank_fusion

__all__ = [
    "CHUNKER_VERSION",
    "Chunk",
    "Degradation",
    "EmbeddingProvider",
    "EmbeddingUnavailableError",
    "Filters",
    "HybridRetriever",
    "LadderState",
    "OpenRouterEmbeddingProvider",
    "RetrievalResult",
    "RetrievedChunk",
    "Rung",
    "Settings",
    "build_embedding_provider",
    "chunk_directory",
    "corpus_version",
    "load_settings",
    "reciprocal_rank_fusion",
]
