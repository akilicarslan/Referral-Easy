"""Shared path setup, corpus accessors and layer doubles for the guardrail tests.

A plain module with a unique name, not ``conftest.py``, for the reason
``tests/skills/skills_fixtures.py`` sets out at length: pytest puts every ``conftest.py`` onto
``sys.path`` under the bare name ``conftest``, so ``from conftest import ...`` resolves to whichever
directory pytest imported last, and ``tests/test_test_layout.py`` fails the suite when it sees one.
Import the names below from here.

The ``sys.path`` bridge
-----------------------
``packages/guardrails`` is not yet a member of ``[tool.uv.workspace]`` in the root
``pyproject.toml``, so ``referraleasy_guardrails`` is not installed into the shared virtualenv. The
bridge adds the directory that *contains* the import package and nothing else, and only when it is
present: **two lines in the root ``pyproject.toml`` plus one in the ``Makefile``'s
``PYTHON_SOURCE_ROOTS`` remove the need for it** (ADR-0038 records that it is outstanding).
``referraleasy_contracts`` is bridged the same way, because this package imports the contract enums
at module scope and a checkout that has not been synced would otherwise fail on import.

What is here and why
--------------------
Every double below exists to make a *rule* observable that a provider or an artifact would otherwise
hide:

* :class:`ScriptedDecisionModelClient` — layer 2's own logic (masking, the closed vocabulary,
  fail-closed parsing) with no network. It records what it was asked, so "the provider never saw the
  identifier" is asserted rather than assumed.
* :class:`TickClock` — a clock that advances by a fixed step per reading, so a latency assertion is
  not a race and the pipeline's per-layer timing is exactly reproducible.
* :class:`VectorEncoder` / :func:`synthetic_reference_set` — layer 3's decision rule with arithmetic
  a test can do in its head, so the uncertainty band is exercised at chosen similarities rather than
  wherever a real encoder happened to land.
* :class:`StubOracle` and :func:`card` — the output guardrail's checks over cards whose every field a
  test chose.
* :class:`SpyLayer` — an orchestrator-side layer whose answer and configuration are fixed, which is
  how the pipeline's ordering, short-circuit and closed-layer rules are tested without any real layer
  being able to influence the result.

Nothing here reaches a provider, a database or the network, and no test may: ``MODEL_BACKEND`` is
``live`` in this checkout's ``.env``, so a test that built a client from the environment would spend
money. Layer 2's live path is a deselected ``@pytest.mark.spike`` and nothing else.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[2]
_GUARDRAIL_SRC = REPO_ROOT / "packages" / "guardrails" / "src"
_CONTRACTS_SRC = REPO_ROOT / "packages" / "contracts" / "python" / "src"
for _candidate in (_CONTRACTS_SRC, _GUARDRAIL_SRC):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from referraleasy_contracts.generated.common import (  # noqa: E402  (after the bridge)
    GuardrailCategory,
    GuardrailDecision,
    GuardrailLayer,
)
from referraleasy_contracts.generated.sse import (  # noqa: E402
    ComponentInstance,
    RenderableComponent,
)
from referraleasy_guardrails.corpus import (  # noqa: E402
    CorpusCase,
    LabelledCorpus,
    Reference,
    ReferenceSet,
    canonical_hash,
)
from referraleasy_guardrails.layer_classifier import (  # noqa: E402
    HASHED_METHOD,
    ThresholdRecord,
)
from referraleasy_guardrails.layer_decision_model import (  # noqa: E402
    DecisionModelError,
    ModelResponse,
)
from referraleasy_guardrails.types import (  # noqa: E402
    LayerVerdict,
    ScreeningRequest,
)

# ---------------------------------------------------------------------------
# The labelled corpus and the reference utterances that ship with the package
# ---------------------------------------------------------------------------

_CORPUS: LabelledCorpus | None = None
_REFERENCES: ReferenceSet | None = None


def corpus() -> LabelledCorpus:
    """The committed labelled corpus, loaded once. The same file the measurement CLI reads."""
    global _CORPUS
    if _CORPUS is None:
        _CORPUS = LabelledCorpus.load()
    return _CORPUS


def references() -> ReferenceSet:
    """The committed reference utterances, loaded once."""
    global _REFERENCES
    if _REFERENCES is None:
        _REFERENCES = ReferenceSet.load()
    return _REFERENCES


def case(case_id: str) -> CorpusCase:
    """One case by id, failing loudly rather than returning None for a renamed fixture."""
    for entry in corpus().cases:
        if entry.case_id == case_id:
            return entry
    known = ", ".join(entry.case_id for entry in corpus().cases)
    raise KeyError(f"no corpus case {case_id!r}; the corpus has: {known}")


def request_for(
    text: str,
    *,
    workflow_state: str = "INTAKE",
    short_reply_expected: bool = False,
    open_question: str | None = None,
    synthetic: bool = True,
) -> ScreeningRequest:
    """A request built the way the workflow builds one, with the context §9 says a layer needs."""
    return ScreeningRequest(
        text=text,
        workflow_state=workflow_state,
        short_reply_expected=short_reply_expected,
        open_question=open_question,
        synthetic=synthetic,
    )


# ---------------------------------------------------------------------------
# Layer 2: a scripted client
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reply:
    """One scripted model reply, with what the provider would have reported about it.

    The usage fields default to ``None``, which is the provider *not reporting* them — the fact the
    layer must keep distinct from a reported zero.
    """

    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    raises: Exception | None = None


def json_reply(
    decision: str = "allow",
    category: str = "benign",
    reason: str = "an ordinary message",
) -> Reply:
    """A well-formed reply from the model, in the shape the prompt asks for."""
    return Reply(
        text=f'{{"decision": "{decision}", "category": "{category}", "reason": "{reason}"}}'
    )


@dataclass
class ScriptedDecisionModelClient:
    """A decision-model client that answers from a list, and remembers every call.

    ``replies`` is consumed in order; when it runs out the client answers ``clarify/off_topic`` — an
    answer that is neither permission nor a refusal — so a test that makes more calls than it
    scripted gets a visible, safe outcome rather than an exception three frames away.
    """

    replies: list[Reply] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(self, *, system: str, user: str, model_id: str, timeout_ms: int) -> ModelResponse:
        self.calls.append(
            {"system": system, "user": user, "model_id": model_id, "timeout_ms": timeout_ms}
        )
        reply = (
            self.replies.pop(0)
            if self.replies
            else json_reply("clarify", "off_topic", "unscripted")
        )
        if reply.raises is not None:
            raise reply.raises
        return ModelResponse(
            text=reply.text,
            model_id=model_id,
            latency_ms=0,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            cost_usd=reply.cost_usd,
        )

    @property
    def users(self) -> tuple[str, ...]:
        """The user prompts the layer built, in order. What the provider would actually receive."""
        return tuple(str(call["user"]) for call in self.calls)

    def transport_error(self) -> DecisionModelError:
        """The exception the real client raises for a failed call, so a script can raise it too."""
        return DecisionModelError("the decision-model call failed: MockTransportError")


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------


@dataclass
class TickClock:
    """A monotonic clock that advances by a fixed step on every reading.

    Deterministic latency without being zero: the pipeline reads the clock before and after each
    layer, so a layer costs exactly ``step_ms`` in its verdict. A test that asserted on a real clock
    would be asserting on the machine's mood.
    """

    step_ms: float = 1.0
    now_ms: float = 0.0
    reads: int = 0

    def monotonic_ms(self) -> float:
        self.reads += 1
        current = self.now_ms
        self.now_ms += self.step_ms
        return current


# ---------------------------------------------------------------------------
# Layer 3: encoders, references and records a test can do arithmetic on
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VectorEncoder:
    """An encoder that returns a chosen vector per text, so layer 3's rule is testable exactly.

    This is a test double and cannot be mistaken for a model: it has no training, no artifact and no
    claim to be semantic. Its whole job is to place a message at a chosen cosine similarity to a
    reference so the threshold, the band and the block rule are exercised at known numbers.
    """

    vectors: Mapping[str, tuple[float, ...]]
    default: tuple[float, ...] = (0.0, 1.0)
    method: str = "test-vector-v1"
    dimensions: int = 2
    artifact_hash: str | None = None

    def encode(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self.vectors.get(text, self.default) for text in texts)


#: The attack and benign reference texts :func:`synthetic_reference_set` builds by default. One
#: vector each, chosen so ``(1, 0)`` is identical to the attack reference and ``(0, 1)`` to the
#: benign one; anything between them lands in the band.
ATTACK_TEXT: Final = "ATTACK-REFERENCE-TEXT"
BENIGN_TEXT: Final = "BENIGN-REFERENCE-TEXT"
ATTACK_VECTOR: Final = (1.0, 0.0)
BENIGN_VECTOR: Final = (0.0, 1.0)


def synthetic_reference_set(
    attacks: Sequence[tuple[str, GuardrailCategory]] | None = None,
    benign: Sequence[str] | None = None,
) -> ReferenceSet:
    """A two-sided reference set with a real content hash, so a record can be matched against it.

    The hash is computed the same way :meth:`ReferenceSet.load` computes it, which is what makes
    ``ThresholdRecord.mismatch`` meaningful here: a test can build a record that *does* speak for this
    set and then edit one reference to show that it stops speaking for it.
    """
    attack_entries = (
        list(attacks)
        if attacks is not None
        else [(ATTACK_TEXT, GuardrailCategory.prompt_injection)]
    )
    benign_texts = list(benign) if benign is not None else [BENIGN_TEXT]
    attack_references = tuple(
        Reference(ref_id=f"atk-{index}", language="en", text=text, category=category)
        for index, (text, category) in enumerate(attack_entries)
    )
    benign_references = tuple(
        Reference(ref_id=f"ben-{index}", language="en", text=text)
        for index, text in enumerate(benign_texts)
    )
    return ReferenceSet(
        version="test-references-v1",
        attacks=attack_references,
        benign=benign_references,
        content_hash=canonical_hash(
            {
                "attacks": [_reference_dict(item) for item in attack_references],
                "benign": [_reference_dict(item) for item in benign_references],
            }
        ),
    )


def _reference_dict(reference: Reference) -> Mapping[str, Any]:
    """The field set ``canonical_hash`` is taken over at load time, reproduced here."""
    return {
        "ref_id": reference.ref_id,
        "language": reference.language,
        "text": reference.text,
        "category": reference.category,
    }


def synthetic_corpus(
    cases: Sequence[CorpusCase], *, version: str = "test-corpus-v1"
) -> LabelledCorpus:
    """A corpus built in memory, hashed the way :meth:`LabelledCorpus.load` hashes a file."""
    return LabelledCorpus(
        version=version,
        cases=tuple(cases),
        content_hash=canonical_hash([_case_dict(entry) for entry in cases]),
    )


def _case_dict(entry: CorpusCase) -> Mapping[str, Any]:
    return {
        "case_id": entry.case_id,
        "channel": entry.channel,
        "kind": entry.kind,
        "expected": entry.expected.value,
        "language": entry.language,
        "partition": entry.partition,
        "workflow_state": entry.workflow_state,
        "text": entry.text,
        "note": entry.note,
        "short_reply_expected": entry.short_reply_expected,
        "open_question": entry.open_question,
        "expected_category": entry.expected_category.value if entry.expected_category else None,
    }


def threshold_record(
    *,
    method: str = HASHED_METHOD,
    threshold: float = 0.5,
    margin: float = 0.08,
    labelled: LabelledCorpus | None = None,
    reference_set: ReferenceSet | None = None,
    artifact_hash: str = "",
    calibration: Mapping[str, Any] | None = None,
    held_out: Mapping[str, Any] | None = None,
) -> ThresholdRecord:
    """A threshold with its measurement attached, speaking for the data passed in.

    A record whose hashes were invented would be refused by ``mismatch``, which is the rule being
    tested — so the defaults here are the *real* corpus and reference set, and a test that wants a
    mismatch edits one field rather than starting from a broken record.
    """
    resolved_corpus = labelled or corpus()
    resolved_references = reference_set or references()
    return ThresholdRecord(
        method=method,
        encoder="VectorEncoder(dimensions=2)",
        threshold=threshold,
        margin=margin,
        corpus_version=resolved_corpus.version,
        corpus_hash=resolved_corpus.content_hash,
        references_version=resolved_references.version,
        references_hash=resolved_references.content_hash,
        selected_on_partition="calibration",
        target_false_positive_rate=0.05,
        calibration=dict(calibration or {"cases": 10, "benign_cases": 6, "attack_cases": 4}),
        held_out=dict(held_out or {"cases": 10, "benign_cases": 6, "attack_cases": 4}),
        artifact_hash=artifact_hash,
        notes="a test record: the arithmetic is the test's, the hash rule is the package's",
        measured_at="2026-10-15T00:00:00+00:00",
        measured_latency_ms={"corpus_mean": 0.0, "corpus_max": 0.0, "corpus_cases": 0},
    )


# ---------------------------------------------------------------------------
# The orchestrator's view: fixed layers
# ---------------------------------------------------------------------------


def verdict(
    layer: GuardrailLayer,
    decision: GuardrailDecision = GuardrailDecision.allow,
    category: GuardrailCategory = GuardrailCategory.benign,
    *,
    reason: str = "scripted verdict",
    response: str | None = None,
    decisive: bool = False,
    method: str = "scripted",
    similarity: float | None = None,
) -> LayerVerdict:
    """A verdict with everything a test wants to control, and nothing it does not."""
    return LayerVerdict(
        layer=layer,
        decision=decision,
        category=category,
        reason=reason,
        response=response,
        decisive=decisive,
        method=method,
        similarity=similarity,
    )


@dataclass
class SpyLayer:
    """A layer whose configuration and answer are fixed, recording the calls it received.

    ``closed_reason`` is what makes this double worth having twice: a caller can build a layer that is
    *not configured* and see what the orchestrator does with it, which is §9's closed-dependency rule.
    """

    name: GuardrailLayer
    answer: LayerVerdict | None = None
    closed_reason: str = ""
    calls: list[tuple[ScreeningRequest, str]] = field(default_factory=list)
    raises: Exception | None = None

    @property
    def reason_if_unconfigured(self) -> str:
        return self.closed_reason or "this scripted layer has nothing behind it"

    def is_configured(self) -> bool:
        return self.answer is not None and not self.closed_reason

    def screen(self, request: ScreeningRequest, masked_text: str) -> LayerVerdict:
        self.calls.append((request, masked_text))
        if self.raises is not None:
            raise self.raises
        if self.answer is None:
            from referraleasy_guardrails.types import raise_unavailable

            raise_unavailable(self.name, self.reason_if_unconfigured)
        assert self.answer.layer is self.name  # a layer that answered as another layer is a bug
        return self.answer


def spy_layers(
    *,
    regex: LayerVerdict | None = None,
    decision_model: LayerVerdict | None = None,
    local_classifier: LayerVerdict | None = None,
    closed: Iterable[GuardrailLayer] = (),
    closed_reason: str = "the operator did not provide this layer",
) -> tuple[SpyLayer, ...]:
    """Three scripted layers in §9's order, with the named ones closed and the rest answering."""
    closed_names = set(closed)
    built: list[SpyLayer] = []
    for layer, answer in (
        (GuardrailLayer.regex, regex),
        (GuardrailLayer.decision_model, decision_model),
        (GuardrailLayer.local_classifier, local_classifier),
    ):
        if layer in closed_names:
            built.append(SpyLayer(layer, answer=None, closed_reason=closed_reason))
        else:
            built.append(
                SpyLayer(layer, answer=answer or verdict(layer, reason=f"{layer.value} allow"))
            )
    return tuple(built)


