"""The embedding adapter: one preprocessing, one width, and no silent substitution.

The properties that matter are not "it returns vectors". They are: the same text is prepared the
same way on both sides of the pipeline (a corpus embedded one way and queried another is a silent
quality bug that no test would otherwise catch); a vector of the wrong width is an error rather
than a degradation, because a 768-wide index queried with 3072-wide vectors does not fail — it
returns nonsense indistinguishable from a good answer; and the provider is reachable only through
an interface, so the offline stand-in is visibly a stand-in.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
import pytest

from referraleasy_rag.config import ConfigError, Settings
from referraleasy_rag.degradation import CallLedger
from referraleasy_rag.embeddings import (
    EmbeddingDimensionError,
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingUnavailableError,
    HashingEmbeddingProvider,
    OpenRouterEmbeddingProvider,
    build_embedding_provider,
)
from referraleasy_rag.text import PREPROCESSING_VERSION, normalize_text

API_KEY = "test-key-value"


class _Response:
    """The slice of an httpx response the adapter reads."""

    def __init__(
        self,
        payload: object,
        *,
        status_code: int = 200,
        text: str | None = None,
        json_error: bool = False,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload, default=str)
        self._json_error = json_error

    def json(self) -> object:
        if self._json_error:
            raise ValueError("not json")
        return self._payload


class _FakeHTTP:
    """Records what was sent and answers with whatever the test scripted."""

    def __init__(
        self,
        *,
        dimensions: int = 768,
        status_code: int = 200,
        error: BaseException | None = None,
        reverse: bool = False,
        json_error: bool = False,
        body: object | None = None,
    ) -> None:
        self.requests: list[dict[str, Any]] = []
        self._dimensions = dimensions
        self._status_code = status_code
        self._error = error
        self._reverse = reverse
        self._json_error = json_error
        self._body = body

    def post(
        self,
        url: str,
        *,
        json: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> _Response:
        payload = dict(json or {})
        self.requests.append({"url": url, "json": payload, "headers": dict(headers or {})})
        if self._error is not None:
            raise self._error
        if self._body is not None:
            return _Response(self._body, status_code=self._status_code, json_error=self._json_error)
        inputs = payload.get("input", [])
        entries = [
            {"index": index, "embedding": [0.1 * (index + 1)] * self._dimensions}
            for index in range(len(inputs) if isinstance(inputs, list) else 0)
        ]
        if self._reverse:
            entries.reverse()
        return _Response(
            {"data": entries}, status_code=self._status_code, json_error=self._json_error
        )


def _provider(
    client: _FakeHTTP,
    *,
    dimensions: int = 768,
    model_id: str = "google/gemini-embedding-001",
    batch_size: int = 32,
    ledger: CallLedger | None = None,
) -> OpenRouterEmbeddingProvider:
    return OpenRouterEmbeddingProvider(
        api_key=API_KEY,
        model_id=model_id,
        dimensions=dimensions,
        batch_size=batch_size,
        ledger=ledger,
        client=client,  # type: ignore[arg-type]
    )


def test_the_request_carries_the_model_the_input_and_the_width() -> None:
    client = _FakeHTTP()
    provider = _provider(client)
    provider.embed_documents(["Elena Marquez is a cardiologist."])

    request = client.requests[0]
    assert request["url"].endswith("/embeddings")
    assert request["json"]["model"] == "google/gemini-embedding-001"
    assert request["json"]["dimensions"] == 768
    assert request["json"]["input"] == ["Elena Marquez is a cardiologist."]
    # The endpoint ignores `output_dimensionality`; sending it would silently return 3072-wide
    # vectors, so the payload is asserted to carry `dimensions` and nothing else.
    assert "output_dimensionality" not in request["json"]


def test_the_authorization_header_is_sent_and_never_echoed() -> None:
    client = _FakeHTTP()
    provider = _provider(client)
    provider.embed_query("anything")

    assert client.requests[0]["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert API_KEY not in provider.describe()
    assert "openrouter" not in provider.describe().lower(), (
        "describe() names the model, nothing else"
    )


def test_vectors_come_back_in_the_requested_order() -> None:
    """The endpoint is allowed to answer out of order; the adapter is not allowed to pass that on."""
    client = _FakeHTTP(dimensions=1, reverse=True)
    provider = _provider(client, dimensions=1)
    vectors = provider.embed_documents(["first", "second", "third"])
    assert vectors == [[pytest.approx(0.1)], [pytest.approx(0.2)], [pytest.approx(0.3)]]


def test_the_reported_width_is_the_index_width() -> None:
    provider = _provider(_FakeHTTP(), dimensions=768)
    assert provider.dimensions == 768
    assert provider.model_id == "google/gemini-embedding-001"
    assert provider.preprocessing_version == PREPROCESSING_VERSION
    assert provider.synthetic is False


def test_a_vector_of_the_wrong_width_stops_the_pipeline() -> None:
    """Not a degradation: a wrong-width vector produces plausible nonsense, not an error."""
    provider = _provider(_FakeHTTP(dimensions=3072), dimensions=768)
    with pytest.raises(EmbeddingDimensionError, match="3072 dimensions"):
        provider.embed_documents(["Elena Marquez"])


def test_a_width_error_is_not_a_degradation() -> None:
    """The ladder must not swallow it: this is a configuration fault, not an outage."""
    assert issubclass(EmbeddingDimensionError, EmbeddingError)
    assert not issubclass(EmbeddingDimensionError, EmbeddingUnavailableError)


def test_a_http_error_is_an_outage() -> None:
    provider = _provider(_FakeHTTP(status_code=502, body={"error": "bad gateway"}))
    with pytest.raises(EmbeddingUnavailableError, match="HTTP 502"):
        provider.embed_query("Elena Marquez")


def test_a_transport_error_is_an_outage() -> None:
    provider = _provider(_FakeHTTP(error=httpx.ConnectError("connection refused")))
    with pytest.raises(EmbeddingUnavailableError, match="ConnectError"):
        provider.embed_query("Elena Marquez")


def test_a_response_that_is_not_json_is_an_outage() -> None:
    provider = _provider(_FakeHTTP(body={"data": []}, json_error=True))
    with pytest.raises(EmbeddingUnavailableError, match="not JSON"):
        provider.embed_query("Elena Marquez")


def test_a_truncated_response_is_an_outage_not_a_partial_answer() -> None:
    """Fewer vectors than inputs would silently shift every document by one."""
    provider = _provider(_FakeHTTP(body={"data": [{"index": 0, "embedding": [0.0] * 768}]}))
    with pytest.raises(EmbeddingUnavailableError, match="for 2 inputs"):
        provider.embed_documents(["one", "two"])


def test_documents_are_embedded_in_batches() -> None:
    client = _FakeHTTP(dimensions=4)
    provider = _provider(client, dimensions=4, batch_size=32)
    vectors = provider.embed_documents([f"document {index}" for index in range(33)])

    assert len(vectors) == 33
    assert [len(request["json"]["input"]) for request in client.requests] == [32, 1]


def test_documents_and_queries_receive_the_identical_preprocessing() -> None:
    """The invariant the whole pipeline rests on: same text in, same vector out."""
    client = _FakeHTTP(dimensions=4)
    provider = _provider(client, dimensions=4)
    # Deliberately messy: leading and doubled spaces, a NO-BREAK SPACE (U+00A0), a ZERO WIDTH SPACE
    # (U+200B) and trailing newlines. Those two characters are the point of the test \u2014 they are what
    # `normalize_text` exists to fold away. Written as escapes rather than pasted literally: an
    # invisible character in source is a defect waiting to be "tidied" away by a formatter or a
    # future reader, and the escape says out loud which one is meant and why it is there.
    messy = "  Elena \u00a0Marquez\u200b   cardiologist\n\n"

    documents = provider.embed_documents([messy])
    query = provider.embed_query(messy)

    assert documents[0] == query
    assert client.requests[0]["json"]["input"] == [normalize_text(messy)]
    assert client.requests[1]["json"]["input"] == [normalize_text(messy)]
    assert client.requests[0]["json"]["model"] == client.requests[1]["json"]["model"]
    assert client.requests[0]["json"]["dimensions"] == client.requests[1]["json"]["dimensions"]


def test_an_empty_document_does_not_produce_empty_input() -> None:
    """Some providers reject "" outright, which would take down a whole batch."""
    client = _FakeHTTP(dimensions=4)
    provider = _provider(client, dimensions=4)
    provider.embed_documents(["   "])
    assert client.requests[0]["json"]["input"] == [" "]


def test_model_calls_are_recorded_per_text_not_per_request() -> None:
    """The seed rung's "no model call at all" claim is only as good as this counter."""
    ledger = CallLedger()
    provider = _provider(_FakeHTTP(dimensions=4), dimensions=4, ledger=ledger)
    provider.embed_documents(["a", "b", "c"])
    provider.embed_query("q")

    assert ledger.model_calls == 4
    assert ledger.network_calls == 4
    assert ledger.kinds() == ["embedding.document", "embedding.query"]


