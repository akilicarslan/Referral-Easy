"""Shared fixtures for the RAG tests.

Everything here is offline by default: a stub search client, a stub embedder and a stub
reranker. The degradation ladder can only be tested by making each dependency fail on purpose,
which is impossible if the subject under test builds its own clients. The tests that need a real
cluster are marked ``integration`` and skip when one is not reachable.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from referraleasy_rag.chunking import Chunk, chunk_directory, corpus_version
from referraleasy_rag.config import Settings, load_settings
from referraleasy_rag.contracts import load_hospitals, load_locations, load_providers
from referraleasy_rag.embeddings import EmbeddingProvider
from referraleasy_rag.opensearch_index import IndexUnavailableError
from referraleasy_rag.rerank import RerankClient, RerankHit, RerankUnavailableError
from referraleasy_rag.retriever import HybridRetriever
from referraleasy_rag.seed import SeedCorpus
from referraleasy_rag.text import stem, tokenize

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Settings read from the repository, with the in-process environment taking precedence."""
    return load_settings(repo_root_override=REPO_ROOT)


@pytest.fixture(scope="session")
def chunks(settings: Settings) -> list[Chunk]:
    """The real fixture corpus, chunked."""
    return chunk_directory(
        load_providers(settings.fixtures_dir),
        load_hospitals(settings.fixtures_dir),
        load_locations(settings.fixtures_dir),
    )


@pytest.fixture(scope="session")
def corpus_version_string(chunks: Sequence[Chunk]) -> str:
    return corpus_version(chunks)


@pytest.fixture(scope="session")
def seed(settings: Settings) -> SeedCorpus:
    """The committed seed corpus (package data, no network)."""
    return SeedCorpus.load(settings.seed_path)


class StubSearchClient:
    """A search client that answers both arms from a scripted hit list and records the bodies."""

    def __init__(
        self,
        hits: Sequence[Mapping[str, Any]] = (),
        *,
        error: BaseException | None = None,
        rrf_only: bool = False,
    ) -> None:
        self.bodies: list[Mapping[str, Any]] = []
        self.indexes: list[str] = []
        self._hits = list(hits)
        self._error = error
        self._rrf_only = rrf_only

    def search(self, *, index: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        self.bodies.append(body)
        self.indexes.append(index)
        if self._error is not None:
            raise self._error
        is_vector = "knn" in json.dumps(body.get("query", {}))
        # The lexical arm sees the scripted hits; the vector arm sees them reversed unless
        # `rrf_only` asks for the same order, which is what makes RRF observable in tests.
        ordered = (
            list(reversed(self._hits)) if is_vector and not self._rrf_only else list(self._hits)
        )
        return {
            "hits": {
                "hits": [
                    {"_id": hit["chunk_id"], "_score": 1.0, "_source": dict(hit)} for hit in ordered
                ]
            }
        }

    @property
    def bm25_body(self) -> Mapping[str, Any]:
        return self.bodies[0]

    @property
    def knn_body(self) -> Mapping[str, Any]:
        return self.bodies[1]


def document(
    chunk_id: str,
    text: str,
    *,
    entity_id: str | None = None,
    entity_type: str = "provider",
    **metadata: Any,
) -> dict[str, Any]:
    """A minimal index document, shaped like a real one."""
    base: dict[str, Any] = {
        "chunk_id": chunk_id,
        "entity_id": entity_id or chunk_id,
        "entity_type": entity_type,
        "source_id": f"provider_directory:{entity_id or chunk_id}",
        "knowledge_version": "kv-test-0001",
        "corpus_version": "test-corpus-1",
        "text": text,
    }
    base.update(metadata)
    return base


@pytest.fixture
def two_providers() -> list[dict[str, Any]]:
    """Two chunks that a lexical arm and a vector arm can disagree about."""
    return [
        document(
            "chunk-a",
            "Elena Marquez is a cardiology specialist. To reach Elena Marquez, call +13125550110.",
            display_name="Elena Marquez",
            contact_phone="+13125550110",
            specialties=["cardiology"],
            languages=["en", "es"],
            hospital_ids=["h-lakeview"],
            cities=["Chicago"],
        ),
        document(
            "chunk-b",
            "Jonas Petersen is a neurology specialist. To reach Jonas Petersen, call +13125550120.",
            display_name="Jonas Petersen",
            contact_phone="+13125550120",
            specialties=["neurology"],
            languages=["en"],
            hospital_ids=["h-lakeview"],
            cities=["Chicago"],
        ),
    ]


class StubEmbedder(EmbeddingProvider):
    """Deterministic local embedder, so a test never reaches a provider by accident."""

    def __init__(self, dimensions: int = 768, *, fail: bool = False) -> None:
        self._dimensions = dimensions
        self._fail = fail
        self.document_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    @property
    def model_id(self) -> str:
        return "stub-embedding"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        for token in tokenize(text):
            vector[hash(stem(token)) % self._dimensions] += 1.0
        return vector

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if self._fail:
            from referraleasy_rag.embeddings import EmbeddingUnavailableError

            raise EmbeddingUnavailableError("stub embedder is unavailable")
        self.document_calls.append(list(texts))
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        if self._fail:
            from referraleasy_rag.embeddings import EmbeddingUnavailableError

            raise EmbeddingUnavailableError("stub embedder is unavailable")
        self.query_calls.append(text)
        return self._vector(text)


class StubReranker(RerankClient):
    """A reranker that either reverses the candidate order or refuses to work."""

    def __init__(self, *, available: bool = True, order: Sequence[int] | None = None) -> None:
        self._available = available
        self._order = list(order) if order is not None else None
        self.calls: list[tuple[str, tuple[str, ...], int]] = []

    @property
    def model_id(self) -> str:
        return "stub-reranker"

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankHit]:
        if not self._available:
            raise RerankUnavailableError("stub reranker is unavailable")
        self.calls.append((query, tuple(documents), top_n))
        order = self._order if self._order is not None else list(range(len(documents)))
        return [
            RerankHit(index=index, score=float(len(order) - position))
            for position, index in enumerate(order)
            if 0 <= index < len(documents)
        ]