# ---------------------------------------------------------------------------
# The output guardrail's sources
# ---------------------------------------------------------------------------


@dataclass
class StubOracle:
    """The three records the output guardrail may consult, as dictionaries.

    Three methods and three dicts, because the port is deliberately three questions: an oracle that
    could answer anything would let the check grow a query per field, and then the guardrail would be
    coupled to whatever schema the caller happens to have.
    """

    entities: set[str] = field(default_factory=set)
    slots: dict[str, str] = field(default_factory=dict)
    bookings: dict[str, str] = field(default_factory=dict)
    asked: list[tuple[str, str]] = field(default_factory=list)

    def entity_exists(self, entity_id: str) -> bool:
        self.asked.append(("entity_exists", entity_id))
        return entity_id in self.entities

    def slot_time(self, slot_id: str) -> str | None:
        self.asked.append(("slot_time", slot_id))
        return self.slots.get(slot_id)

    def booking_state(self, booking_id: str) -> str | None:
        self.asked.append(("booking_state", booking_id))
        return self.bookings.get(booking_id)


#: A valid v4 uuid for ``ComponentInstance.instance_id``, fixed so cards are byte-identical per run.
INSTANCE_ID: Final = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def card(
    props: Mapping[str, Any] | None = None,
    *,
    component: RenderableComponent = RenderableComponent.SlotPicker,
    instance_id: str = INSTANCE_ID,
    **overrides: Any,
) -> Mapping[str, Any]:
    """A component payload in the contract's envelope, ready for the output guardrail.

    Built as a plain mapping rather than as a model so a test can also build the *invalid* envelopes
    the schema check exists to refuse.
    """
    payload: dict[str, Any] = {
        "instance_id": instance_id,
        "component": component.value,
        "props": dict(props or {}),
        "workflow_state_version": 1,
    }
    payload.update(overrides)
    return payload


def validated_card(**kwargs: Any) -> ComponentInstance:
    """The same card, validated into the contract's model — the "this should pass" input."""
    return ComponentInstance.model_validate(card(**kwargs))
