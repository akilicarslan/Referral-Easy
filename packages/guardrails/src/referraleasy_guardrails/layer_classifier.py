# ruff: noqa: RUF001, RUF002  # the §9 sentence about calibrated probability is quoted in Turkish
"""Layer 3 — the local small semantic classifier, with its method, artifact hash and thresholds (§9).

§9: *"Lokal küçük semantik model: CPU/ONNX classifier veya değerlendirilmiş encoder tabanlı
yaklaşım. Eğitim/yöntem, artifact hash ve eşikler kayıtlıdır. Belirsizlik veya çelişki clarify/HITL
üretir; model confidence kalibre olasılık sayılmaz."* Four requirements, and this module is built so
that each one is checkable rather than asserted:

**The method is recorded.** :class:`ThresholdRecord` carries the method id, the encoder identity, the
reference set's hash, the corpus hash, the sample size and class balance, the confusion matrix at the
chosen threshold, the false-positive rate, and the measured latency. A layer whose record is missing,
stale or for a different method is **closed** — it does not fall back to a threshold somebody liked.

**The artifact hash is recorded.** ADR-0005's intake rule, implemented: the bytes are verified against
the SHA-256 transcribed from the ADR before anything is loaded, and a mismatch refuses. Models are
never committed, so the hash is the contract and the download is ephemeral.

**Uncertainty and disagreement produce clarify.** The band around the threshold is not decoration:
inside it the layer declines to decide and the workflow asks a human, which is a different outcome
from allowing and a different outcome from refusing.

**A similarity score is not a calibrated probability.** This is the failure mode that turns a
demo into a lie: ``0.8`` is the cosine similarity between a message and one reference sentence. It is
not "80% likely an attack", it does not become a probability by being thresholded, and no field here
is named ``confidence`` or ``probability``. A test enforces that by AST rather than by review.

Two encoders ship:

``HashedReferenceEncoder``
    char n-grams (3–4) hashed into a fixed-width vector, sublinear term frequency, L2 normalised.
    Pure stdlib, deterministic, no artifact, ~microseconds per message. This is a **lexical** matcher:
    it finds attacks that resemble the references and misses the ones that do not, and the report
    says exactly that rather than calling it semantic.

``OnnxEncoder``
    ADR-0005's pinned CPU artifact — ``all-MiniLM-L6-v2``, quantised, hash-verified, tokenised with
    ``tokenizers`` and run through ``onnxruntime``. It is the intended layer-3 implementation. It
    needs an optional dependency and a 23 MB artifact, so in a checkout without them the encoder rung
    is *closed* rather than silently replaced by the lexical one: an operator selects the rung by
    naming it, and a report always states which rung produced a number.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from referraleasy_contracts.generated.common import (
    GuardrailCategory,
    GuardrailDecision,
    GuardrailLayer,
)
from referraleasy_guardrails.corpus import (
    ArtifactPin,
    Reference,
    ReferenceSet,
    sha256_file,
)
from referraleasy_guardrails.types import (
    LayerVerdict,
    ScreeningRequest,
    fold,
    raise_unavailable,
)

__all__ = [
    "OFFLINE_METHOD_PIN",
    "ArtifactHashMismatch",
    "Encoder",
    "EncoderUnavailable",
    "HashedReferenceEncoder",
    "LocalClassifierGuardrail",
    "OnnxEncoder",
    "ThresholdRecord",
    "cosine",
    "load_threshold_record",
    "select_threshold",
]

#: The environment value that selects the offline (lexical) rung, in the same variable that otherwise
#: names a pinned artifact. Naming the rung explicitly is the point: a silent fallback from the
#: encoder to a weaker matcher would put a different implementation's operating point under the
#: encoder's name in every report.
OFFLINE_METHOD_PIN: Final = "offline/hashed-char-ngram-v1"

#: Recorded with the lexical rung's numbers, and never with the encoder's.
HASHED_METHOD: Final = "hashed-char-ngram-v1"

#: The band width as a fraction of the threshold, used when a record does not carry one.
DEFAULT_MARGIN: Final = 0.08


class EncoderUnavailable(RuntimeError):
    """The encoder cannot run: no runtime, no artifact, or an artifact that will not load."""


class ArtifactHashMismatch(EncoderUnavailable):
    """The artifact's bytes are not the bytes the pin describes. Refusing is the whole point."""

    def __init__(self, path: Path, expected: str, actual: str) -> None:
        super().__init__(
            f"{path} has SHA-256 {actual}, but the pin requires {expected}; refusing to load it"
        )
        self.path = path
        self.expected = expected
        self.actual = actual


