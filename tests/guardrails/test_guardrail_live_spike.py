# ruff: noqa: RUF001  # §9's Turkish is quoted verbatim
"""The one live call: §9's layer 2 against the real OpenRouter decision model (phase 4.2).

Everything else about layer 2 is exercised without a provider — the prompt, the parsing, the closed
vocabulary, the refusal rules, and the HTTP path over ``httpx.MockTransport``. This module is the
other half of that claim and nothing more: it makes the call the mock cannot make, on the masked
message §9 specifies, and reports what came back.

**How this module is allowed to skip.** The Phase 0 spikes in `tests/contracts/spikes/` fail rather
than skip, because each of them verifies a pin the project cannot ship without. This one is not that:
the offline profile is the default and a complete configuration (§9's `MODEL_BACKEND=offline`), the
provider path is already covered over a mock transport, and a demo that spent money every time
someone ran `make spike` would be a worse demo. So the live call happens only when an operator asks
for it, with ``GUARDRAIL_LIVE_SPIKE=1`` — and once they have asked, a missing key or an unresolved pin
is a **failure**, not a quiet skip, because at that point the operator believes a live run happened.

Run it explicitly:

    GUARDRAIL_LIVE_SPIKE=1 uv run --no-sync pytest tests/guardrails/test_guardrail_live_spike.py -m spike -q -s

What is asserted is deliberately weaker than "the model is right": a valid decision, attributed to
the model that produced it, with no identifier in the message it was shown. The model's *judgement*
is recorded and reported rather than asserted — a spike that demanded the model block a given
sentence would be a test of the model's weights, and it would fail for reasons nobody here can fix.
"""

from __future__ import annotations

import os

import pytest
from guardrail_fixtures import case

from referraleasy_contracts.generated.common import GuardrailCategory, GuardrailDecision
from referraleasy_guardrails.config import ConfigError, GuardrailSettings
from referraleasy_guardrails.layer_decision_model import (
    DECISION_SYSTEM_PROMPT,
    DecisionModelGuardrail,
    OpenRouterDecisionModelClient,
    build_user_prompt,
    parse_decision,
    unmasked_identifiers,
)
from referraleasy_guardrails.types import ScreeningRequest, is_unavailable, mask_pii

pytestmark = pytest.mark.spike

#: The opt-in. Set to anything other than "0"/"" to run the live call.
OPT_IN = "GUARDRAIL_LIVE_SPIKE"

#: A benign message carrying both identifiers the masker exists for: a seven-digit member id and an
#: email. The layer refuses to send either, so a completed call is evidence that the masking held.
BENIGN = "Merhaba, üye numaram 2205518 ve e-postam hasta@example.com. Salı günü randevu istiyorum."


def _settings() -> GuardrailSettings:
    try:
        settings = GuardrailSettings.from_env()
    except ConfigError as error:
        pytest.fail(
            "GUARDRAIL_LIVE_SPIKE was set, so a live run was requested and the checkout's own "
            f"configuration does not resolve:\n\n{error}\n\n"
            "Nothing was sent. Fix the pins, or unset the flag to leave the live path alone.",
            pytrace=False,
        )
    if not settings.live_model_backend:
        pytest.fail(
            "GUARDRAIL_LIVE_SPIKE was set but MODEL_BACKEND is not `live`, so no call can be made. "
            f"MODEL_BACKEND={settings.model_backend!r}. Set MODEL_BACKEND=live in .env for this run "
            "only, rather than editing the file the other agents are using.",
            pytrace=False,
        )
    if not settings.decision_model or not settings.openrouter_api_key:
        pytest.fail(
            "GUARDRAIL_LIVE_SPIKE was set but the pin is incomplete: "
            f"GUARDRAIL_DECISION_MODEL={settings.decision_model!r} "
            f"and OPENROUTER_API_KEY is {'set' if settings.openrouter_api_key else 'empty'}. "
            "§9's model must be chosen explicitly: `CEV` is not a resolved model id, and neither is "
            "an empty string.",
            pytrace=False,
        )
    return settings


@pytest.fixture(scope="module")
def live_settings() -> GuardrailSettings:
    if os.environ.get(OPT_IN, "").strip() in {"", "0"}:
        pytest.skip(
            f"live model call not requested; set {OPT_IN}=1 to run it. The offline profile is a "
            "complete configuration (§9), and layer 2's provider path is mocked in "
            "tests/guardrails/test_guardrail_layer2_transport.py."
        )
    return _settings()


@pytest.fixture(scope="module")
def live_client(live_settings: GuardrailSettings) -> OpenRouterDecisionModelClient:
    """The real client, against the real endpoint, with the documented model id."""
    return OpenRouterDecisionModelClient(
        live_settings.openrouter_api_key, base_url=live_settings.openrouter_base_url
    )


@pytest.fixture(scope="module")
def live_layer(
    live_settings: GuardrailSettings, live_client: OpenRouterDecisionModelClient
) -> DecisionModelGuardrail:
    return DecisionModelGuardrail(
        live_settings.decision_model,
        client=live_client,
        timeout_ms=live_settings.total_timeout_ms,
    )


