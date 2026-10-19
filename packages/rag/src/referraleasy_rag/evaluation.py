"""Retrieval evaluation: recall@k and MRR, with the reranker on and off.

Ten hand-written queries with known correct answers (see ``data/retrieval_eval.json``). Small,
but it is the difference between "the pipeline returns plausible text" and "the pipeline finds
the right entity, and here is the number".

What is measured, per query:

* **recall@k** — the fraction of the expected entities that appear in the top *k* results;
* **MRR** — 1 / (rank of the first relevant result), 0 when nothing relevant was returned.

Both are computed over **distinct entities**: two chunks of the same provider are one entity
retrieved, not two. A pipeline that returns the same doctor twice has not answered twice.

Run it with::

    uv run python -m referraleasy_rag.evaluation              # hybrid, rerank on and off
    uv run python -m referraleasy_rag.evaluation --seed-only  # the JSON rung, no network

Rerank is *unavailable* in the current workspace (ADR-0029), so the "rerank on" column is
expected to report the ``hybrid_rrf`` rung and to be identical to "rerank off". That equality is
the honest result, not a bug: it is what the degradation ladder is supposed to produce, and the
report says so rather than presenting the fallback as a reranked run.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from referraleasy_rag.config import Settings, eval_dataset_path, load_settings
from referraleasy_rag.degradation import CallLedger, LadderState, Rung
from referraleasy_rag.embeddings import EmbeddingProvider, build_embedding_provider
from referraleasy_rag.opensearch_index import SearchClient
from referraleasy_rag.rerank import NullRerankClient, RerankClient, build_rerank_client
from referraleasy_rag.retriever import Filters, HybridRetriever, RetrievalResult
from referraleasy_rag.seed import SeedCorpus


@dataclass(frozen=True, slots=True)
class EvalQuery:
    """One evaluation query and its known answer."""

    query_id: str
    query: str
    expected_entity_ids: tuple[str, ...]
    filters: Filters = field(default_factory=Filters)
    kind: str = "unspecified"
    note: str = ""


@dataclass(frozen=True, slots=True)
class QueryOutcome:
    """What one query returned, and whether it was right."""

    query_id: str
    query: str
    kind: str
    ranked_entity_ids: tuple[str, ...]
    expected_entity_ids: tuple[str, ...]
    rung: str
    degraded: bool
    first_relevant_rank: int | None
    recall_at_k: Mapping[int, float]

    @property
    def reciprocal_rank(self) -> float:
        if self.first_relevant_rank is None:
            return 0.0
        return 1.0 / self.first_relevant_rank

    @property
    def hit(self) -> bool:
        return self.first_relevant_rank is not None


@dataclass(frozen=True, slots=True)
class EvalReport:
    """Aggregate numbers for one configuration."""

    label: str
    k_values: tuple[int, ...]
    outcomes: tuple[QueryOutcome, ...]
    rungs: Mapping[str, int] = field(default_factory=dict)

    @property
    def recall_at_k(self) -> dict[int, float]:
        report: dict[int, float] = {}
        for k in self.k_values:
            values = [outcome.recall_at_k.get(k, 0.0) for outcome in self.outcomes]
            report[k] = sum(values) / len(values) if values else 0.0
        return report

    @property
    def mrr(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(outcome.reciprocal_rank for outcome in self.outcomes) / len(self.outcomes)

    @property
    def hit_rate(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(1 for outcome in self.outcomes if outcome.hit) / len(self.outcomes)

    @property
    def degraded_outcomes(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.degraded)

    def headline(self) -> str:
        recalls = ", ".join(f"recall@{k}={value:.3f}" for k, value in self.recall_at_k.items())
        return (
            f"{self.label}: MRR={self.mrr:.3f}, {recalls}, "
            f"hit_rate={self.hit_rate:.3f} ({len(self.outcomes)} queries, "
            f"{self.degraded_outcomes} degraded)"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "mrr": round(self.mrr, 4),
            "recall_at_k": {str(k): round(v, 4) for k, v in self.recall_at_k.items()},
            "hit_rate": round(self.hit_rate, 4),
            "queries": len(self.outcomes),
            "degraded_queries": self.degraded_outcomes,
            "rungs": dict(self.rungs),
            "per_query": [
                {
                    "id": outcome.query_id,
                    "query": outcome.query,
                    "kind": outcome.kind,
                    "rung": outcome.rung,
                    "degraded": outcome.degraded,
                    "first_relevant_rank": outcome.first_relevant_rank,
                    "ranked_entity_ids": list(outcome.ranked_entity_ids),
                    "expected_entity_ids": list(outcome.expected_entity_ids),
                    "recall_at_k": {str(k): v for k, v in outcome.recall_at_k.items()},
                }
                for outcome in self.outcomes
            ],
        }


def load_eval_queries(path: Path | None = None) -> list[EvalQuery]:
    """Read the evaluation dataset. A malformed entry is an error, not a skipped query."""
    location = path or eval_dataset_path()
    payload = json.loads(location.read_text(encoding="utf-8"))
    raw_queries = payload.get("queries") if isinstance(payload, dict) else None
    if not isinstance(raw_queries, list):
        raise ValueError(f"evaluation dataset carries no queries array: {location}")
    queries: list[EvalQuery] = []
    for index, raw in enumerate(raw_queries):
        if not isinstance(raw, Mapping):
            raise ValueError(f"dataset entry {index} is not an object")
        expected = raw.get("expected_entity_ids")
        if not isinstance(expected, list) or not expected:
            raise ValueError(f"dataset entry {index} has no expected_entity_ids")
        raw_filters = raw.get("filters")
        filters_payload: Mapping[str, Any] = raw_filters if isinstance(raw_filters, Mapping) else {}
        queries.append(
            EvalQuery(
                query_id=str(raw.get("id", index)),
                query=str(raw.get("query", "")),
                expected_entity_ids=tuple(str(item) for item in expected),
                filters=Filters(
                    specialty=_optional_str(filters_payload.get("specialty")),
                    languages=tuple(_str_list(filters_payload.get("languages"))),
                    hospital_ids=tuple(_str_list(filters_payload.get("hospital_ids"))),
                    cities=tuple(_str_list(filters_payload.get("cities"))),
                    entity_types=tuple(_str_list(filters_payload.get("entity_types"))),
                ),
                kind=str(raw.get("kind", "unspecified")),
                note=str(raw.get("note", "")),
            )
        )
    return queries


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def evaluate(
    retriever: HybridRetriever,
    queries: Sequence[EvalQuery],
    *,
    label: str,
    k_values: Sequence[int] = (1, 3, 5),
) -> EvalReport:
    """Run every query and compute the metrics over distinct entities."""
    outcomes: list[QueryOutcome] = []
    rungs: dict[str, int] = {}
    for query in queries:
        result: RetrievalResult = retriever.search(
            query.query,
            filters=query.filters,
            top_k=max(k_values),
        )
        rungs[str(result.rung)] = rungs.get(str(result.rung), 0) + 1
        outcomes.append(_outcome(query, result, k_values=tuple(k_values)))
    return EvalReport(label=label, k_values=tuple(k_values), outcomes=tuple(outcomes), rungs=rungs)


def _outcome(
    query: EvalQuery, result: RetrievalResult, *, k_values: tuple[int, ...]
) -> QueryOutcome:
    ranked: list[str] = []
    for chunk in result.results:
        if chunk.entity_id not in ranked:
            ranked.append(chunk.entity_id)
    expected = set(query.expected_entity_ids)
    first_rank: int | None = None
    for position, entity_id in enumerate(ranked):
        if entity_id in expected:
            first_rank = position + 1
            break
    recall = {
        k: (len(set(ranked[:k]) & expected) / len(expected)) if expected else 0.0 for k in k_values
    }
    return QueryOutcome(
        query_id=query.query_id,
        query=query.query,
        kind=query.kind,
        ranked_entity_ids=tuple(ranked),
        expected_entity_ids=query.expected_entity_ids,
        rung=str(result.rung),
        degraded=result.degraded,
        first_relevant_rank=first_rank,
        recall_at_k=recall,
    )


def evaluate_seed_only(
    seed: SeedCorpus,
    queries: Sequence[EvalQuery],
    *,
    top_k: int = 5,
) -> EvalReport:
    """Evaluate the JSON rung on its own: no index, no embeddings, no network."""
    outcomes: list[QueryOutcome] = []
    for query in queries:
        seed_ledger = CallLedger()
        ladder = LadderState(ledger=seed_ledger)
        ladder.degrade(Rung.SEED_FALLBACK, "evaluation of the seed rung")
        hits = seed.search(query.query, filters=query.filters.to_clause(), top_k=top_k)
        ladder.attach_seed_ledger(seed_ledger)
        ranked: list[str] = []
        for hit in hits:
            if hit.document.entity_id not in ranked:
                ranked.append(hit.document.entity_id)
        expected = set(query.expected_entity_ids)
        first_rank = next(
            (position + 1 for position, item in enumerate(ranked) if item in expected), None
        )
        outcomes.append(
            QueryOutcome(
                query_id=query.query_id,
                query=query.query,
                kind=query.kind,
                ranked_entity_ids=tuple(ranked),
                expected_entity_ids=query.expected_entity_ids,
                rung=str(Rung.SEED_FALLBACK),
                degraded=True,
                first_relevant_rank=first_rank,
                recall_at_k={
                    k: (len(set(ranked[:k]) & expected) / len(expected)) if expected else 0.0
                    for k in (1, 3, 5)
                },
            )
        )
    return EvalReport(
        label="seed-only (no index, no model call)",
        k_values=(1, 3, 5),
        outcomes=tuple(outcomes),
        rungs={str(Rung.SEED_FALLBACK): len(outcomes)},
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: ``uv run python -m referraleasy_rag.evaluation``."""
    parser = argparse.ArgumentParser(description="Retrieval evaluation (recall@k, MRR)")
    parser.add_argument("--seed-only", action="store_true", help="evaluate the JSON rung only")
    parser.add_argument("--dataset", type=Path, default=None, help="alternative dataset path")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("--index", default=None, help="index name (default: from the environment)")
    args = parser.parse_args(argv)

    settings: Settings = load_settings()
    queries = load_eval_queries(args.dataset)
    reports: list[EvalReport] = []

    if args.seed_only:
        reports.append(evaluate_seed_only(SeedCorpus.load(settings.seed_path), queries))
    else:
        reports.extend(_hybrid_reports(settings, queries, index_name=args.index))

    if args.json:
        print(json.dumps([report.to_dict() for report in reports], indent=2, sort_keys=True))
    else:
        for report in reports:
            print(report.headline())
            for outcome in report.outcomes:
                rank = outcome.first_relevant_rank if outcome.first_relevant_rank else "-"
                print(
                    f"  {outcome.query_id} [{outcome.kind:<10}] rank={rank!s:<3} "
                    f"rung={outcome.rung:<14} {outcome.query[:52]}"
                )
            print()
    return 0


