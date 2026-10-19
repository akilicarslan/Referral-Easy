# ruff: noqa: RUF001  # §9's Turkish sentences are quoted verbatim
"""The seam the package was written for: the gateway's own pipeline driving these three layers (§9).

Both sides of the seam exist in this checkout. The gateway (`services/gateway/`) ships a
``GuardrailPipeline`` that §9's ordering, short-circuit and closed-dependency rules are stated in, and
a ``DecisionModelGuardrail`` / ``LocalClassifierGuardrail`` pair that takes a ``screen`` callable and
treats ``None`` as "not configured". This package (`packages/guardrails/`) ships the three layers that
callable would be. The seam between them is **structural, not nominal**: the gateway's pipeline reads
``layer.name``, calls ``layer.screen(request, masked_text)``, and copies ``layer / decision / category
/ reason / response`` off whatever comes back — which is why this file can hand it layers it has never
met and assert on the result.

What is asserted here, and why each one is worth a test:

* **The gateway's pipeline drives these layers to the same decisions** as this package's own pipeline
  over the corpus. Two orchestrators that disagree about what a message was refused for are two
  products, and the demo would be showing one while the gateway served the other.
* **Masking is done once, by the orchestrator, and the provider never sees an identifier.** Layer 2
  refuses to send a message that still carries one, so this is not an intention: it is a behaviour of
  both paths, exercised here through the gateway's pipeline.
* **A request built by the gateway works.** The gateway's ``ScreeningRequest`` carries four facts and
  no ``open_question``; the package's carries five. The layers read the fifth defensively
  (``getattr``), and the test pins the difference rather than pretending it does not exist.
* **The refusal is the class the calling pipeline catches.** §9's closed dependency becomes a ``503``
  only if the exception that leaves the layer is the one the pipeline has an ``except`` clause for.
  ``raise_unavailable`` resolves that at runtime and this file proves the resolution lands.
* **The package does not depend on the service.** One function knows the gateway exists, its import
  is inside the function, and a subprocess asserts that importing the package imports no service.
* **The places the two pipelines used to differ, and no longer do.** This file once pinned two
  divergences as facts: the gateway left ``decision`` at ``allow`` when a layer was unavailable, and it
  quoted the last verdict of any kind as the reason for a decision taken from the last *non-allow* one.
  ADR-0038 recorded both and said the wiring must adopt this package's rules; it has, and the tests
  below now assert the *agreement* while keeping the history in their docstrings. The assertions stayed
  because the failure mode is invisible from the outside — the write path refused in both readings — so
  a future regression would not announce itself through a booking test.

``test_guardrail_layer1_parity.py`` is gone, as its own docstring said it would be: it existed to make
the interval between this package existing and the gateway using it safe, by comparing the gateway's
layer-1 copy against this one verdict for verdict. The copy is deleted with the wiring (ADR-0038), so
there is no second implementation left to compare against.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from guardrail_fixtures import (
    REPO_ROOT,
    ScriptedDecisionModelClient,
    corpus,
    json_reply,
    spy_layers,
    verdict,
)

from referraleasy_contracts.generated.common import (
    GuardrailCategory,
    GuardrailDecision,
    GuardrailLayer,
)
from referraleasy_gateway import guardrails as gateway
from referraleasy_gateway.errors import GuardrailUnavailableError
from referraleasy_gateway.timing import ManualClock
from referraleasy_guardrails.build import build_layers
from referraleasy_guardrails.config import GuardrailSettings
from referraleasy_guardrails.layer_classifier import OFFLINE_METHOD_PIN, HashedReferenceEncoder
from referraleasy_guardrails.pipeline import GuardrailPipeline as PackagePipeline
from referraleasy_guardrails.types import (
    LAYER_ORDER,
    ScreeningRequest,
    is_unavailable,
    mask_pii,
    raise_unavailable,
)
from referraleasy_guardrails.types import GuardrailUnavailable as PackageUnavailable

#: The package's source tree, for the two layering assertions at the end of this file.
PACKAGE_SRC = REPO_ROOT / "packages" / "guardrails" / "src" / "referraleasy_guardrails"

#: A message with both kinds of identifier the masker exists for: a seven-digit member id (the floor
#: the two maskers disagree about) and an email (the one they agree on).
SENSITIVE = "my member id is 2205518 and my email is hasta@example.com"


def configured_layers():
    """The three real layers, with a scripted model and the deterministic encoder behind them."""
    return build_layers(
        GuardrailSettings(
            decision_model="openai/gpt-5-nano",
            local_model=OFFLINE_METHOD_PIN,
            local_threshold="0.32",
        ),
        client=ScriptedDecisionModelClient(replies=[json_reply() for _ in range(80)]),
        encoder=HashedReferenceEncoder(),
    )


def gateway_pipeline(*, masker=None, layers=None):
    """The gateway's pipeline, running the layers handed to it, on a clock a test controls."""
    resolved = layers or configured_layers()
    return gateway.GuardrailPipeline(
        layers=resolved.ordered,
        clock=ManualClock(read_cost_ms=5.0),
        masker=masker or mask_pii,
    ), resolved