def test_the_live_reply_is_either_a_decision_or_a_reason_to_close_the_input_path(
    live_settings: GuardrailSettings,
    live_layer: DecisionModelGuardrail,
    live_client: OpenRouterDecisionModelClient,
) -> None:
    """The wire finding, printed, with the invariant §9 actually requires asserted.

    Two things can come back from a real model, and only one of them may be treated as an answer.
    This test prints the raw reply — the evidence this spike exists to produce — and asserts the rule
    that holds whichever way it went: a reply the parser does not recognise becomes a refusal that
    closes the input path, never an allow. §9's closed-dependency rule is the reason that invariant is
    the assertion rather than "the model said allow".
    """
    assert unmasked_identifiers(mask_pii(BENIGN)) == (), (
        "the masker leaks before the call is even made, so the rest of this test would be measuring "
        "a message the layer is going to refuse"
    )
    masked = mask_pii(BENIGN)
    request = ScreeningRequest(text=BENIGN, workflow_state="INTAKE")

    raw = live_client.complete(
        system=DECISION_SYSTEM_PROMPT,
        user=build_user_prompt(request, masked),
        model_id=live_settings.decision_model,
        timeout_ms=live_settings.total_timeout_ms,
    )
    parsed = parse_decision(raw.text)
    print(
        "\nlive decision-model reply (raw, exactly as the provider returned it)\n"
        f"  model             : {raw.model_id}\n"
        f"  raw text          : {raw.text!r}\n"
        f"  parses as         : {parsed.decision.value if parsed else 'NOT A DECISION'}\n"
        f"  latency_ms        : {raw.latency_ms}\n"
        f"  prompt tokens     : {raw.prompt_tokens}\n"
        f"  completion tokens : {raw.completion_tokens}\n"
        f"  cost_usd          : {raw.cost_usd}\n"
    )

    try:
        answer = live_layer.screen(request, masked)
    except Exception as error:  # the class depends on what the pipeline in this process catches
        assert is_unavailable(error), f"a live failure that is not a closure: {error!r}"
        assert parsed is None, (
            "the layer refused a reply the parser accepted, which is a different bug from the one "
            "this file reports"
        )
        print(
            "\n  the layer closed the input path instead of answering:\n"
            f"    {error.reason if hasattr(error, 'reason') else error}\n"
            "  §9: a layer without an answer refuses the write; it never becomes an allow.\n"
        )
        return

    assert answer.decision in set(GuardrailDecision), answer.decision
    assert answer.category in set(GuardrailCategory), answer.category
    assert answer.method == f"openrouter:{live_settings.decision_model}"
    assert answer.reason.strip()
    assert parsed is not None and parsed.decision is answer.decision, (
        "a live verdict that does not match what the reply parsed to means the layer and the parser "
        "disagree about the same bytes"
    )


def test_a_prompt_injection_from_the_corpus_reaches_the_model_and_comes_back_a_decision(
    live_settings: GuardrailSettings,
    live_layer: DecisionModelGuardrail,
    live_client: OpenRouterDecisionModelClient,
) -> None:
    """The attack the corpus carries, sent masked: the model's answer is recorded, not demanded.

    §9 does not promise that the model blocks every injection — it promises that whatever the model
    says is parsed into the contract's vocabulary and can never be *executed*, and that a reply which
    is not a decision is not an allow. That is what is asserted; the raw reply is printed, because
    "the layer refused" without the bytes behind it is not evidence anyone can act on.
    """
    entry = case("attack-injection-en-ignore-previous")
    request = ScreeningRequest(
        text=entry.text, workflow_state=entry.workflow_state, synthetic=False
    )
    masked = mask_pii(entry.text)
    assert unmasked_identifiers(masked) == ()

    raw = live_client.complete(
        system=DECISION_SYSTEM_PROMPT,
        user=build_user_prompt(request, masked),
        model_id=live_settings.decision_model,
        timeout_ms=live_settings.total_timeout_ms,
    )
    parsed = parse_decision(raw.text)
    print(
        "\nlive injection screening (raw, exactly as the provider returned it)\n"
        f"  case              : {entry.case_id} (corpus expectation: {entry.expected.value})\n"
        f"  prompt            : {masked!r}\n"
        f"  raw text          : {raw.text!r}\n"
        f"  parses as         : {parsed.decision.value if parsed else 'NOT A DECISION'}\n"
        f"  latency_ms        : {raw.latency_ms}\n"
        f"  prompt tokens     : {raw.prompt_tokens}\n"
        f"  completion tokens : {raw.completion_tokens}\n"
        f"  cost_usd          : {raw.cost_usd}\n"
    )

    try:
        answer = live_layer.screen(request, masked)
    except Exception as error:
        assert is_unavailable(error), f"a live failure that is not a closure: {error!r}"
        assert parsed is None, (
            "the layer refused a reply the parser accepted, which is a different bug from the one "
            "this file reports"
        )
        print(
            "\n  the layer closed the input path instead of answering:\n"
            f"    {getattr(error, 'reason', error)}\n"
            "  §9: a layer without an answer refuses the write; it never becomes an allow.\n"
        )
        return

    assert answer.decision in set(GuardrailDecision)
    assert answer.method == f"openrouter:{live_layer.model_id}"
    assert parsed is not None and parsed.decision is answer.decision
    print(
        "\nlive injection verdict\n"
        f"  decision  : {answer.decision.value}\n"
        f"  category  : {answer.category.value}\n"
        f"  reason    : {answer.reason}\n"
    )
