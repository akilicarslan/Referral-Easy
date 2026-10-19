"""The degradation ladder: every rung reachable, none reachable by climbing, all of them visible.

Two kinds of test live here. The state machine's own rules are checked directly (a descent table,
a terminal rung, invariants about who may take the seed rung). Then each rung is produced through
the real retriever with one collaborator made to fail — because a ladder that is only *described*
as degrading is a ladder nobody has seen work.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from referraleasy_rag.degradation import (
    ALLOWED_TRANSITIONS,
    RUNG_DESCRIPTIONS,
    RUNG_ORDER,
    CallLedger,
    LadderState,
    LadderViolation,
    Rung,
)
from referraleasy_rag.retriever import Filters

QUERY = "Elena Marquez cardiologist phone number"


# ------------------------------------------------------------------ the state machine


def test_the_descent_table_only_ever_goes_down() -> None:
    for position, rung in enumerate(RUNG_ORDER):
        for target in ALLOWED_TRANSITIONS[rung]:
            assert RUNG_ORDER.index(target) > position, f"{rung} may not climb to {target}"


def test_the_terminal_rung_is_terminal() -> None:
    assert ALLOWED_TRANSITIONS[Rung.HUMAN_HANDOFF] == frozenset()
    ladder = LadderState()
    ladder.hand_off("no_source_found")
    with pytest.raises(LadderViolation, match="cannot move from"):
        ladder.degrade(Rung.HYBRID_RERANK, "wishful_thinking")


def test_climbing_is_a_violation_not_a_recovery() -> None:
    ladder = LadderState()
    ladder.degrade(Rung.BM25_ONLY, "embeddings_unavailable")
    with pytest.raises(LadderViolation, match="cannot move from"):
        ladder.degrade(Rung.HYBRID_RERANK, "recovered")
    with pytest.raises(LadderViolation, match="cannot move from"):
        ladder.degrade(Rung.HYBRID_RRF, "recovered")


def test_a_shallower_rung_can_be_skipped() -> None:
    """A dead index goes straight to the seed; it does not have to pass through BM25."""
    ladder = LadderState()
    ladder.degrade(Rung.SEED_FALLBACK, "opensearch_unavailable")
    assert ladder.rung is Rung.SEED_FALLBACK
    assert ladder.visited == [Rung.HYBRID_RERANK, Rung.SEED_FALLBACK]


def test_the_seed_rung_may_not_be_taken_after_a_model_call() -> None:
    """Its whole value is answering with nothing available; a spent call is something available."""
    ledger = CallLedger()
    ledger.record(kind="embedding.query", model=True)
    ladder = LadderState(ledger=ledger)
    with pytest.raises(LadderViolation, match="without a model call"):
        ladder.degrade(Rung.SEED_FALLBACK, "opensearch_unavailable")


def test_a_seed_ledger_that_carried_a_model_call_is_rejected() -> None:
    ledger = CallLedger()
    ledger.record(kind="embedding.query", model=True)
    ladder = LadderState()
    with pytest.raises(LadderViolation, match="made a model call"):
        ladder.attach_seed_ledger(ledger)


def test_seed_is_silent_only_when_the_rung_was_taken_and_called_nothing() -> None:
    ladder = LadderState()
    assert ladder.seed_is_silent is False, "no seed phase happened, so nothing is proven"
    ladder.degrade(Rung.SEED_FALLBACK, "opensearch_unavailable")
    assert ladder.seed_is_silent is False
    ladder.attach_seed_ledger(CallLedger())
    assert ladder.seed_is_silent is True


def test_only_the_top_rung_is_not_degraded() -> None:
    for rung in RUNG_ORDER:
        ladder = LadderState()
        if rung is not Rung.HYBRID_RERANK:
            ladder.degrade(rung, "test")
        assert ladder.degraded is (rung is not Rung.HYBRID_RERANK)
        assert rung in RUNG_DESCRIPTIONS
        assert ladder.description


def test_degrading_to_the_rung_you_are_already_on_is_a_no_op() -> None:
    ladder = LadderState()
    ladder.degrade(Rung.HYBRID_RERANK, "nothing happened")
    assert ladder.degradations == []


def test_reasons_are_recorded_in_order_and_kept() -> None:
    ladder = LadderState()
    ladder.degrade(Rung.HYBRID_RRF, "reranker_unavailable", "HTTP 404")
    ladder.degrade(Rung.BM25_ONLY, "index_lost_mid_retrieval", "ConnectError")
    assert ladder.reasons == ["reranker_unavailable", "index_lost_mid_retrieval"]
    assert ladder.summary_line().startswith("rung=bm25_only (DEGRADED)")
    assert ladder.to_dict()["calls"] == {"network_calls": 0, "model_calls": 0, "kinds": []}


def test_a_ledger_counts_model_calls_per_text_not_per_request() -> None:
    """Batching must not be a way to satisfy "no LLM call" while making one."""
    ledger = CallLedger()
    ledger.record(kind="embedding.document", count=32, model=True)
    ledger.record(kind="opensearch.bm25", network=True)
    assert ledger.model_calls == 32
    assert ledger.network_calls == 33
    assert not ledger.is_empty


def test_the_request_count_is_requests_not_units_sent() -> None:
    """A batched embed is 32 model calls in 1 request; the operator line reports both."""
    ledger = CallLedger()
    ledger.record(kind="embedding.document", count=32, model=True)
    ledger.record(kind="embedding.document", count=1, model=True)
    ledger.record(kind="opensearch.bm25")
    ledger.record(kind="opensearch.knn")
    assert ledger.model_calls == 33, "texts embedded"
    assert ledger.network_calls == 35, "units sent: 32 + 1 embedded texts, 2 searches"
    assert ledger.requests == 4, "round trips: two embedding batches and two searches"

    silent = CallLedger()
    assert silent.requests == 0
    silent.record(kind="seed.search", count=5, network=False)
    assert silent.requests == 0, "a local search is not an outbound request"
    assert silent.network_calls == 0


# ------------------------------------------------------------------ each rung, for real


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("full_pipeline", "hybrid_rerank"),
        ("reranker_unavailable", "hybrid_rrf"),
        ("rerank_disabled", "hybrid_rrf"),
        ("embeddings_unavailable", "bm25_only"),
        ("index_unavailable", "seed_fallback"),
        ("nothing_found", "human_handoff"),
    ],
)
def test_every_rung_is_reachable_and_visibly_flagged(
    case: str,
    expected: str,
    build_retriever: Callable[..., Any],
    stubs: Any,
    two_providers: list[dict[str, Any]],
) -> None:
    harness = _harness_for(case, build_retriever, stubs, two_providers)
    result = harness.retriever.search(QUERY if case != "nothing_found" else "zzzq nothing")

    assert str(result.rung) == expected
    assert result.degraded is (expected != "hybrid_rerank")
    assert result.to_dict()["ladder"]["rung"] == expected
    if expected == "hybrid_rerank":
        assert "DEGRADED" not in result.ladder.summary_line()
    else:
        assert "DEGRADED" in result.ladder.summary_line()


def test_the_full_pipeline_actually_reranks(
    build_retriever: Callable[..., Any], stubs: Any, two_providers: list[dict[str, Any]]
) -> None:
    """The reranker's order is the returned order, or "reranking" means nothing."""
    harness = _harness_for("full_pipeline", build_retriever, stubs, two_providers)
    result = harness.retriever.search(QUERY)

    assert harness.reranker.calls, "the reranker must have been called"
    assert [chunk.chunk_id for chunk in result.results] == ["chunk-b", "chunk-a"]
    assert result.results[0].rerank_score is not None
    assert result.results[0].arms == ("bm25", "vector"), "a reranked hit keeps its provenance"


