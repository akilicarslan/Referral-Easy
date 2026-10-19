"""Embedding adapters.

One interface, several implementations, so that swapping the provider cannot change what a
document means:

* :class:`OpenRouterEmbeddingProvider` — the live provider. `POST /api/v1/embeddings` with
  `google/gemini-embedding-001` (verified: 768 dimensions, sane similarity ordering). The
  endpoint ignores `output_dimensionality` and honours `dimensions`; using the wrong one
  silently returns 3072-wide vectors, so the response width is checked on every call.
* :class:`HashingEmbeddingProvider` — deterministic local vectors for `EMBEDDING_BACKEND=offline`.
  Flagged synthetic, never presented as a model's output.
* a future Vertex adapter. ADR-0006 pins `gemini-embedding-001` on Vertex with asymmetric task
  types (`RETRIEVAL_DOCUMENT` for the corpus, `RETRIEVAL_QUERY` for queries). The interface
  keeps documents and queries on separate methods precisely so that adapter can add the
  asymmetry without changing a single call site. The OpenRouter endpoint has no task-type
  parameter, which is a real quality difference, not a detail: it is recorded in ADR-0029.

Both paths go through :func:`referraleasy_rag.text.normalize_text`, the same function, before
anything is embedded. A corpus embedded one way and queried another is the silent quality bug
this module exists to make impossible.
"""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from collections.abc import Sequence

import httpx

from referraleasy_rag.config import Settings, require_api_key
from referraleasy_rag.degradation import CallLedger
from referraleasy_rag.text import PREPROCESSING_VERSION, normalize_text


class EmbeddingError(RuntimeError):
    """Base class for embedding failures."""


class EmbeddingUnavailableError(EmbeddingError):
    """The provider could not be reached or refused the request.

    This is the condition the second rung of the ladder handles: retrieval continues with BM25
    alone and the answer is marked degraded. It is deliberately *not* raised for a bad
    configuration — see :class:`EmbeddingDimensionError`.
    """


class EmbeddingDimensionError(EmbeddingError):
    """The provider returned vectors of the wrong width.

    Not a degradation: a 768-dimension index queried with 3072-dimension vectors does not fail,
    it returns nonsense, and the nonsense is indistinguishable from a good answer. This has to
    stop the pipeline loudly.
    """


class EmbeddingProvider(ABC):
    """What the retriever and the indexer are allowed to assume about an embedder."""

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Model identifier, recorded on every document so a stale index is detectable."""

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Vector width. Must equal the index's ``knn_vector`` dimension."""

    @property
    def synthetic(self) -> bool:
        """True when vectors come from a local stand-in rather than a model."""
        return False

    @property
    def preprocessing_version(self) -> str:
        """Name of the preprocessing applied to both documents and queries."""
        return PREPROCESSING_VERSION

    @abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed corpus text. Never receives a query."""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """Embed a search query. Never receives corpus text."""

    def describe(self) -> str:
        """One-line description for logs and index summaries. Never contains a credential."""
        kind = "synthetic" if self.synthetic else "live"
        return (
            f"{self.model_id} ({self.dimensions}d, {kind}, preprocessing "
            f"{self.preprocessing_version})"
        )


def _prepare(texts: Sequence[str]) -> list[str]:
    """The single preprocessing step, applied to documents and queries alike."""
    prepared = [normalize_text(text) for text in texts]
    return [text if text else " " for text in prepared]


def _snippet(text: str, limit: int = 200) -> str:
    """First line of an upstream error, truncated. Never contains our own credentials."""
    collapsed = " ".join(text.split())
    return collapsed[:limit]


class OpenRouterEmbeddingProvider(EmbeddingProvider):
    """Live embeddings through OpenRouter's OpenAI-compatible endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        model_id: str,
        dimensions: int,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_seconds: float = 60.0,
        batch_size: int = 32,
        ledger: CallLedger | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if dimensions <= 0:
            raise EmbeddingDimensionError(f"dimensions must be positive, got {dimensions}")
        self._api_key = api_key
        self._model_id = model_id
        self._dimensions = dimensions
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._batch_size = max(1, batch_size)
        self._ledger = ledger
        self._client = client

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _post(self, payload: dict[str, object]) -> dict[str, object]:
        url = f"{self._base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = self._client.post(url, json=payload, headers=headers)
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailableError(
                f"embedding request to {self._base_url} failed: {type(exc).__name__}"
            ) from exc
        if response.status_code >= 400:
            raise EmbeddingUnavailableError(
                f"embedding request returned HTTP {response.status_code}: {_snippet(response.text)}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise EmbeddingUnavailableError("embedding response was not JSON") from exc
        if not isinstance(body, dict):
            raise EmbeddingUnavailableError("embedding response was not a JSON object")
        return body

    def _embed(self, texts: Sequence[str], *, kind: str) -> list[list[float]]:
        prepared = _prepare(texts)
        vectors: list[list[float]] = []
        for start in range(0, len(prepared), self._batch_size):
            batch = prepared[start : start + self._batch_size]
            if self._ledger is not None:
                # A model call, not merely a network call: the seed rung must show zero of these,
                # and a ledger that under-counts them would let that invariant pass vacuously.
                self._ledger.record(
                    kind=f"embedding.{kind}",
                    detail=self._model_id,
                    count=len(batch),
                    model=True,
                )
            body = self._post(
                {"model": self._model_id, "input": list(batch), "dimensions": self._dimensions}
            )
            data = body.get("data")
            if not isinstance(data, list) or len(data) != len(batch):
                raise EmbeddingUnavailableError(
                    f"embedding response carried {len(data) if isinstance(data, list) else 0} "
                    f"vectors for {len(batch)} inputs"
                )
            ordered = sorted(
                (entry for entry in data if isinstance(entry, dict)),
                key=lambda entry: (
                    entry.get("index", 0) if isinstance(entry.get("index"), int) else 0
                ),
            )
            for entry in ordered:
                vector = entry.get("embedding")
                if not isinstance(vector, list):
                    raise EmbeddingUnavailableError("embedding entry without an embedding array")
                if len(vector) != self._dimensions:
                    raise EmbeddingDimensionError(
                        f"{self._model_id} returned {len(vector)} dimensions but the index is "
                        f"built for {self._dimensions}; check EMBEDDING_DIMENSIONS and the "
                        f"`dimensions` field on this endpoint"
                    )
                vectors.append([float(value) for value in vector])
        return vectors

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts, kind="document")

    def embed_query(self, text: str) -> list[float]:
        vectors = self._embed([text], kind="query")
        return vectors[0]


