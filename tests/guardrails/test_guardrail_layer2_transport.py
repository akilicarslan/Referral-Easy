"""Layer 2's provider path, over ``httpx.MockTransport`` — no network, no credential, real bytes (§9).

The layer's rules are tested in ``test_guardrail_layer2_rules.py`` with an injected client. This file
tests the part that file deliberately does not: the HTTP call itself — the request body, the headers,
the timeout, the status-code handling and the response parsing.

Two claims are load-bearing here and are asserted rather than described:

* **the credential never reaches a message.** A 4xx from the provider raises a ``DecisionModelError``
  whose text is the provider's own words; the test greps the raised message for the key. §9's masking
  rule gets the patient's identifiers out of the request, and this is the same rule for the token that
  authorises it.
* **the classification is not sampled.** ``temperature`` is 0 in the body, because a verdict that
  changes between two identical messages cannot be measured, and §9's per-layer numbers are the whole
  reason the layer is being measured at all.

Nothing here reaches a provider, and nothing here reads the environment: the live call is a separate,
deselected spike (``test_guardrail_live_spike.py``), because this checkout's ``MODEL_BACKEND`` is
``live`` and a test that built the real client from the environment would spend money the first time
it ran.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from referraleasy_guardrails.layer_decision_model import (
    DECISION_SYSTEM_PROMPT,
    MAX_COMPLETION_TOKENS,
    DecisionModelError,
    OpenRouterDecisionModelClient,
    parse_decision,
)

BASE_URL = "https://openrouter.test/api/v1"
MODEL_ID = "openai/gpt-5-nano"
#: Obviously not a credential, and it never leaves the process: ``MockTransport`` answers every request.
TEST_KEY = "test-key-not-a-credential"

CHAT_COMPLETION = {
    "id": "gen-123",
    "model": MODEL_ID,
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": '{"decision": "allow", "category": "benign", "reason": "ordinary"}',
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 210, "completion_tokens": 24, "cost": 0.000042},
}

#: The reply the pinned model actually returned on the **first live call** (2026-10-15), reproduced
#: field by field: a reasoning model with an undersized budget spends every completion token on
#: reasoning and returns `content: null` with `finish_reason: length`. This payload is in the file
#: because it is the one that would have refused every message in a live deployment.
TRUNCATED_BY_BUDGET = {
    "id": "gen-live-1",
    "model": MODEL_ID,
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "reasoning": None,
                "reasoning_details": [{"type": "reasoning.encrypted", "data": "gAAAAA-not-a-key"}],
            },
            "finish_reason": "length",
            "native_finish_reason": "length",
        }
    ],
    "usage": {
        "prompt_tokens": 476,
        "completion_tokens": 192,
        "cost": 0.0001006,
        "completion_tokens_details": {"reasoning_tokens": 192},
    },
}


def client_for(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    api_key: str = TEST_KEY,
) -> OpenRouterDecisionModelClient:
    return OpenRouterDecisionModelClient(
        api_key, base_url=BASE_URL, transport=httpx.MockTransport(handler)
    )


def test_the_request_body_is_the_classifier_call_and_not_a_chat() -> None:
    """The exact body, asserted field by field, because three of these fields are safety properties.

    ``temperature`` 0 keeps the verdict reproducible, ``response_format`` asks for the one small JSON
    object the parser accepts, and the completion budget bounds the reply so a provider cannot turn a
    screening call into a page of prose. The budget is asserted against the constant rather than a
    literal, and the constant carries the live measurement that set it: the pinned model is a
    reasoning model, so a budget sized for the answer alone truncates before the model answers at all.
    The header is asserted too, on its exact value rather than on "some Authorization is present".
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=CHAT_COMPLETION)

    response = client_for(handler).complete(
        system=DECISION_SYSTEM_PROMPT,
        user="<message>hello</message>",
        model_id=MODEL_ID,
        timeout_ms=2500,
    )

    assert len(seen) == 1
    request = seen[0]
    body = json.loads(request.content)
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/chat/completions"
    assert body["model"] == MODEL_ID
    assert body["temperature"] == 0
    assert body["max_tokens"] == MAX_COMPLETION_TOKENS
    assert body["max_tokens"] > 1000, (
        "a budget that fits only the JSON object is a budget that a reasoning model spends on "
        "reasoning and never answers with — measured live at 192 reasoning tokens for 200 allowed"
    )
    assert body["response_format"] == {"type": "json_object"}
    assert body["usage"] == {"include": True}
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == DECISION_SYSTEM_PROMPT
    assert body["messages"][1]["content"] == "<message>hello</message>"
    assert request.headers["authorization"] == f"Bearer {TEST_KEY}"
    assert response.text.startswith('{"decision": "allow"')
    assert response.model_id == MODEL_ID