def context_of(entry) -> dict[str, object]:
    """The facts both pipelines can be told.

    ``open_question`` is deliberately absent: the gateway's request cannot carry it, so passing it to
    only one of the two pipelines would compare different inputs rather than different orchestrators.
    """
    return {
        "workflow_state": entry.workflow_state,
        "short_reply_expected": entry.short_reply_expected,
    }


# ---------------------------------------------------------------------------
# The order, and the two pipelines' identity of purpose
# ---------------------------------------------------------------------------


def test_both_pipelines_declare_step_9_s_order_and_refuse_anything_else() -> None:
    assert tuple(gateway.LAYER_ORDER) == LAYER_ORDER
    assert [layer.value for layer in LAYER_ORDER] == [
        "regex",
        "decision_model",
        "local_classifier",
    ]

    layers = configured_layers()
    swapped = (layers.regex, layers.local_classifier, layers.decision_model)
    for refused in (
        lambda: gateway.GuardrailPipeline(layers=swapped, clock=ManualClock()),
        lambda: PackagePipeline(layers=swapped),
    ):
        with pytest.raises(ValueError, match="must run in") as raised:
            refused()
        assert all(
            name in str(raised.value) for name in ("regex", "decision_model", "local_classifier")
        )


def test_the_gateway_s_pipeline_drives_these_layers_to_the_same_decisions() -> None:
    """The corpus, screened twice: once by this package's pipeline, once by the gateway's."""
    entries = corpus().user_messages()
    assert len(entries) >= 30, "the comparison is over the committed corpus, not a sample of it"

    package_pipeline = configured_layers().pipeline()
    gateway_run, _ = gateway_pipeline()
    decisions: dict[GuardrailDecision, int] = {}
    for entry in entries:
        mine = package_pipeline.screen(entry.text, **context_of(entry))
        theirs = gateway_run.screen(entry.text, **context_of(entry))
        assert theirs.decision is mine.decision, entry.case_id
        assert theirs.complete is mine.complete, entry.case_id
        assert theirs.response == mine.response, entry.case_id
        if mine.decision is GuardrailDecision.block:
            # A block ends the run in both pipelines, so there is one verdict to quote and no room
            # for the two to explain themselves differently.
            assert theirs.category is mine.category, entry.case_id
            assert theirs.reason == mine.reason, entry.case_id
        decisions[mine.decision] = decisions.get(mine.decision, 0) + 1

    assert len(decisions) >= 2, f"the corpus screened to a single decision: {decisions}"
    assert decisions.get(GuardrailDecision.block, 0) >= 1, "no refusal in the corpus comparison"


def test_the_two_orchestrators_agree_on_which_layer_a_clarify_quotes() -> None:
    """The decision, its category, its reason and its response come from one verdict — on both sides.

    This test used to pin a *disagreement*, and the history is worth keeping because the defect was
    invisible from the outside: the same three verdicts, both pipelines deciding ``clarify`` and both
    complete, but only this package quoted the layer that actually decided. The gateway assigned the
    decision from the last non-allow verdict while taking the **reason from the last verdict of any
    kind** — here a layer that allowed — so its sentence explained a question using the words of a
    layer that had no objection, which is exactly the failure the package's own comment warns about
    ("a ``clarify`` quoting the reason of a layer that answered ``allow`` would send an operator
    looking at the wrong layer").

    ADR-0038 recorded it as a divergence and said the wiring must adopt this package's rule. It has.
    The two now agree, and this asserts the agreement at the level of the sentence an operator reads
    rather than at the level of the enum, because the enum was never the part that was wrong.
    """
    first = verdict(
        GuardrailLayer.regex,
        GuardrailDecision.clarify,
        GuardrailCategory.off_topic,
        reason="the first question",
    )
    second = verdict(
        GuardrailLayer.decision_model,
        GuardrailDecision.clarify,
        GuardrailCategory.malformed_input,
        reason="the second question",
    )
    layers = spy_layers(regex=first, decision_model=second)

    mine = PackagePipeline(layers=layers).screen("evet", workflow_state="INTAKE")
    theirs = gateway.GuardrailPipeline(layers=layers, clock=ManualClock()).screen(
        "evet", workflow_state="INTAKE"
    )

    assert mine.decision is theirs.decision is GuardrailDecision.clarify
    assert mine.complete is theirs.complete is True
    assert mine.category is theirs.category is GuardrailCategory.off_topic, (
        "both take the category from the verdict that decided, not from the last non-allow one"
    )
    assert mine.reason == theirs.reason == "the first question", (
        "the gateway's outcome explained a clarify with the reason of a layer that allowed"
    )