def test_without_a_reranker_the_rrf_order_is_kept(
    build_retriever: Callable[..., Any], stubs: Any, two_providers: list[dict[str, Any]]
) -> None:
    """RRF, not the raw arm order: the two arms disagree, and agreement is what RRF rewards."""
    harness = _harness_for("reranker_unavailable", build_retriever, stubs, two_providers)
    result = harness.retriever.search(QUERY)

    assert [chunk.chunk_id for chunk in result.results] == ["chunk-a", "chunk-b"]
    assert all(chunk.rerank_score is None for chunk in result.results)
    assert all(chunk.rrf_score is not None for chunk in result.results)
    assert result.ladder.reasons == ["reranker_unavailable"]
    assert any("RRF ordering kept" in note for note in result.notes)


def test_a_disabled_reranker_is_a_degradation_not_a_silent_setting(
    build_retriever: Callable[..., Any], stubs: Any, two_providers: list[dict[str, Any]]
) -> None:
    harness = _harness_for("rerank_disabled", build_retriever, stubs, two_providers)
    result = harness.retriever.search(QUERY)
    assert result.ladder.reasons == ["rerank_disabled"]
    assert result.degraded is True


def test_without_embeddings_the_lexical_arm_still_answers(
    build_retriever: Callable[..., Any], stubs: Any, two_providers: list[dict[str, Any]]
) -> None:
    harness = _harness_for("embeddings_unavailable", build_retriever, stubs, two_providers)
    result = harness.retriever.search(QUERY)

    assert result.ladder.reasons == ["embeddings_unavailable"]
    assert [chunk.chunk_id for chunk in result.results] == ["chunk-a", "chunk-b"]
    assert [chunk.arms for chunk in result.results] == [("bm25",), ("bm25",)]
    assert harness.reranker.calls == [], "nothing to rerank when only one arm ran"