def _hybrid_reports(
    settings: Settings, queries: Sequence[EvalQuery], *, index_name: str | None
) -> list[EvalReport]:
    """Hybrid runs with the reranker on and off, against the live index."""
    from referraleasy_rag.client import build_client, probe_opensearch

    probe = probe_opensearch(settings)
    if not probe.reachable:
        print(
            "OpenSearch is unreachable on every candidate URL; run with --seed-only for the "
            "JSON rung.",
            file=sys.stderr,
        )
        return []
    client: SearchClient = build_client(settings, url=probe.url)
    embedder: EmbeddingProvider = build_embedding_provider(settings)
    seed = SeedCorpus.load(settings.seed_path)
    reports: list[EvalReport] = []
    for rerank_enabled, label in ((True, "hybrid, rerank on"), (False, "hybrid, rerank off")):
        reranker: RerankClient = (
            build_rerank_client(settings) if rerank_enabled else NullRerankClient()
        )
        retriever = HybridRetriever(
            settings,
            client=client,
            embedder=embedder,
            reranker=reranker,
            seed=seed,
            index_name=index_name,
            rerank_enabled=rerank_enabled,
        )
        reports.append(evaluate(retriever, queries, label=label))
    if reports[0].mrr == reports[1].mrr:
        print(
            "note: rerank-on and rerank-off scored identically, which is what the ladder "
            "produces while the reranker is unavailable (see ADR-0029).\n",
            file=sys.stderr,
        )
    return reports


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