# ---------------------------------------------------------------------------
# Masking, once, by the orchestrator
# ---------------------------------------------------------------------------


def test_through_the_gateway_s_pipeline_the_provider_receives_a_masked_message_only() -> None:
    client = ScriptedDecisionModelClient(replies=[json_reply()])
    layers = build_layers(
        GuardrailSettings(
            decision_model="openai/gpt-5-nano",
            local_model=OFFLINE_METHOD_PIN,
            local_threshold="0.32",
        ),
        client=client,
        encoder=HashedReferenceEncoder(),
    )
    pipeline, _ = gateway_pipeline(layers=layers)

    outcome = pipeline.screen(SENSITIVE, workflow_state="INTAKE")

    assert outcome.complete is True, "the refusal would be about the masking, not the deployment"
    assert len(client.calls) == 1, "layer 2 is called once, with the masked message"
    prompt = client.users[0]
    assert "hasta@example.com" not in prompt
    assert "2205518" not in prompt
    assert "[email]" in prompt and "[long_digits]" in prompt
    assert "<message>" in prompt and "</message>" in prompt


def test_there_is_one_masker_and_it_removes_the_seven_digit_id() -> None:
    """One implementation, one floor, so the two sides cannot disagree about what an identifier is.

    This test used to pin the opposite: the gateway's `mask_pii` used a nine-digit floor while this
    package used seven, so a seven-digit member id — the length the directory actually hands out —
    survived the gateway's masker and layer 2 refused to send the message. Nothing leaked either way,
    because the layer refuses rather than transmits, but the *outcome* differed, which is the worst
    kind of difference: a deployment whose screening silently cannot complete.

    The wiring adopted one masker (ADR-0038), and it is the same object rather than two matching
    implementations — `is` rather than `==`, because two implementations that agree today are the
    thing that drifted in the first place.
    """
    assert gateway.mask_pii is mask_pii, (
        "the gateway imports the package's masker, it does not copy it"
    )
    assert mask_pii(SENSITIVE) == "my member id is [long_digits] and my email is [email]"

    client = ScriptedDecisionModelClient(replies=[json_reply()])
    layers = build_layers(
        GuardrailSettings(
            decision_model="openai/gpt-5-nano",
            local_model=OFFLINE_METHOD_PIN,
            local_threshold="0.32",
        ),
        client=client,
        encoder=HashedReferenceEncoder(),
    )
    # The pipeline's own default masker, which is what the running service uses — passing it
    # explicitly would let this test pass while `from_settings` shipped a different one.
    pipeline, _ = gateway_pipeline(layers=layers)

    outcome = pipeline.screen(SENSITIVE, workflow_state="INTAKE")

    assert outcome.complete is True, (
        "the shared masker removed the id, so layer 2 had nothing to refuse"
    )
    assert len(client.calls) == 1, "the provider was called once, with the masked message"
    assert "2205518" not in client.users[0]
    assert "[long_digits]" in client.users[0]
    # Nothing to report as unavailable, which is the whole point: the layer that used to refuse the
    # message over a leftover identifier now has no leftover identifier to refuse.
    assert outcome.unavailable == ()


# ---------------------------------------------------------------------------
# The request the gateway builds
# ---------------------------------------------------------------------------