def test_the_timeout_is_sent_in_seconds_and_the_usage_comes_back_as_reported() -> None:
    """Milliseconds in the configuration, seconds on the wire, and the provider's own numbers out.

    The timeout is read off the request httpx actually built, so an edit that passed milliseconds
    straight to httpx would show up here as a 2500-second timeout rather than as a comment nobody
    reads.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=CHAT_COMPLETION)

    response = client_for(handler).complete(
        system=DECISION_SYSTEM_PROMPT, user="hello", model_id=MODEL_ID, timeout_ms=2500
    )
    assert seen[0].extensions["timeout"]["read"] == 2.5
    assert response.prompt_tokens == 210
    assert response.completion_tokens == 24
    assert response.cost_usd == pytest.approx(0.000042)
    assert response.request_id == "gen-123"


def test_a_provider_that_does_not_report_usage_leaves_the_fields_unmeasured() -> None:
    """Absent is not zero: the provider said nothing, so the layer says nothing (``usage_totals``)."""
    payload = {key: value for key, value in CHAT_COMPLETION.items() if key != "usage"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    response = client_for(handler).complete(
        system=DECISION_SYSTEM_PROMPT, user="hello", model_id=MODEL_ID, timeout_ms=2500
    )
    assert response.prompt_tokens is None
    assert response.completion_tokens is None
    assert response.cost_usd is None
    assert response.text.startswith('{"decision"')


def test_an_error_status_keeps_the_provider_s_message_and_never_the_credential() -> None:
    """A 404 from OpenRouter means "no endpoint serves this model", which an operator must see.

    The message carries the provider's own words and is grepped for the credential: the failure this
    guards against is an error string that logs the bearer token alongside the diagnosis.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"error": {"message": f"No endpoints found for {MODEL_ID}."}}
        )

    with pytest.raises(DecisionModelError) as raised:
        client_for(handler).complete(
            system=DECISION_SYSTEM_PROMPT, user="hello", model_id=MODEL_ID, timeout_ms=2500
        )
    message = str(raised.value)
    assert "404" in message
    assert "No endpoints found" in message
    assert TEST_KEY not in message


def test_an_error_body_is_truncated_so_a_proxy_page_cannot_flood_a_log() -> None:
    """A gateway in front of the provider may answer with HTML; 200 characters of it come through."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>" + "x" * 5000 + "</html>")

    with pytest.raises(DecisionModelError) as raised:
        client_for(handler).complete(
            system=DECISION_SYSTEM_PROMPT, user="hello", model_id=MODEL_ID, timeout_ms=2500
        )
    message = str(raised.value)
    assert "502" in message
    assert "xxxx" in message
    assert len(message) < 400, (
        "the provider's body must not be able to grow the error without limit"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"error": "not a completion"},
        {"choices": [{"message": {"content": None}}]},
        {},
    ],
)
def test_a_body_that_is_not_a_chat_completion_is_an_error(payload: dict[str, object]) -> None:
    """A 200 carrying something else is a failure to answer, and the layer closes rather than allows.

    Note what is *not* here: a completion with a non-string content is not turned into an empty text
    and passed on to the parser. It is a broken reply, and the layer's closure reason should say so.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(DecisionModelError) as raised:
        client_for(handler).complete(
            system=DECISION_SYSTEM_PROMPT, user="hello", model_id=MODEL_ID, timeout_ms=2500
        )
    assert "not a chat completion" in str(raised.value)


