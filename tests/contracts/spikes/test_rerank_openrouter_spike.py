"""Spike S-07: OpenRouter rerank request/response shape for cohere/rerank-v3.5.

Verified from the vendor's machine-readable spec (https://openrouter.ai/openapi.json,
paths -> /rerank -> post) and recorded in docs/adr/ADR-0007. This file turns that
spec reading into an executable assertion, because a spec example is not a working
call.

Requires OPENROUTER_API_KEY and RERANK_MODEL_ID. Run with ``make spike``.
"""

from __future__ import annotations

import os

import pytest
from _spike_env import require_env, require_module

pytestmark = pytest.mark.spike

ADR = "ADR-0007-rerank-openrouter-cohere-rerank-v3-5.md"
ENDPOINT = "https://openrouter.ai/api/v1/rerank"

DOCUMENTS = [
    "Cardiology referral for a 61 year old with exertional chest pain.",
    "Dermatology referral for a persistent facial rash.",
    "Orthopaedic referral after a fall with wrist pain.",
    "Gastroenterology referral for reflux symptoms.",
]
QUERY = "urgent heart-related referral"


def _requirements() -> tuple[str, str]:
    api_key = require_env(
        "OPENROUTER_API_KEY",
        purpose="authenticates POST /api/v1/rerank",
        where="OpenRouter dashboard -> Keys -> Create key (https://openrouter.ai/keys)",
        adr=ADR,
    )
    model = require_env(
        "RERANK_MODEL_ID",
        purpose="the rerank model id this spike verifies",
        where="the vendor spec only ever names `cohere/rerank-v3.5`, and the public "
        "/api/v1/models catalogue contains no rerank models at all - so the id cannot be "
        "confirmed without a call; this spike is that call",
        adr=ADR,
    )
    return api_key, model


def test_rerank_request_response_shape() -> None:
    api_key, model = _requirements()
    httpx = require_module("httpx", distribution="httpx", adr=ADR)

    payload: dict[str, object] = {
        "model": model,
        "query": QUERY,
        "documents": DOCUMENTS,
        "top_n": 2,
    }
    with httpx.Client(timeout=float(os.environ.get("SPIKE_HTTP_TIMEOUT_S", "30"))) as client:
        response = client.post(
            ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if response.status_code != 200:
        pytest.fail(
            f"RERANK CALL FAILED: HTTP {response.status_code}\n\n"
            f"POST {ENDPOINT}\n{response.text[:2000]}\n\n"
            "Status meanings (ADR-0007): 401 bad/missing key, 402 insufficient credits, "
            "403 forbidden, 404 model id not served by this account, 429 rate limited.\n"
            f"If the body says the model is unknown, RERANK_MODEL_ID={model!r} is not a "
            f"valid pin - record that in docs/adr/{ADR} and pick a served model.",
            pytrace=False,
        )

    body = response.json()

    # Required response fields (spec: required = ["model", "results"]).
    assert "results" in body, f"response has no `results` key: {sorted(body)}"
    assert "model" in body, f"response has no `model` key: {sorted(body)}"
    results = body["results"]
    assert isinstance(results, list) and results, f"`results` is empty or not a list: {results!r}"

    # Each result carries exactly index + relevance_score + document.
    for item in results:
        for field in ("index", "relevance_score", "document"):
            assert field in item, f"result item is missing {field!r}: {item!r}"
        assert isinstance(item["index"], int), f"index is not an int: {item['index']!r}"
        assert isinstance(item["relevance_score"], (int, float)), (
            f"relevance_score is not numeric: {item['relevance_score']!r}"
        )
        assert 0 <= item["index"] < len(DOCUMENTS), (
            f"index {item['index']} does not address the input list of {len(DOCUMENTS)} "
            "documents - the join key back to the caller is unusable"
        )

    # Results are documented as sorted by relevance; assert it rather than trusting it.
    scores = [item["relevance_score"] for item in results]
    assert scores == sorted(scores, reverse=True), f"results were not sorted by relevance: {scores}"

    # top_n is honoured.
    assert len(results) <= 2, f"top_n=2 returned {len(results)} results"

    # The most relevant document must be the cardiology one, else the rerank is noise
    # and a rerank cut-off threshold derived from this model would be meaningless.
    top_text = str(results[0]["document"])
    assert "Cardiology" in top_text, (
        "the reranker did not put the cardiology document first "
        f"(got: {top_text!r}). The call works but the ranking does not, so no threshold "
        f"can be measured from it. Record this in docs/adr/{ADR}."
    )


def test_relevance_score_range_is_recorded() -> None:
    """Probe the score scale.

    The spec documents no range for `relevance_score` - it may be a normalised
    similarity or a raw logit, and it may vary by upstream provider. A guardrail-style
    cut-off must therefore be measured, not assumed (ADR-0007). This test records the
    observed range and fails only if the scores are non-numeric or non-finite.
    """
    api_key, model = _requirements()
    httpx = require_module("httpx", distribution="httpx", adr=ADR)

    with httpx.Client(timeout=float(os.environ.get("SPIKE_HTTP_TIMEOUT_S", "30"))) as client:
        response = client.post(
            ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"model": model, "query": QUERY, "documents": DOCUMENTS},
        )
    if response.status_code != 200:
        pytest.fail(
            f"RERANK SCORE PROBE FAILED: HTTP {response.status_code}\n\n{response.text[:2000]}",
            pytrace=False,
        )

    scores = [float(item["relevance_score"]) for item in response.json()["results"]]
    assert scores, "no scores returned"
    assert all(score == score for score in scores), "a score was NaN"
    print(f"\n[RERANK SCORE RANGE] n={len(scores)} min={min(scores):.6f} max={max(scores):.6f}")
    print(f"[RERANK SCORE RANGE] sorted={sorted(scores, reverse=True)}")
    print(
        "[RERANK SCORE RANGE] if max <= 1.0 the scale is plausibly normalised; "
        "anything else means a cut-off threshold must be established from measurement. "
        f"Record the range in docs/adr/{ADR}."
    )

    assert max(scores) > 0, "every score was zero - the reranker returned no signal"
