# 7 · Hybrid RAG and the degradation ladder

Spec: §10 · ADR-0006, ADR-0007, ADR-0008, ADR-0029 · Code: `packages/rag/src/referraleasy_rag/`,
`infra/opensearch/index-templates/referraleasy-directory-vector.json`, `make index`

[← back to the index](README.md)

## Wiring status

```mermaid
flowchart LR
    PM["Provider Matching<br/>search_directory tool"]:::built -- "today" --> DS["mock-api<br/>/directory/search<br/>degradation = static_seed"]:::built
    PM -. "intended" .-> RAG["packages/rag<br/>HybridRetriever"]:::unwired
    RAG --> OS[("OpenSearch<br/>referraleasy-directory-vector")]:::built
    Idx["make index<br/>chunk → embed → bulk"]:::built --> OS

    classDef built fill:#e8f4ea,stroke:#2e7d32,color:#1b1b1b
    classDef unwired fill:#fff8e1,stroke:#f9a825,color:#1b1b1b,stroke-dasharray:4 3
```

The package is complete and tested, including 13 OpenSearch-backed integration tests. No service
imports it yet. Every answer from the tool that *is* wired says `static_seed`, so a filtered answer
is never presented as an exhaustive one.

## Indexing

```mermaid
flowchart LR
    Fx["fixtures/directory<br/>hospitals · providers · visit types<br/>contacts · admin rule text"] --> Ch["chunking.py<br/>atoms never split<br/>name + phone in one atom<br/>entity name in every header"]
    Ch --> Meta["every chunk: entity_id · source_id<br/>knowledge_version · updated_at"]
    Ch -. "slots are NOT chunked —<br/>retrieval may not assert availability" .-> X["(excluded)"]
    Meta --> Pre["text.py — same preprocessing<br/>for documents and queries"]
    Pre --> Emb["embeddings.py<br/>Vertex gemini-embedding-001, 768-d<br/>(offline: deterministic adapter)"]
    Emb --> Bulk["indexing.py → bulk write<br/>refuses to report success on 0 docs"]
    Bulk --> OS[("directory-vector index<br/>BM25 text + knn_vector")]
```

## Query pipeline

```mermaid
flowchart TD
    Q["query + Filters<br/>language · specialty · hospital"] --> F["Filters.to_clause() — once"]
    F --> BM["BM25 arm<br/>top 20, dedup"]
    F --> KN["kNN arm<br/>same model, top 20, dedup"]
    Q --> QE["query embedding"] --> KN
    BM --> RRF["rrf.py<br/>dedup + reciprocal rank fusion"]
    KN --> RRF
    RRF --> RR["rerank top 10<br/>OpenRouter /api/v1/rerank<br/>cohere/rerank-v3.5"]
    RR --> Top["sourced top 3–5"]
    Top --> Geo["geo.py — distance from coordinates<br/>(never from a score; None if no coords)"]
    Geo --> Auth["authority.py<br/>structured coverage record beats prose<br/>conflicts returned, not hidden<br/>availability/booking claims stripped"]
    Auth --> Res["RetrievalResult<br/>rung · degraded · degradations[] · sources"]
```

## The degradation ladder

The ladder only goes down, never back up. Every step down is recorded with a reason, and any rung
other than `hybrid_rerank` sets `degraded = true`.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> hybrid_rerank
    hybrid_rerank --> hybrid_rrf: reranker down
    hybrid_rerank --> bm25_only: embedding down
    hybrid_rrf --> bm25_only: embedding down
    hybrid_rerank --> seed_fallback: OpenSearch down
    hybrid_rrf --> seed_fallback: OpenSearch down
    bm25_only --> seed_fallback: OpenSearch down
    hybrid_rerank --> human_handoff: no source
    hybrid_rrf --> human_handoff: no source
    bm25_only --> human_handoff: no source
    seed_fallback --> human_handoff: no source
    human_handoff --> [*]

    note right of seed_fallback
        versioned seed JSON, deterministic filters,
        no network call, no LLM — the rung that
        must work with everything else down
    end note
    note right of human_handoff
        terminal: reached by finding nothing,
        never by inventing something
    end note
```

## Retrieval evaluation

`evaluation.py` measures recall@k and MRR with the reranker on and off, so the reranker's
contribution is a number rather than an assumption.