def test_a_reply_cut_off_at_the_token_budget_is_named_as_a_budget_problem() -> None:
    """The live failure, as an offline test: the fix is a client constant, not a parser change.

    An operator reading "the reply was not a chat completion" would look at the transport, the
    provider and the response shape — every one of them working perfectly in the call this payload
    reproduces. The reason has to name the budget and the reasoning tokens that consumed it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=TRUNCATED_BY_BUDGET)

    with pytest.raises(DecisionModelError) as raised:
        client_for(handler).complete(
            system=DECISION_SYSTEM_PROMPT, user="hello", model_id=MODEL_ID, timeout_ms=2500
        )
    reason = str(raised.value)
    assert "cut off at the token budget" in reason
    assert "finish_reason=length" in reason
    assert "192 of 192 completion tokens were reasoning" in reason
    assert "not a chat completion" not in reason, (
        "blaming the response shape instead of the budget sends an operator to the wrong file"
    )


def test_a_budget_error_without_the_provider_s_breakdown_says_so_rather_than_counting_to_zero() -> (
    None
):
    """A provider that reports no reasoning tokens leaves that unmeasured, not zero."""
    payload = {
        **TRUNCATED_BY_BUDGET,
        "usage": {"prompt_tokens": 476, "completion_tokens": 192},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(DecisionModelError) as raised:
        client_for(handler).complete(
            system=DECISION_SYSTEM_PROMPT, user="hello", model_id=MODEL_ID, timeout_ms=2500
        )
    assert "the provider reported no reasoning breakdown" in str(raised.value)


def test_a_reasoning_model_that_answered_is_a_completion_like_any_other() -> None:
    """The shape that works live: ``reasoning_details`` present, ``content`` a string, budget enough."""
    payload = {
        **CHAT_COMPLETION,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": '{"decision": "allow", "category": "benign", "reason": "on-topic"}',
                    "reasoning": None,
                    "reasoning_details": [
                        {"type": "reasoning.encrypted", "data": "gAAAAA-redacted"}
                    ],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 476,
            "completion_tokens": 389,
            "cost": 0.0001794,
            "completion_tokens_details": {"reasoning_tokens": 320},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    response = client_for(handler).complete(
        system=DECISION_SYSTEM_PROMPT, user="hello", model_id=MODEL_ID, timeout_ms=2500
    )
    assert response.completion_tokens == 389, (
        "the reported completion includes the reasoning tokens; that is the provider's number, and "
        "it is what the measured cost per screening is computed from"
    )
    assert response.cost_usd == pytest.approx(0.0001794)
    assert parse_decision(response.text) is not None


def test_a_blank_content_is_a_broken_reply_rather_than_an_empty_decision() -> None:
    """Whitespace is not an answer: the parser would refuse it, and the reason should say which layer."""
    payload = {
        **CHAT_COMPLETION,
        "choices": [{"index": 0, "message": {"content": "   "}, "finish_reason": "stop"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(DecisionModelError) as raised:
        client_for(handler).complete(
            system=DECISION_SYSTEM_PROMPT, user="hello", model_id=MODEL_ID, timeout_ms=2500
        )
    assert "not a chat completion" in str(raised.value)


def test_a_transport_failure_is_reported_by_type_and_does_not_leak_the_credential() -> None:
    """A refused connection is named, not stringified wholesale — the exception text is what lands in logs."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("connection refused")

    with pytest.raises(DecisionModelError) as raised:
        client_for(handler).complete(
            system=DECISION_SYSTEM_PROMPT, user="hello", model_id=MODEL_ID, timeout_ms=2500
        )
    assert calls == [1]
    assert "ConnectError" in str(raised.value)
    assert TEST_KEY not in str(raised.value)


def test_an_empty_credential_refuses_before_a_request_is_made() -> None:
    """No key, no call. Asserted on the handler's call count, so a "fail open" path cannot hide."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        calls.append(1)
        return httpx.Response(200, json=CHAT_COMPLETION)

    with pytest.raises(DecisionModelError) as raised:
        client_for(handler, api_key="").complete(
            system=DECISION_SYSTEM_PROMPT, user="hello", model_id=MODEL_ID, timeout_ms=2500
        )
    assert "no OpenRouter credential" in str(raised.value)
    assert calls == []


def test_a_custom_base_url_is_honoured_and_a_trailing_slash_is_not_doubled() -> None:
    """The base URL is configuration (ADR-0002's provider path), not a hard-coded constant."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=CHAT_COMPLETION)

    client = OpenRouterDecisionModelClient(
        TEST_KEY, base_url="https://mirror.test/v1/", transport=httpx.MockTransport(handler)
    )
    client.complete(system=DECISION_SYSTEM_PROMPT, user="hello", model_id=MODEL_ID, timeout_ms=1000)
    assert seen == ["https://mirror.test/v1/chat/completions"]