class Encoder(Protocol):
    """What a layer-3 encoder must provide. Two implementations, one decision rule.

    The three attributes are declared as read-only properties rather than as plain variables because
    one implementation is a frozen dataclass, and a frozen field is not assignable to a *writable*
    protocol attribute. Read-only is also the truer statement: the encoder's method and artifact hash
    are facts about the loaded artifact, not settings a caller may adjust.
    """

    @property
    def method(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    @property
    def artifact_hash(self) -> str | None: ...

    def encode(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity of two vectors. Returns 0.0 for a zero vector rather than raising.

    A zero vector is what an empty message produces, and "no similarity to anything" is exactly the
    right reading of it — the layer's job is then to say so (the band decides), not to divide by zero.
    """
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right, strict=False):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot / (math.sqrt(left_norm) * math.sqrt(right_norm))


def _normalise(vector: list[float]) -> tuple[float, ...]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0.0:
        return tuple(vector)
    return tuple(value / norm for value in vector)


@dataclass(frozen=True, slots=True)
class HashedReferenceEncoder:
    """Char n-grams, hashed into a fixed-width vector. Deterministic, stdlib only, no artifact.

    Determinism matters more than it sounds: ``hash()`` on a string is salted per process, so a
    vectoriser built on it would produce different vectors in different runs and a threshold measured
    in one process would be meaningless in the next. ``blake2b`` is used instead, which makes the
    measurement reproducible from the recorded corpus hash alone.

    Character n-grams rather than words because the messages are Turkish and English, and Turkish
    agglutinates: a word-level matcher would treat ``"randevumu"`` and ``"randevu"`` as unrelated
    while a 3–4 gram matcher does not.
    """

    dimensions: int = 512
    ngrams: tuple[int, ...] = (3, 4)
    method: str = HASHED_METHOD
    artifact_hash: str | None = None

    def encode(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._encode_one(text) for text in texts)

    def _encode_one(self, text: str) -> tuple[float, ...]:
        folded = fold(text)
        counts: dict[int, int] = {}
        for size in self.ngrams:
            for start in range(max(len(folded) - size + 1, 0)):
                gram = folded[start : start + size]
                if gram.strip() == "":
                    continue
                digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest, "big") % self.dimensions
                counts[index] = counts.get(index, 0) + 1
        vector = [0.0] * self.dimensions
        for index, count in counts.items():
            # Sublinear term frequency: a message that repeats a word ten times is not ten times as
            # much about that word, and repeated padding is a cheap way to dominate a cosine score.
            vector[index] = 1.0 + math.log(count)
        return _normalise(vector)


class OnnxEncoder:
    """ADR-0005's pinned CPU encoder, loaded only from a hash-verified artifact.

    ``onnxruntime``, ``tokenizers`` and ``numpy`` are an optional extra, so they are imported at load
    time rather than at module import: a checkout without them must still be able to import this
    package and report that the encoder rung is closed.
    """

    def __init__(
        self, session: Any, tokenizer: Any, *, pin: ArtifactPin, max_length: int = 256
    ) -> None:
        self._session = session
        self._tokenizer = tokenizer
        self._max_length = max_length
        self.pin = pin
        #: Named with the artifact's hash, so a report says which bytes produced a number.
        self.method = f"minilm-onnx:{pin.sha256[:12]}"
        self.dimensions = 384  # all-MiniLM-L6-v2's hidden size; asserted against the graph's output
        self.artifact_hash = pin.sha256

    @classmethod
    def load(cls, pin: ArtifactPin, artifact_dir: Path) -> OnnxEncoder:
        """Verify the bytes, then load. A missing file or a wrong digest is a refusal, not a warning."""
        graph = artifact_dir / pin.file
        tokenizer_file = artifact_dir / pin.tokenizer_file
        if not graph.is_file() or not tokenizer_file.is_file():
            raise EncoderUnavailable(
                f"the pinned artifact for {pin.pin} is not present in {artifact_dir}. Models are "
                "never committed (they are 23 MB and .pre-commit-config.yaml refuses files over 2 MB), "
                "so this is downloaded and verified on demand, not assumed."
            )
        actual = sha256_file(graph)
        if actual != pin.sha256:
            raise ArtifactHashMismatch(graph, pin.sha256, actual)

        # All three optional dependencies are checked *here*, at load time, so that "the encoder rung
        # is closed" is something an operator learns at startup rather than at the first patient
        # message. numpy is imported in `encode`, but a checkout with onnxruntime and no numpy is a
        # checkout whose layer 3 would fail on its first call, which is the failure this prevents.
        try:
            for module_name in ("onnxruntime", "tokenizers", "numpy"):
                importlib.import_module(module_name)
        except ImportError as error:
            raise EncoderUnavailable(
                "the pinned encoder needs its optional runtime (onnxruntime, tokenizers, numpy) and "
                f"this environment is missing {error.name!r}. The rung is closed rather than "
                f"substituted (ADR-0005): install the extra, or name the offline rung "
                f"{OFFLINE_METHOD_PIN!r} deliberately."
            ) from error
        onnxruntime = importlib.import_module("onnxruntime")
        tokenizers = importlib.import_module("tokenizers")
        try:
            session = onnxruntime.InferenceSession(str(graph), providers=["CPUExecutionProvider"])
            tokenizer = tokenizers.Tokenizer.from_file(str(tokenizer_file))
        except (
            Exception
        ) as error:  # the bytes are verified, so any failure here is a runtime problem
            # Deliberately broad: the bytes are already verified, so a failure here is a runtime
            # problem (a graph the runtime refuses, a tokenizer that is not a tokenizer) and the
            # contract of this method is a *refusal*, not a traceback in the middle of startup.
            raise EncoderUnavailable(
                f"the artifact for {pin.pin} is present and its hash matches, but it would not "
                f"load: {type(error).__name__}: {error}"
            ) from error
        tokenizer.enable_truncation(max_length=256)
        tokenizer.enable_padding(length=None)
        return cls(session, tokenizer, pin=pin)

    def encode(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        numpy = importlib.import_module("numpy")
        encodings = self._tokenizer.encode_batch(list(texts), add_special_tokens=True)
        ids = numpy.asarray([encoding.ids for encoding in encodings], dtype="int64")
        mask = numpy.asarray([encoding.attention_mask for encoding in encodings], dtype="int64")
        names = {item.name for item in self._session.get_inputs()}
        feed: dict[str, Any] = {"input_ids": ids}
        if "attention_mask" in names:
            feed["attention_mask"] = mask
        if "token_type_ids" in names:
            feed["token_type_ids"] = numpy.zeros_like(ids)
        output = self._session.run(None, feed)[0]
        pooled = self._pool(numpy, output, mask)
        return tuple(_normalise(list(row)) for row in pooled)

    @staticmethod
    def _pool(numpy: Any, output: Any, mask: Any) -> Any:
        """Mean-pool a token-level output; pass a sentence-level output through.

        ADR-0005 recorded that whether the export's graph already pools is *unverified*, and the
        honest way to handle an unverified assumption is to accept both shapes: a 3-D output
        (batch, tokens, hidden) is pooled with the attention mask, a 2-D output (batch, hidden) is
        already a sentence embedding and is used as it is.
        """
        if output.ndim == 2:
            return output
        expanded = mask[..., None].astype("float32")
        summed = (output * expanded).sum(axis=1)
        counts = numpy.clip(expanded.sum(axis=1), 1e-9, None)
        return summed / counts


@dataclass(frozen=True, slots=True)
class ClassifierScore:
    """One scored message: what it resembled, and by how much.

    Every field is a measurement. ``similarity`` is a cosine similarity to the closest attack
    reference — **not** a probability, not calibrated, and not to be presented as a confidence.
    """

    similarity: float
    benign_similarity: float
    attack_ref_id: str
    benign_ref_id: str
    attack_category: GuardrailCategory
    method: str
    threshold: float
    margin: float

    @property
    def high(self) -> float:
        return self.threshold + self.margin

    @property
    def low(self) -> float:
        return self.threshold - self.margin

    @property
    def inside_band(self) -> bool:
        return self.low < self.similarity < self.high

    def decide(self) -> tuple[GuardrailDecision, GuardrailCategory, str]:
        """The decision rule, in one place, so `/admin` and the report cannot describe it differently."""
        if self.similarity >= self.high and self.similarity >= self.benign_similarity:
            return (
                GuardrailDecision.block,
                self.attack_category,
                f"cosine similarity {self.similarity:.3f} to reference {self.attack_ref_id} "
                f"({self.attack_category.value}) is at or above the measured threshold "
                f"{self.high:.3f} for method {self.method}",
            )
        if self.similarity <= self.low and self.benign_similarity >= self.similarity:
            return (
                GuardrailDecision.allow,
                GuardrailCategory.benign,
                f"cosine similarity {self.similarity:.3f} is at or below {self.low:.3f} and the "
                f"closest reference is benign ({self.benign_ref_id}) for method {self.method}",
            )
        return (
            GuardrailDecision.clarify,
            GuardrailCategory.off_topic,
            f"cosine similarity {self.similarity:.3f} (attack reference {self.attack_ref_id}) sits "
            f"inside the uncertainty band {self.low:.3f}–{self.high:.3f} for method {self.method}: "
            "uncertainty and disagreement resolve to clarify/HITL, never to a silent allow (§9)",
        )

    def as_dict(self) -> Mapping[str, object]:
        return {
            "similarity": round(self.similarity, 6),
            "benign_similarity": round(self.benign_similarity, 6),
            "attack_ref_id": self.attack_ref_id,
            "benign_ref_id": self.benign_ref_id,
            "method": self.method,
            "threshold": self.threshold,
            "margin": self.margin,
            "score_is_calibrated_probability": False,
        }


@dataclass(frozen=True, slots=True)
class ThresholdRecord:
    """ADR-0005's intake rule as a type: a threshold is only usable with its measurement.

    ``threshold`` is not a number somebody chose; it is the operating point selected on the
    calibration partition and reported together with what it produced there and on the held-out
    partition. The two hashes are what make the record *about* a specific corpus and reference set,
    so editing either invalidates it instead of silently redefining what was measured.
    """

    method: str
    encoder: str
    threshold: float
    margin: float
    corpus_version: str
    corpus_hash: str
    references_version: str
    references_hash: str
    selected_on_partition: str
    target_false_positive_rate: float
    calibration: Mapping[str, Any]
    held_out: Mapping[str, Any]
    #: The SHA-256 of the artifact the record was measured with, in the defaulted block because it is
    #: absent for the stdlib rung. Empty is that fact stated: the `method` above is what stops an empty
    #: hash from being read as "the pinned encoder, unverified".
    artifact_hash: str = ""
    notes: str = ""
    measured_at: str = ""
    measured_latency_ms: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ThresholdRecord:
        return cls(
            method=str(payload["method"]),
            encoder=str(payload.get("encoder", payload["method"])),
            threshold=float(payload["threshold"]),
            margin=float(payload.get("margin", DEFAULT_MARGIN)),
            corpus_version=str(payload.get("corpus_version", "")),
            corpus_hash=str(payload.get("corpus_hash", "")),
            references_version=str(payload.get("references_version", "")),
            references_hash=str(payload.get("references_hash", "")),
            selected_on_partition=str(payload.get("selected_on_partition", "")),
            target_false_positive_rate=float(payload.get("target_false_positive_rate", 0.0)),
            calibration=dict(payload.get("calibration") or {}),
            held_out=dict(payload.get("held_out") or {}),
            artifact_hash=str(payload.get("artifact_hash", "")),
            notes=str(payload.get("notes", "")),
            measured_at=str(payload.get("measured_at", "")),
            measured_latency_ms=dict(payload.get("measured_latency_ms") or {}),
        )

    def as_dict(self) -> Mapping[str, Any]:
        return asdict(self)

    def mismatch(
        self,
        *,
        method: str,
        corpus: Any,
        references: ReferenceSet,
        artifact_hash: str | None = None,
    ) -> str | None:
        """Why this record cannot speak for the current data, or None when it can.

        ``artifact_hash`` is the encoder's own hash when there is one. §9 requires the artifact hash
        *recorded*, and recording it is only worth something if it is also compared: a threshold
        measured against one set of bytes and applied to another is a number about neither.
        """
        if self.method != method:
            return (
                f"the record measures method {self.method!r}, and this layer runs {method!r}; a "
                "threshold from one method does not transfer to another"
            )
        if artifact_hash is not None and self.artifact_hash and artifact_hash != self.artifact_hash:
            return (
                f"the record was measured with an artifact hashing to {self.artifact_hash[:12]}, and "
                f"the encoder in use hashes to {artifact_hash[:12]}"
            )
        if self.corpus_hash != corpus.content_hash:
            return (
                f"the record was measured on corpus {self.corpus_hash[:12]}, and the corpus in the "
                f"tree hashes to {corpus.content_hash[:12]}; re-measure rather than re-interpret"
            )
        if self.references_hash != references.content_hash:
            return (
                f"the record was measured against reference set {self.references_hash[:12]}, and the "
                f"set in the tree hashes to {references.content_hash[:12]}"
            )
        return None


def load_threshold_record(path: Path | None = None) -> ThresholdRecord | None:
    """Read the recorded threshold, or None when nothing has been measured for this package."""
    target = path or (Path(__file__).resolve().parent / "data" / "threshold_record.json")
    if not target.is_file():
        return None
    payload = json.loads(target.read_text(encoding="utf-8"))
    if payload.get("threshold") in (None, "", "CHANGEME_MEASURED_THRESHOLD"):
        return None
    return ThresholdRecord.from_mapping(payload)


def select_threshold(
    scores: Sequence[tuple[float, bool]],
    *,
    target_false_positive_rate: float,
    margin: float = DEFAULT_MARGIN,
    step: float = 0.01,
) -> float:
    """Choose the operating point on the calibration partition, with its false-positive budget first.

    ``scores`` is ``(attack_similarity, is_attack)`` per case. The rule is deliberately the boring
    one: the **lowest** threshold whose block rate on the benign cases stays inside the budget, so
    the layer is as sensitive as the false-positive budget allows and no more. Ties are broken toward
    the lower threshold, because a guardrail that misses an attack costs more than one that asks a
    human a question.

    This is not a calibration procedure and does not claim to be: it selects a number, and the
    corpus is what makes the number mean anything.
    """
    best: float | None = None
    candidate = 0.99
    while candidate > 0.0:
        benign = [score for score, is_attack in scores if not is_attack]
        flagged = sum(1 for score in benign if score >= candidate + margin)
        rate = flagged / len(benign) if benign else 1.0
        if rate <= target_false_positive_rate:
            best = candidate
        candidate = round(candidate - step, 6)
    return best if best is not None else 0.99


def _reference_index(
    encoder: Encoder, references: ReferenceSet
) -> tuple[
    tuple[tuple[float, ...], ...],
    tuple[Reference, ...],
    tuple[tuple[float, ...], ...],
    tuple[Reference, ...],
]:
    """Encode both sides once. The reference set is static, so this is paid at construction."""
    attack_vectors = encoder.encode(references.text_of(references.attacks))
    benign_vectors = encoder.encode(references.text_of(references.benign))
    return attack_vectors, references.attacks, benign_vectors, references.benign


class LocalClassifierGuardrail:
    """§9 layer 3, closed unless a measured threshold exists for the method it runs.

    ``is_configured`` is False — and every write that needs this layer is refused — when the encoder
    cannot be built, when there is no threshold record, when the record is for a different method or
    a different corpus, or when the environment threshold disagrees with the recorded one. Each of
    those reasons is carried into the refusal, because "layer 3 is off" is not something an operator
    can act on and "the record was measured on a different corpus" is.
    """

    name: GuardrailLayer = GuardrailLayer.local_classifier

    def __init__(
        self,
        *,
        encoder: Encoder,
        references: ReferenceSet,
        record: ThresholdRecord | None,
        env_threshold: str = "",
        corpus: Any | None = None,
        reason_if_unconfigured: str = "",
    ) -> None:
        self.encoder = encoder
        self.references = references
        self._record = record
        self._reason = reason_if_unconfigured
        if record is not None:
            self.threshold = record.threshold
            self.margin = record.margin
        else:
            self.threshold = 0.0
            self.margin = DEFAULT_MARGIN
        #: Carried into the closure reasons so a caller can see *which* measurement is in force.
        self._env_threshold = env_threshold.strip()
        self._corpus = corpus
        self._attack_vectors, self._attack_refs, self._benign_vectors, self._benign_refs = (
            _reference_index(encoder, references)
        )

    # -- construction -------------------------------------------------------

    @classmethod
    def build(
        cls,
        *,
        pin: str,
        encoder: Encoder | None,
        references: ReferenceSet,
        record: ThresholdRecord | None,
        corpus: Any | None,
        env_threshold: str = "",
        closed_reason: str = "",
    ) -> LocalClassifierGuardrail:
        """Assemble the layer, preferring a *stated* refusal over a substitute implementation."""
        method = encoder.method if encoder is not None else HASHED_METHOD
        reason = closed_reason
        if not reason and encoder is None:
            reason = (
                f"no local classifier is available for {pin!r}. Layer 3 runs the pinned encoder "
                f"({pin}) or the explicitly named offline rung ({OFFLINE_METHOD_PIN}); nothing is "
                "substituted silently."
            )
        if not reason and record is None:
            reason = (
                "no threshold measurement is recorded for this method. §9 requires the method, the "
                "artifact hash and the thresholds to be recorded, and ADR-0005 forbids applying an "
                "unmeasured default; until the measurement exists the stage is inert and says so."
            )
        if not reason and record is not None:
            reason = (
                record.mismatch(
                    method=method,
                    corpus=corpus,
                    references=references,
                    artifact_hash=encoder.artifact_hash if encoder is not None else None,
                )
                or ""
            )
        if not reason and env_threshold and record is not None:
            try:
                differs = abs(float(env_threshold) - record.threshold) > 1e-9
            except ValueError:
                differs = True
            if differs:
                reason = (
                    f"GUARDRAIL_LOCAL_THRESHOLD={env_threshold} does not match the measured "
                    f"threshold {record.threshold} recorded for method {method}; an operator can "
                    "raise or lower a threshold without touching the measurement, which is exactly "
                    "what the intake rule exists to prevent"
                )
        layer = cls(
            encoder=encoder or HashedReferenceEncoder(),
            references=references,
            record=None if reason else record,
            env_threshold=env_threshold,
            corpus=corpus,
            reason_if_unconfigured=reason,
        )
        if reason:
            # Keep the record visible on a closed layer: the report has to be able to say *what* a
            # closed layer would have run with, without that implying it ran. `is_configured` is what
            # decides, and it reads the reason, not the record.
            layer._record = record
        return layer

    # -- state --------------------------------------------------------------

    def is_configured(self) -> bool:
        return not self._reason and self._record is not None

    @property
    def method(self) -> str:
        """The method in force, or ``""`` when none is: a closed layer has no method to report.

        This matters more than it looks. A closed layer still holds an encoder object (so the
        decision rule has something to score with if a record ever arrives), and reporting *its*
        method would print ``hashed-char-ngram-v1`` beside a layer that ran nothing — the exact
        misattribution every report in this package is keyed by method to avoid. What the layer
        *would* have run with is in ``reason_if_unconfigured``.
        """
        return self.encoder.method if self.is_configured() else ""

    @property
    def reason_if_unconfigured(self) -> str:
        return self._reason

    @property
    def record(self) -> ThresholdRecord | None:
        return self._record

    # -- scoring ------------------------------------------------------------

    def score(self, text: str) -> ClassifierScore:
        """The similarity of one message to the closest attack and benign references."""
        vector = self.encoder.encode([text])[0]
        attack_similarity, attack_index = _best(vector, self._attack_vectors)
        benign_similarity, benign_index = _best(vector, self._benign_vectors)
        attack_reference = self._attack_refs[attack_index]
        return ClassifierScore(
            similarity=attack_similarity,
            benign_similarity=benign_similarity,
            attack_ref_id=attack_reference.ref_id,
            benign_ref_id=self._benign_refs[benign_index].ref_id,
            attack_category=attack_reference.category or GuardrailCategory.benign,
            method=self.encoder.method,
            threshold=self.threshold,
            margin=self.margin,
        )

    def screen(self, request: ScreeningRequest, masked_text: str) -> LayerVerdict:
        if not self.is_configured():
            raise_unavailable(self.name, self._reason)
        started = time.perf_counter()
        score = self.score(masked_text)
        decision, category, reason = score.decide()
        return LayerVerdict(
            layer=self.name,
            decision=decision,
            category=category,
            reason=(
                f"{reason}. The score is a cosine similarity to one reference sentence, not a "
                "calibrated probability (§9)"
            ),
            latency_ms=int((time.perf_counter() - started) * 1000),
            method=self.encoder.method,
            similarity=score.similarity,
        )


def _best(vector: Sequence[float], matrix: Sequence[Sequence[float]]) -> tuple[float, int]:
    best_score = -1.0
    best_index = 0
    for index, reference in enumerate(matrix):
        score = cosine(vector, reference)
        if score > best_score:
            best_score = score
            best_index = index
    return best_score, best_index