def test_a_request_built_without_an_open_question_reads_as_none_and_the_layers_use_the_rest() -> (
    None
):
    """The fifth field is additive, and a gateway-built request still works without it.

    This test used to assert the field was *absent* from the gateway's request — a difference it
    pinned rather than hid. The wiring removed the difference: the gateway now builds this package's
    ``ScreeningRequest``, so the only question left is the one that actually matters, which is whether
    a request that leaves ``open_question`` unset is still readable. It is, and the four facts it does
    carry are used.
    """
    theirs = gateway.ScreeningRequest(
        text="evet", workflow_state="SCHEDULING", short_reply_expected=True
    )
    assert theirs.open_question is None, "unset, not absent: the layers read it with `getattr`"
    # The request the gateway builds now *is* this package's, `masked_text` included — the old
    # assertion here was that the field was absent, which was true of the copy the wiring deleted.
    assert type(theirs) is ScreeningRequest, "one request type, so the two sides cannot drift"
    assert theirs.masked_text, (
        "the package's own convenience for its tests, and the gateway inherits it"
    )

    client = ScriptedDecisionModelClient(replies=[json_reply()])
    layers = build_layers(
        GuardrailSettings(decision_model="openai/gpt-5-nano"),
        client=client,
    )
    answer = layers.decision_model.screen(theirs, "evet")
    assert answer.decision is GuardrailDecision.allow
    assert "open_question: (none)" in client.users[0]
    assert "question_open: yes" in client.users[0], "the four facts it does carry are used"

    mine = ScreeningRequest(
        text="evet",
        workflow_state="SCHEDULING",
        short_reply_expected=True,
        open_question="Bir randevu saati seçelim mi?",
    )
    layers.decision_model.screen(mine, "evet")
    assert "open_question: Bir randevu saati seçelim mi?" in client.users[1]
    assert "(none)" not in client.users[1], "the field is additive, not ignored"


# ---------------------------------------------------------------------------
# The wrapper seam the wiring actually uses
# ---------------------------------------------------------------------------


def test_the_layer_set_s_callables_are_what_the_gateway_s_wrappers_take() -> None:
    """``LayerSet.screens()`` returns exactly the pair of ``screen | None`` the wrappers declare."""
    decision_screen, classifier_screen = configured_layers().screens()
    wrappers = (
        gateway.RegexGuardrail(max_chars=2000),
        gateway.DecisionModelGuardrail("openai/gpt-5-nano", screen=decision_screen),
        gateway.LocalClassifierGuardrail(OFFLINE_METHOD_PIN, screen=classifier_screen),
    )
    pipeline = gateway.GuardrailPipeline(layers=wrappers, clock=ManualClock())
    assert pipeline.configured is True

    outcome = pipeline.screen(
        "merhaba, yarın için bir randevu almak istiyorum", workflow_state="INTAKE"
    )
    assert outcome.complete is True
    assert [item.layer for item in outcome.verdicts] == list(LAYER_ORDER)
    assert outcome.decision is GuardrailDecision.allow, (
        "the scripted model allows and the hashed classifier does not recognise the message as an "
        "attack, so all three layers permitted it"
    )


def test_a_closed_layer_set_hands_the_wrappers_none_and_both_pipelines_ask_instead_of_allowing() -> (
    None
):
    """The same three wrapper objects, two orchestrators, one reading of one closure.

    The second difference between the pipelines, and the more consequential one — and the one ADR-0038
    called "the single most important integration item". The gateway's pipeline used to record an
    unavailable layer and leave ``decision`` at the ``allow`` it started with, so a caller reading
    ``outcome.decision`` alone read *permission* where nothing had been screened. ``require_complete``
    still refused the write, which is why no booking test ever caught it and why the defect was worth
    an ADR of its own: the guarantee held for the write path and nowhere else.

    The wiring adopted this package's rule. Both pipelines now turn a closed dependency into a
    ``clarify`` — not a ``block``, because nobody decided — and both still refuse the write.
    """
    closed = build_layers(GuardrailSettings())
    assert closed.screens() == (None, None)
    wrappers = (
        gateway.RegexGuardrail(max_chars=2000),
        gateway.DecisionModelGuardrail("", screen=closed.screens()[0]),
        gateway.LocalClassifierGuardrail("", screen=closed.screens()[1]),
    )
    pipeline = gateway.GuardrailPipeline(layers=wrappers, clock=ManualClock())
    assert pipeline.configured is False

    message = "merhaba, yarın için bir randevu almak istiyorum"
    outcome = pipeline.screen(message, workflow_state="INTAKE")
    assert outcome.complete is False
    assert outcome.decision is GuardrailDecision.clarify, (
        "the gateway's pipeline reported an allow with unavailable layers; a caller reading only "
        "`decision` would have read it as permission"
    )
    with pytest.raises(GuardrailUnavailableError) as raised:
        pipeline.require_complete(outcome)
    assert raised.value.detail == {"unavailable_layers": ["decision_model", "local_classifier"]}
    assert "A static help card is still available" in str(raised.value)

    mine = PackagePipeline(layers=wrappers).screen(message, workflow_state="INTAKE")
    assert mine.complete is False
    assert mine.decision is outcome.decision, (
        "the same three layers, the same closure, and the two orchestrators now read it identically"
    )