class FlakySearchClient(StubSearchClient):
    """Answers the first ``succeed_calls`` searches, then loses the cluster.

    The mid-flight outage is the case the ladder has to get right: a model call has already been
    spent by the time the index dies, so the seed rung — which must cost no model call — is not
    available and the pipeline may only fall back to the lexical arm or hand off.
    """

    def __init__(
        self,
        hits: Sequence[Mapping[str, Any]] = (),
        *,
        succeed_calls: int = 1,
        error: BaseException | None = None,
    ) -> None:
        super().__init__(hits)
        self._succeed_calls = succeed_calls
        # Deliberately not named `_error`: that is the parent's always-fail switch, and sharing
        # the name would make this client fail on its very first call.
        self._outage = error or IndexUnavailableError("the cluster stopped answering")

    def search(self, *, index: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        if len(self.bodies) >= self._succeed_calls:
            raise self._outage
        return super().search(index=index, body=body)


@dataclass(frozen=True, slots=True)
class RetrieverHarness:
    """A retriever wired to stubs, plus the stubs, so a test can watch both sides."""

    retriever: HybridRetriever
    client: StubSearchClient
    embedder: StubEmbedder
    reranker: StubReranker


class StubFactory:
    """Builds the stubs, so a test module never has to import them from ``conftest``.

    Test modules sit in a directory without ``__init__.py``, so a sibling ``conftest`` is not an
    importable module from there; handing the stubs out through a fixture keeps the tests free of
    sys.path tricks.
    """

    def embedder(self, *, dimensions: int = 768, fail: bool = False) -> StubEmbedder:
        return StubEmbedder(dimensions, fail=fail)

    def reranker(
        self, *, available: bool = True, order: Sequence[int] | None = None
    ) -> StubReranker:
        return StubReranker(available=available, order=order)

    def client(
        self,
        hits: Sequence[Mapping[str, Any]] = (),
        *,
        error: BaseException | None = None,
        rrf_only: bool = False,
    ) -> StubSearchClient:
        return StubSearchClient(hits, error=error, rrf_only=rrf_only)

    def flaky(
        self,
        hits: Sequence[Mapping[str, Any]] = (),
        *,
        succeed_calls: int = 1,
        error: BaseException | None = None,
    ) -> FlakySearchClient:
        return FlakySearchClient(hits, succeed_calls=succeed_calls, error=error)


@pytest.fixture
def stubs() -> StubFactory:
    return StubFactory()


@pytest.fixture
def build_retriever(settings: Settings, seed: SeedCorpus) -> Callable[..., RetrieverHarness]:
    """Build a fully stubbed retriever, with any collaborator made to fail on demand.

    Every rung of the ladder is reachable from here, which is the point: a fallback that cannot be
    made to fire in a test is a fallback nobody has verified.
    """

    def build(
        *,
        hits: Sequence[Mapping[str, Any]] = (),
        search_error: BaseException | None = None,
        search_client: StubSearchClient | None = None,
        embedder: StubEmbedder | None = None,
        reranker: StubReranker | None = None,
        rerank_enabled: bool = True,
        settings_override: Settings | None = None,
        no_client: bool = False,
    ) -> RetrieverHarness:
        client: StubSearchClient | None = None
        if not no_client:
            client = search_client or StubSearchClient(hits, error=search_error)
        stub_embedder = embedder or StubEmbedder()
        stub_reranker = reranker or StubReranker()
        retriever = HybridRetriever(
            settings_override or settings,
            client=client,
            embedder=stub_embedder,
            reranker=stub_reranker,
            seed=seed,
            rerank_enabled=rerank_enabled,
        )
        return RetrieverHarness(
            retriever=retriever,
            client=client or StubSearchClient(),
            embedder=stub_embedder,
            reranker=stub_reranker,
        )

    return build


@pytest.fixture
def stub_embedder() -> StubEmbedder:
    return StubEmbedder()


@pytest.fixture
def stub_reranker() -> StubReranker:
    return StubReranker()


@pytest.fixture
def blocked_sockets(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make any network attempt fail loudly.

    Used to prove that the seed rung is not merely *observed* to make no call, but is incapable
    of making one: if any code path under test opens a socket, the test errors.
    """

    def guard(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a network call was attempted on a rung that must not make one")

    monkeypatch.setattr(socket, "socket", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket, "getaddrinfo", guard)
    yield
