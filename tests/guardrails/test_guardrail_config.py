# ruff: noqa: RUF002  # §9's Turkish sentence and the vocabulary it names are quoted verbatim
"""The guardrail environment contract and the startup refusals it carries (§9).

§9 is explicit that the decision model is chosen rather than assumed — *"«CEV» çözülmüş model ID
değildir; uygun küçük model env'de açıkça seçilir"* — and this file is where that sentence is enforced:
the placeholder that §9 names, the vendor-less model id, the ``live`` backend with no model or no key,
the threshold that is not a number, and the timeout that is. Each one is a refusal at **startup**
rather than a surprise at the first patient message, and every problem in one configuration is
reported together, because an operator fixing one variable per deploy cycle is how the fourth one gets
skipped.

Two rules are asserted as behaviour rather than as wording:

* **A credential's value is never printed.** The refusal names ``OPENROUTER_API_KEY``; the test gives
  the key a value and requires that value to be absent from the message.
* **The threshold here is a number, not a policy.** The configuration checks that it *is* a number;
  whether it agrees with the measured record is layer 3's check (ADR-0005's intake rule), and the test
  pins that division of labour so neither place starts doing the other's job.

Both of those are also exercised through the layers themselves at the end of the file, because a
setting that reaches nothing is configuration theatre.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from guardrail_fixtures import ScriptedDecisionModelClient, json_reply

from referraleasy_contracts.generated.common import GuardrailCategory, GuardrailDecision
from referraleasy_guardrails.build import build_layers
from referraleasy_guardrails.config import (
    DEFAULT_ENV_FILE,
    DEFAULT_LOCAL_ARTIFACT_DIR,
    PLACEHOLDER_PREFIX,
    PLACEHOLDERS,
    ConfigError,
    GuardrailProfile,
    GuardrailSettings,
    is_placeholder,
    load_settings,
    parse_env_file,
    validate_pins,
)
from referraleasy_guardrails.layer_classifier import OFFLINE_METHOD_PIN
from referraleasy_guardrails.types import ScreeningRequest

#: §9's own example of a token that is not a resolved model id, plus the shapes the same habit
#: produces. The first one is the one that matters; the rest are there so the rule is not a special
#: case for one string.
PLACEHOLDER_VALUES = (
    "CEV",
    "cev",
    f"{PLACEHOLDER_PREFIX}-model",
    PLACEHOLDER_PREFIX,
    "tbd",
    "model",
)


def refused(env: dict[str, str]) -> ConfigError:
    """Resolve a configuration and return the refusal, failing the test if it is accepted."""
    with pytest.raises(ConfigError) as raised:
        GuardrailSettings.from_env(env)
    return raised.value


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------


def test_an_unset_profile_is_step_9_s_full_profile_and_an_unknown_one_is_refused() -> None:
    settings = GuardrailSettings.from_env({})
    assert settings.profile is GuardrailProfile.THREE_STAGE, (
        "§9's showcase profile is the default: unset means the full one, and it is documented"
    )
    assert GuardrailSettings().profile is GuardrailProfile.THREE_STAGE
    assert GuardrailSettings.from_env({"GUARDRAIL_MODE": "conditional"}).profile is (
        GuardrailProfile.CONDITIONAL
    )
    assert GuardrailSettings.from_env({"GUARDRAIL_MODE": "three_stage"}).profile is (
        GuardrailProfile.THREE_STAGE
    )

    error = refused({"GUARDRAIL_MODE": "fast"})
    message = str(error)
    assert "GUARDRAIL_MODE='fast' is not a known profile" in message
    assert "three_stage" in message and "conditional" in message, (
        "the refusal lists what is known, so the operator does not have to read the source"
    )
    # A near-miss spelling is a value that was *set*, so it is refused rather than silently defaulted.
    assert "GUARDRAIL_MODE" in str(refused({"GUARDRAIL_MODE": "Three-Stage"}))


def test_the_process_environment_wins_over_the_env_file_and_the_file_is_read_simply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GUARDRAIL_MODE", raising=False)
    env_file = tmp_path / DEFAULT_ENV_FILE
    env_file.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "export GUARDRAIL_MODE=conditional",
                'GUARDRAIL_LOCAL_THRESHOLD="0.5"',
                "NOT_A_SETTING",
                "  GUARDRAIL_TOTAL_TIMEOUT_MS = 1500  ",
            ]
        ),
        encoding="utf-8",
    )
    parsed = parse_env_file(env_file)
    assert parsed == {
        "GUARDRAIL_MODE": "conditional",
        "GUARDRAIL_LOCAL_THRESHOLD": "0.5",
        "GUARDRAIL_TOTAL_TIMEOUT_MS": "1500",
    }
    assert parse_env_file(tmp_path / "missing.env") == {}

    from_file = GuardrailSettings.from_env(repo_root=tmp_path)
    assert from_file.profile is GuardrailProfile.CONDITIONAL
    assert from_file.local_threshold == "0.5"
    assert from_file.total_timeout_ms == 1500

    monkeypatch.setenv("GUARDRAIL_MODE", "three_stage")
    assert GuardrailSettings.from_env(repo_root=tmp_path).profile is GuardrailProfile.THREE_STAGE, (
        "a shell variable has to be able to override the file, or a measured run cannot be repeated "
        "under the other profile without editing the checkout"
    )


def test_the_resolved_environment_is_kept_so_a_report_can_say_where_a_value_came_from() -> None:
    source = {"GUARDRAIL_MODE": "conditional"}
    settings = GuardrailSettings.from_env(source)
    assert settings.env == source
    assert settings.env is not source, (
        "the mapping is copied, so a caller cannot edit it afterwards"
    )
    assert load_settings(source) == settings


# ---------------------------------------------------------------------------
# The pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", PLACEHOLDER_VALUES)
def test_a_placeholder_decision_model_is_refused_with_step_9_s_own_example(value: str) -> None:
    assert is_placeholder(value) is True
    assert value.lower() in PLACEHOLDERS or value.upper().startswith(PLACEHOLDER_PREFIX)
    error = refused({"GUARDRAIL_DECISION_MODEL": value})
    message = str(error)
    assert "GUARDRAIL_DECISION_MODEL" in message
    assert "placeholder" in message and "(§9)" in message
    assert "Choose the small decision model explicitly" in message


def test_a_model_id_without_a_vendor_is_refused_rather_than_guessed() -> None:
    error = refused({"GUARDRAIL_DECISION_MODEL": "gpt-5-nano"})
    assert "names no vendor" in str(error)
    assert "<vendor>/<model>" in str(error)
    # The catalogue shape is accepted, and the placeholder vocabulary is not a prefix rule that would
    # also swallow a real model named after one of its words.
    accepted = GuardrailSettings.from_env({"GUARDRAIL_DECISION_MODEL": "openai/gpt-5-nano"})
    assert accepted.decision_model == "openai/gpt-5-nano"
    assert is_placeholder("openai/gpt-5-nano") is False
    assert is_placeholder("") is True, (
        "empty is the placeholder of placeholders, checked by its own rule"
    )


def test_a_live_backend_without_a_model_or_a_key_is_refused_and_both_are_reported() -> None:
    both = refused(
        {"MODEL_BACKEND": "live", "GUARDRAIL_DECISION_MODEL": "", "OPENROUTER_API_KEY": ""}
    )
    message = str(both)
    assert "MODEL_BACKEND=live but GUARDRAIL_DECISION_MODEL is empty" in message
    assert "MODEL_BACKEND=live but OPENROUTER_API_KEY is empty" in message

    no_key = refused({"MODEL_BACKEND": "live", "GUARDRAIL_DECISION_MODEL": "openai/gpt-5-nano"})
    assert "OPENROUTER_API_KEY is empty" in str(no_key)
    assert "GUARDRAIL_DECISION_MODEL is empty" not in str(no_key)

    no_model = refused({"MODEL_BACKEND": "live", "OPENROUTER_API_KEY": "x"})
    assert "GUARDRAIL_DECISION_MODEL is empty" in str(no_model)
    assert "OPENROUTER_API_KEY is empty" not in str(no_model)

    # The offline profile needs neither: zero network calls, so there is no key to have.
    offline = GuardrailSettings.from_env({"MODEL_BACKEND": "offline"})
    assert offline.live_model_backend is False
    # Whitespace is not a way to spell a different backend: a padded `LIVE` is live, so it needs the
    # same pins — refused when they are missing, accepted when they are there.
    assert "OPENROUTER_API_KEY is empty" in str(refused({"MODEL_BACKEND": "  LIVE  "}))
    padded = GuardrailSettings.from_env(
        {
            "MODEL_BACKEND": "  LIVE  ",
            "GUARDRAIL_DECISION_MODEL": "openai/gpt-5-nano",
            "OPENROUTER_API_KEY": "x",
        }
    )
    assert padded.live_model_backend is True
    assert padded.model_backend == "LIVE", (
        "the value is stripped, and only the comparison is folded"
    )


def test_a_credential_s_value_is_never_printed_in_a_refusal() -> None:
    """The refusal names variables. Printing a value would put a secret in a log line."""
    secret = "sk-or-v1-this-must-not-appear-anywhere"
    # The key is present and *doing its job* here: it is layer 3's threshold that is broken, and the
    # refusal is about that. A configuration with a key must not raise a problem about the key, and
    # must not quote it while it has one in hand.
    error = refused(
        {
            "MODEL_BACKEND": "live",
            "OPENROUTER_API_KEY": secret,
            "GUARDRAIL_DECISION_MODEL": "openai/gpt-5-nano",
            "GUARDRAIL_LOCAL_THRESHOLD": "very high",
        }
    )
    message = str(error)
    assert secret not in message
    assert "GUARDRAIL_LOCAL_THRESHOLD" in message
    assert "OPENROUTER_API_KEY" not in message, (
        "the key was present, so it is not the problem — a refusal about it would send the operator "
        "to the wrong variable"
    )

    # And the value does reach the client that needs it (or a live deployment could not call at all).
    accepted = GuardrailSettings.from_env(
        {
            "MODEL_BACKEND": "live",
            "OPENROUTER_API_KEY": secret,
            "GUARDRAIL_DECISION_MODEL": "openai/gpt-5-nano",
        }
    )
    assert accepted.openrouter_api_key == secret
    assert secret not in repr(accepted), "a settings repr that echoes the key is a key in a log"


def test_a_non_numeric_or_non_positive_number_is_refused_by_key() -> None:
    assert "GUARDRAIL_TOTAL_TIMEOUT_MS='soon' is not an integer" in str(
        refused({"GUARDRAIL_TOTAL_TIMEOUT_MS": "soon"})
    )
    assert "must be positive" in str(refused({"GUARDRAIL_TOTAL_TIMEOUT_MS": "0"}))
    assert "must be positive" in str(refused({"GUARDRAIL_TOTAL_TIMEOUT_MS": "-1"}))
    assert "GUARDRAIL_MAX_MESSAGE_CHARS='lots' is not an integer" in str(
        refused({"GUARDRAIL_MAX_MESSAGE_CHARS": "lots"})
    )
    assert (
        GuardrailSettings.from_env({"GUARDRAIL_TOTAL_TIMEOUT_MS": "1200"}).total_timeout_ms == 1200
    )
    assert GuardrailSettings.from_env({}).max_message_chars == 2000


def test_the_threshold_has_to_be_a_number_here_and_a_measured_value_there() -> None:
    """The division of labour in one test: this module checks the shape, layer 3 checks the record."""
    assert "is not a number" in str(refused({"GUARDRAIL_LOCAL_THRESHOLD": "high"}))
    # A number that disagrees with the record is *config*, and the configuration accepts it: the
    # refusal belongs to the layer, which knows what was measured (an env threshold is not a policy).
    settings = GuardrailSettings.from_env({"GUARDRAIL_LOCAL_THRESHOLD": "0.99"})
    assert settings.local_threshold == "0.99"
    assert GuardrailSettings.from_env({"GUARDRAIL_LOCAL_THRESHOLD": "0.5"}).local_threshold == "0.5"
    assert GuardrailSettings.from_env({}).local_threshold == ""


def test_every_problem_in_one_configuration_is_reported_together() -> None:
    error = refused(
        {
            "GUARDRAIL_MODE": "turbo",
            "GUARDRAIL_TOTAL_TIMEOUT_MS": "0",
            "GUARDRAIL_DECISION_MODEL": "CHANGEME",
            "GUARDRAIL_LOCAL_THRESHOLD": "very high",
            "MODEL_BACKEND": "live",
            "OPENROUTER_API_KEY": "",
        }
    )
    message = str(error)
    for name in (
        "GUARDRAIL_MODE",
        "GUARDRAIL_TOTAL_TIMEOUT_MS",
        "GUARDRAIL_DECISION_MODEL",
        "GUARDRAIL_LOCAL_THRESHOLD",
        "OPENROUTER_API_KEY",
    ):
        assert name in message, f"{name} was not reported"
    assert message.count("  - ") >= 4, "the problems are a list, not the first one found"


def test_validation_is_available_to_a_caller_that_builds_the_settings_itself() -> None:
    """``from_env`` validates; a hand-built settings object is validated by asking."""
    hand_built = GuardrailSettings(decision_model="CEV", total_timeout_ms=0)
    validate_pins(GuardrailSettings(decision_model="openai/gpt-5-nano"))
    with pytest.raises(ConfigError) as raised:
        validate_pins(hand_built)
    assert "GUARDRAIL_DECISION_MODEL" in str(raised.value)
    assert "GUARDRAIL_TOTAL_TIMEOUT_MS" in str(raised.value)


def test_the_defaults_do_not_depend_on_the_checkout_s_state() -> None:
    settings = GuardrailSettings.from_env({})
    assert settings.artifact_dir == Path(DEFAULT_LOCAL_ARTIFACT_DIR).expanduser()
    assert settings.openrouter_base_url == "https://openrouter.ai/api/v1"
    assert settings.model_backend == "offline", (
        "an unset backend is the offline one: the expensive profile is opted into, never inherited"
    )
    assert settings.decision_model == "" and settings.local_model == ""
    assert settings.openrouter_api_key == ""

    explicit = GuardrailSettings.from_env(
        {
            "GUARDRAIL_LOCAL_ARTIFACT_DIR": "~/somewhere/else",
            "OPENROUTER_BASE_URL": "",
            "MODEL_BACKEND": "  ",
            "OPENROUTER_API_KEY": "  spaced  ",
        }
    )
    assert explicit.artifact_dir == Path("~/somewhere/else").expanduser()
    assert explicit.openrouter_base_url == "https://openrouter.ai/api/v1", (
        "a whitespace base URL falls back to the documented default rather than to an empty string"
    )
    assert explicit.model_backend == "offline"
    assert explicit.openrouter_api_key == "spaced", "values are stripped of surrounding whitespace"


# ---------------------------------------------------------------------------
# The settings reach the layers, or they are decoration
# ---------------------------------------------------------------------------


def test_the_message_cap_from_the_environment_is_the_one_layer_one_enforces() -> None:
    """The cap is not decoration: the layer that reads it is the one §9 gives the length limit to."""
    text = "bu mesaj yirmi karakterden uzun"
    request = ScreeningRequest(text=text, workflow_state="INTAKE")

    niggardly = build_layers(GuardrailSettings(max_message_chars=20))
    verdict = niggardly.regex.screen(request, text)
    assert verdict.decision is GuardrailDecision.block
    assert verdict.category is GuardrailCategory.oversized_input
    assert "20" in verdict.reason

    generous = build_layers(GuardrailSettings(max_message_chars=2000))
    assert generous.regex.screen(request, text).decision is GuardrailDecision.allow


def test_the_timeout_from_the_environment_is_the_one_layer_two_hands_the_client() -> None:
    client = ScriptedDecisionModelClient(replies=[json_reply()])
    layers = build_layers(
        GuardrailSettings(decision_model="openai/gpt-5-nano", total_timeout_ms=1234),
        client=client,
    )
    layers.decision_model.screen(
        ScreeningRequest(text="merhaba", workflow_state="INTAKE"), "merhaba"
    )
    assert client.calls[0]["timeout_ms"] == 1234
    assert client.calls[0]["model_id"] == "openai/gpt-5-nano"


def test_the_offline_rung_is_the_only_pin_that_needs_no_artifact() -> None:
    """ADR-0005: the offline rung is named, not downloaded; a model id without a revision is refused."""
    layers = build_layers(GuardrailSettings(local_model=OFFLINE_METHOD_PIN, local_threshold="0.32"))
    state = layers.closed()
    assert "local_classifier" not in state, "the named offline rung is a configured layer"
    unpinned = build_layers(GuardrailSettings(local_model="sentence-transformers/all-MiniLM-L6-v2"))
    assert "local_classifier" in unpinned.closed()
    assert "names no pinned artifact" in unpinned.closed()["local_classifier"]