def test_the_gateway_s_layer_report_reads_the_verdicts_these_layers_produced() -> None:
    client = ScriptedDecisionModelClient(replies=[json_reply() for _ in range(4)])
    layers = build_layers(
        GuardrailSettings(
            decision_model="openai/gpt-5-nano",
            local_model=OFFLINE_METHOD_PIN,
            local_threshold="0.32",
        ),
        client=client,
        encoder=HashedReferenceEncoder(),
    )
    pipeline, _ = gateway_pipeline(layers=layers)
    for text in ("merhaba", "randevu almak istiyorum", "salı günü olur mu", "teşekkürler"):
        pipeline.screen(text, workflow_state="INTAKE")

    report = gateway.layer_report(pipeline.outcomes_seen)
    assert report["invocations"] == 4
    by_layer = report["by_layer"]
    assert set(by_layer) == {layer.value for layer in LAYER_ORDER}
    assert [by_layer[name]["verdicts"] for name in by_layer] == [4, 4, 4]
    assert all(entry["mean_latency_ms"] == 5.0 for entry in by_layer.values()), (
        "the gateway's clock prices each layer, and the package's layers carry their own; the "
        "orchestrator's number is the one reported"
    )
    assert report["synthetic"] is True


# ---------------------------------------------------------------------------
# The exception, and the dependency direction
# ---------------------------------------------------------------------------


def test_the_closure_raised_here_is_the_class_the_gateway_s_pipeline_catches() -> None:
    with pytest.raises(gateway.GuardrailUnavailable) as raised:
        raise_unavailable(GuardrailLayer.local_classifier, "no artifact named")
    error = raised.value
    assert type(error) is gateway.GuardrailUnavailable, (
        "with the gateway importable, the exception leaving a layer is the gateway's own class — an "
        "equivalent-but-different one would surface as an unhandled 500 instead of §9's 503"
    )
    assert error.layer is GuardrailLayer.local_classifier
    assert error.reason == "no artifact named"
    assert is_unavailable(error) is True

    assert is_unavailable(PackageUnavailable(GuardrailLayer.regex, "no pattern engine")) is True
    assert is_unavailable(ValueError("a genuine bug, not a closure")) is False

    # And the pipeline that owns the write path catches it, rather than letting it escape.
    layers = build_layers(GuardrailSettings())
    pipeline, _ = gateway_pipeline(layers=layers)
    outcome = pipeline.screen("merhaba", workflow_state="INTAKE")
    assert [error.layer for error in outcome.unavailable] == [
        GuardrailLayer.decision_model,
        GuardrailLayer.local_classifier,
    ]


def test_only_one_function_in_the_package_knows_the_service_exists() -> None:
    """The dependency direction is ``services → packages``, enforced rather than documented."""
    hits: dict[str, list[int]] = {}
    for path in sorted(PACKAGE_SRC.rglob("*.py")):
        lines = [
            number
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if "referraleasy_gateway" in line
        ]
        if lines:
            hits[path.name] = lines
    assert set(hits) == {"types.py"}, hits
    indented = [
        line
        for line in (PACKAGE_SRC / "types.py").read_text(encoding="utf-8").splitlines()
        if "referraleasy_gateway" in line
    ]
    assert all(line.startswith((" ", "\t")) for line in indented), (
        "every mention is inside a function body: a module-scope import would make the package "
        "unimportable without the service"
    )
    assert len(indented) == 1, "one runtime import, in raise_unavailable, and nothing else"


def test_importing_the_package_does_not_import_the_service(tmp_path: Path) -> None:
    """The subprocess is the assertion: in-process, the gateway is already imported by other tests."""
    code = (
        "import sys, referraleasy_guardrails;"
        "print('service' if 'referraleasy_gateway' in sys.modules else 'clean')"
    )
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [
                str(REPO_ROOT / "packages" / "guardrails" / "src"),
                str(REPO_ROOT / "packages" / "contracts" / "python" / "src"),
            ]
        ),
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean"
