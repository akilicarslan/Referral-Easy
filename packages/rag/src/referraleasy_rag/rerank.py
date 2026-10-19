"""Rerank client.

The wire shape implemented here is the one OpenRouter documents for
``POST /api/v1/rerank``::

    {"model": "cohere/rerank-v3.5", "query": "...", "documents": ["...", "..."], "top_n": 5}
    -> {"results": [{"index": 3, "relevance_score": 0.91, "document": {...}}, ...]}

**This endpoint is not usable in the current workspace, and that is recorded, not worked
around.** A live call returns HTTP 404 with

    "No endpoints found that satisfy the workspace's ZDR policy (guardrail violation)"

because the workspace excludes every rerank endpoint that does not meet its zero-data-retention
policy. A per-request override cannot loosen a workspace guardrail, so there is no request shape
that would succeed. (ADR-0007 pinned the endpoint and the model id from the vendor's own spec;
ADR-0029 records that the workspace refuses it.)

The client therefore stays faithful to the documented shape and treats the refusal as an
ordinary :class:`RerankUnavailableError`. That is not a workaround for a missing feature: the
pipeline is *required* to degrade correctly when a reranker is unavailable, and a provider that
is genuinely unavailable is the honest way to exercise that rung. When a reranker becomes
reachable, the same client starts returning orderings and the rung stops being taken — no code
change required.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from referraleasy_rag.config import Settings, require_api_key
from referraleasy_rag.degradation import CallLedger

#: Kept verbatim so a reader can see the shape the implementation follows.
RERANK_PATH = "/rerank"


class RerankUnavailableError(RuntimeError):
    """The reranker could not be used. Retrieval keeps its RRF ordering and reports degraded."""


@dataclass(frozen=True, slots=True)
class RerankHit:
    """One reranked document, keyed back to its position in the input list."""

    index: int
    score: float


class RerankClient(ABC):
    """A reranker. Implementations either return a full ordering or raise."""

    @property
    @abstractmethod
    def model_id(self) -> str: ...

    @abstractmethod
    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankHit]:
        """Return at most ``top_n`` hits, best first, indices into ``documents``."""


class NullRerankClient(RerankClient):
    """A reranker that is configured off. Raises, so the caller takes the documented rung."""

    def __init__(self, reason: str = "rerank disabled by configuration") -> None:
        self._reason = reason

    @property
    def model_id(self) -> str:
        return "none"

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankHit]:
        raise RerankUnavailableError(self._reason)


class OpenRouterRerankClient(RerankClient):
    """OpenRouter ``/rerank`` client, faithful to the documented request and response shapes."""

    def __init__(
        self,
        *,
        api_key: str,
        model_id: str,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_seconds: float = 60.0,
        ledger: CallLedger | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._model_id = model_id
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._ledger = ledger
        self._client = client

    @property
    def model_id(self) -> str:
        return self._model_id

    def rerank(self, query: str, documents: Sequence[str], top_n: int) -> list[RerankHit]:
        if not documents:
            return []
        payload = {
            "model": self._model_id,
            "query": query,
            "documents": list(documents),
            "top_n": top_n,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self._base_url}{RERANK_PATH}"
        if self._ledger is not None:
            self._ledger.record(kind="rerank", detail=self._model_id, model=True)
        try:
            if self._client is not None:
                response = self._client.post(url, json=payload, headers=headers)
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise RerankUnavailableError(
                f"rerank request failed: {type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise RerankUnavailableError(
                f"rerank returned HTTP {response.status_code}: {_snippet(response.text)}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise RerankUnavailableError("rerank response was not JSON") from exc
        if not isinstance(body, dict):
            raise RerankUnavailableError("rerank response was not a JSON object")
        results = body.get("results")
        if not isinstance(results, list):
            raise RerankUnavailableError("rerank response carried no results array")
        hits: list[RerankHit] = []
        for entry in results:
            if not isinstance(entry, dict):
                continue
            index = entry.get("index")
            score = entry.get("relevance_score", entry.get("score"))
            if isinstance(index, bool) or not isinstance(index, int):
                raise RerankUnavailableError("rerank result without an integer index")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise RerankUnavailableError("rerank result without a numeric score")
            if 0 <= index < len(documents):
                hits.append(RerankHit(index=index, score=float(score)))
        if not hits:
            raise RerankUnavailableError("rerank returned no usable results")
        hits.sort(key=lambda hit: (-hit.score, hit.index))
        return hits[:top_n]


def build_rerank_client(settings: Settings, *, ledger: CallLedger | None = None) -> RerankClient:
    """Select the reranker the environment asks for, or a raising stand-in when it is off."""
    if settings.rerank_backend.strip().lower() in {"off", "none", "disabled"}:
        return NullRerankClient(f"RERANK_BACKEND={settings.rerank_backend}")
    if not settings.openrouter_api_key:
        # No key means the reranker cannot be reached. Not an error: an unavailable reranker is
        # a defined rung, so this returns a client whose failure mode is the documented one.
        return NullRerankClient("OPENROUTER_API_KEY is not set")
    api_key = require_api_key(settings, purpose="Rerank through OpenRouter")
    return OpenRouterRerankClient(
        api_key=api_key,
        model_id=settings.rerank_model_id,
        base_url=settings.openrouter_base_url,
        timeout_seconds=settings.request_timeout_seconds,
        ledger=ledger,
    )


def _snippet(text: str, limit: int = 200) -> str:
    """First line of an upstream error, truncated. Kept for the ADR-0007 guardrail message."""
    return " ".join(text.split())[:limit]


__all__ = [
    "RERANK_PATH",
    "NullRerankClient",
    "OpenRouterRerankClient",
    "RerankClient",
    "RerankHit",
    "RerankUnavailableError",
    "build_rerank_client",
]