def test_a_dead_index_falls_through_to_the_seed_corpus(
    build_retriever: Callable[..., Any], stubs: Any, two_providers: list[dict[str, Any]]
) -> None:
    harness = _harness_for("index_unavailable", build_retriever, stubs, two_providers)
    result = harness.retriever.search(QUERY)

    assert result.ladder.reasons == ["opensearch_unavailable"]
    assert result.results, "the seed corpus holds Elena Marquez"
    assert all(chunk.arms == ("seed",) for chunk in result.results)
    assert result.ladder.ledger.model_calls == 0
    assert result.ladder.ledger.network_calls == 0
    assert result.index_knowledge_version is None
    assert result.seed_knowledge_version


def test_the_seed_rung_is_proven_silent_by_the_ledger(
    build_retriever: Callable[..., Any], blocked_sockets: None
) -> None:
    """Not merely observed to make no call: incapable of making one, with sockets blocked."""
    harness = build_retriever(no_client=True)
    result = harness.retriever.search(QUERY)

    assert str(result.rung) == "seed_fallback"
    assert result.ladder.seed_is_silent is True
    assert result.to_dict()["ladder"]["seed_made_no_call"] is True
    assert result.results


def test_finding_nothing_hands_off_instead_of_inventing(
    build_retriever: Callable[..., Any], stubs: Any, two_providers: list[dict[str, Any]]
) -> None:
    harness = _harness_for("nothing_found", build_retriever, stubs, two_providers)
    result = harness.retriever.search("zzzq nothing")

    assert result.requires_human is True
    assert result.results == ()
    assert result.ladder.reasons == ["no_source_found"]
    assert result.citations == []


def test_no_index_and_no_seed_match_still_hands_off(
    build_retriever: Callable[..., Any], blocked_sockets: None
) -> None:
    """The last rung is not a licence to answer: an empty seed search hands off too."""
    harness = build_retriever(no_client=True)
    result = harness.retriever.search("zzqqxx vvwwyy")

    assert result.requires_human is True
    assert result.results == ()
    assert result.ladder.reasons == ["opensearch_unavailable", "no_source_found_in_seed"]


def test_an_empty_query_hands_off_without_touching_the_index(
    build_retriever: Callable[..., Any], blocked_sockets: None
) -> None:
    harness = build_retriever()
    result = harness.retriever.search("   ")

    assert result.requires_human is True
    assert result.ladder.reasons == ["empty_query"]
    assert harness.client.bodies == []


def test_an_index_that_dies_after_a_model_call_cannot_use_the_seed_rung(
    build_retriever: Callable[..., Any], stubs: Any, two_providers: list[dict[str, Any]]
) -> None:
    """The one transition the ladder forbids: a spent embed followed by a 'free' fallback."""
    harness = build_retriever(search_client=stubs.flaky(two_providers, succeed_calls=1))
    result = harness.retriever.search(QUERY)

    assert str(result.rung) == "bm25_only"
    assert result.ladder.reasons == ["index_lost_mid_retrieval"]
    assert Rung.SEED_FALLBACK not in result.ladder.visited
    assert result.ladder.ledger.model_calls == 1, "the query embedding was spent before the outage"
    assert [chunk.arms for chunk in result.results] == [("bm25",), ("bm25",)]


def test_an_index_that_dies_after_a_model_call_hands_off_when_the_lexical_arm_is_empty(
    build_retriever: Callable[..., Any], stubs: Any, blocked_sockets: None
) -> None:
    harness = build_retriever(search_client=stubs.flaky([], succeed_calls=1))
    result = harness.retriever.search(QUERY)

    assert result.requires_human is True
    assert result.ladder.reasons == ["index_lost_mid_retrieval"]
    assert Rung.SEED_FALLBACK not in result.ladder.visited


def test_filters_survive_the_descent_to_the_seed_rung(build_retriever: Callable[..., Any]) -> None:
    """A degraded answer must not be a broader one than the user asked for."""
    harness = build_retriever(no_client=True)
    result = harness.retriever.search("Elena Marquez", filters=Filters(languages=("pl",)))
    assert result.requires_human is True
    assert result.ladder.reasons == ["opensearch_unavailable", "no_source_found_in_seed"]
    assert result.filters_applied == {"languages": ["pl"]}


def _harness_for(
    case: str, build_retriever: Callable[..., Any], stubs: Any, two_providers: list[dict[str, Any]]
) -> Any:
    """One collaborator broken per case, and nothing else."""
    if case == "full_pipeline":
        return build_retriever(hits=two_providers, reranker=stubs.reranker(order=[1, 0]))
    if case == "reranker_unavailable":
        return build_retriever(hits=two_providers, reranker=stubs.reranker(available=False))
    if case == "rerank_disabled":
        return build_retriever(hits=two_providers, rerank_enabled=False)
    if case == "embeddings_unavailable":
        return build_retriever(hits=two_providers, embedder=stubs.embedder(fail=True))
    if case == "index_unavailable":
        return build_retriever(no_client=True)
    if case == "nothing_found":
        return build_retriever(hits=[])
    raise AssertionError(f"unknown case {case!r}")