class HashingEmbeddingProvider(EmbeddingProvider):
    """Deterministic local vectors for offline mode (``EMBEDDING_BACKEND=offline``).

    Token hashing into a fixed-width, L2-normalized vector. It preserves lexical overlap and
    nothing else — no synonymy, no semantics — and it says so: every document it touches is
    flagged synthetic. It exists so the pipeline can be exercised end to end without a network
    call, not to stand in for a model in production.
    """

    def __init__(
        self, *, dimensions: int = 768, model_id: str = "offline-hashing-embedding-1"
    ) -> None:
        self._dimensions = dimensions
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def synthetic(self) -> bool:
        return True

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        prepared = normalize_text(text)
        tokens = prepared.lower().split()
        for position, token in enumerate(tokens):
            digest = hashlib.sha256(f"{position}:{token}".encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimensions
            weight = 1.0 + (digest[4] / 255.0)
            vector[index] += weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def build_embedding_provider(
    settings: Settings, *, ledger: CallLedger | None = None, client: httpx.Client | None = None
) -> EmbeddingProvider:
    """Select the adapter the environment asks for.

    `EMBEDDING_PROVIDER=vertex` is the ADR-0006 pin, but the Vertex credentials are unresolved
    (ADR-0006 records that status), so a live environment is served by the OpenRouter adapter
    with the same model family and the same width. That substitution is printed by the indexer
    and recorded in ADR-0029 rather than hidden here.
    """
    if settings.embeddings_are_offline:
        return HashingEmbeddingProvider(dimensions=settings.embedding_dimensions)
    api_key = require_api_key(settings, purpose="Embedding through OpenRouter")
    return OpenRouterEmbeddingProvider(
        api_key=api_key,
        model_id=settings.openrouter_embedding_model,
        dimensions=settings.embedding_dimensions,
        base_url=settings.openrouter_base_url,
        timeout_seconds=settings.request_timeout_seconds,
        batch_size=settings.embedding_batch_size,
        ledger=ledger,
        client=client,
    )


__all__ = [
    "EmbeddingDimensionError",
    "EmbeddingError",
    "EmbeddingProvider",
    "EmbeddingUnavailableError",
    "HashingEmbeddingProvider",
    "OpenRouterEmbeddingProvider",
    "build_embedding_provider",
]