def test_the_offline_provider_is_flagged_synthetic() -> None:
    provider = HashingEmbeddingProvider(dimensions=16)
    assert provider.synthetic is True
    assert provider.dimensions == 16
    assert "synthetic" in provider.describe()


def test_the_offline_provider_is_deterministic_and_normalized() -> None:
    provider = HashingEmbeddingProvider(dimensions=16)
    first = provider.embed_documents(["Elena Marquez, MD is a cardiologist."])
    second = provider.embed_query("Elena Marquez, MD is a cardiologist.")
    assert first[0] == second
    assert pytest.approx(sum(value * value for value in first[0]), rel=1e-9) == 1.0
    assert all(-1.0 <= value <= 1.0 for value in first[0])


def test_the_offline_provider_moves_with_text_overlap() -> None:
    provider = HashingEmbeddingProvider(dimensions=64)
    near = provider.embed_query("Elena Marquez cardiologist Chicago")
    far = provider.embed_query("completely unrelated tokens here")

    def cosine(left: Sequence[float], right: Sequence[float]) -> float:
        return sum(a * b for a, b in zip(left, right, strict=True))

    assert cosine(near, near) > cosine(near, far)


def test_an_offline_environment_builds_the_local_provider(settings: Settings) -> None:
    offline = _with(settings, EMBEDDING_BACKEND="offline", OPENROUTER_API_KEY="")
    provider = build_embedding_provider(offline)
    assert isinstance(provider, HashingEmbeddingProvider)
    assert provider.dimensions == settings.embedding_dimensions


def test_a_live_environment_without_a_key_refuses_to_start(settings: Settings) -> None:
    """A missing credential is not a degradation: it is a misconfiguration, and it says so."""
    live = _with(settings, EMBEDDING_BACKEND="live", OPENROUTER_API_KEY="")
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        build_embedding_provider(live)


def test_the_live_environment_selects_the_openrouter_adapter(settings: Settings) -> None:
    """`EMBEDDING_PROVIDER=vertex` is pinned by ADR-0006; the substitution is made explicit here
    and recorded in ADR-0029 rather than hidden in a default."""
    live = _with(settings, EMBEDDING_BACKEND="live", OPENROUTER_API_KEY="present")
    provider: EmbeddingProvider = build_embedding_provider(live)
    assert isinstance(provider, OpenRouterEmbeddingProvider)
    assert provider.dimensions == live.embedding_dimensions
    assert live.embedding_provider == "vertex", "the pin itself is untouched"


def test_a_zero_width_provider_is_refused_at_construction() -> None:
    with pytest.raises(EmbeddingDimensionError, match="must be positive"):
        _provider(_FakeHTTP(), dimensions=0)


def _with(settings: Settings, **overrides: str) -> Settings:
    """Settings with environment values replaced, as a process environment would."""
    from dataclasses import replace

    env = {**dict(settings.env), **overrides}
    return replace(
        settings,
        env=env,
        embedding_backend=overrides.get("EMBEDDING_BACKEND", settings.embedding_backend),
        openrouter_api_key=overrides.get("OPENROUTER_API_KEY") or None,
    )
