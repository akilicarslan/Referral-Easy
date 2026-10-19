"""The labelled corpus, the reference utterances and the artifact pins — all as data (§9).

§9 asks for measurements rather than opinions — *"Measure false positives/false negatives and added
latency/cost per layer"* — and a measurement needs a labelled set that was written down before the
numbers were. This module is where that set lives and how it is hashed.

Three things are deliberate:

* **The corpus is not derived from the implementation.** The cases come from what patients and
  attackers actually say (a request, a rude request, a bare `evet`, a coverage question, a reordered
  injection, a base64 blob, an emergency, the fixture document that carries an injection attempt).
  Several of them are cases the implementation gets wrong, and they are kept precisely because
  excluding them would make the reported rate a description of the corpus rather than of the layer.
* **Every corpus and reference file is identified by a content hash.** Layer 3's threshold is
  measured against a specific corpus revision (ADR-0005's intake rule), so the threshold record
  carries these hashes and a changed corpus invalidates it instead of quietly redefining what the
  threshold was measured on.
* **The calibration/held_out split is enforced, not advisory.** Layer 3's threshold is selected on
  ``calibration`` and the headline rates are computed on ``held_out``, so tuning the threshold is not
  the same act as reporting its performance. A test asserts both partitions are non-empty and that
  they do not overlap.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

# First-party: the contract enums, so a case's expectation is stated in the same vocabulary the
# layers answer in.
from referraleasy_contracts.generated.common import GuardrailCategory, GuardrailDecision
from referraleasy_guardrails.types import ScreeningRequest

__all__ = [
    "ARTIFACTS_FILE",
    "CORPUS_FILE",
    "REFERENCE_SET_FILE",
    "ArtifactPin",
    "CorpusCase",
    "LabelledCorpus",
    "Reference",
    "ReferenceSet",
    "data_dir",
    "sha256_bytes",
    "sha256_file",
]

DATA_DIR: Final = Path(__file__).resolve().parent / "data"
CORPUS_FILE: Final = DATA_DIR / "labelled_corpus.v1.json"
REFERENCE_SET_FILE: Final = DATA_DIR / "reference_utterances.v1.json"
ARTIFACTS_FILE: Final = DATA_DIR / "artifacts.json"

#: The repo root, derived from this file's location: ``packages/guardrails/src/referraleasy_guardrails``
#: is four levels below it, never the cwd.
REPO_ROOT: Final = Path(__file__).resolve().parents[4]

USER_MESSAGE: Final = "user_message"


def data_dir() -> Path:
    return DATA_DIR


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    """Streamed digest, because the artifact this verifies is 23 MB and is not the only one we may want."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(payload: object) -> str:
    """A stable hash of a JSON-serialisable value, so a record can name exactly what it measured."""
    return sha256_bytes(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))


@dataclass(frozen=True, slots=True)
class CorpusCase:
    """One labelled message, and why it is in the corpus."""

    case_id: str
    channel: str
    kind: str
    expected: GuardrailDecision
    language: str
    partition: str
    workflow_state: str
    text: str
    note: str
    short_reply_expected: bool = False
    open_question: str | None = None
    expected_category: GuardrailCategory | None = None

    @property
    def is_user_message(self) -> bool:
        """Whether this is a message the three layers screen at all.

        The fixture document is *not*: it arrives as retrieved content, and §9's answer to prompt
        injection in retrieved content is not a better regex — it is the tool allowlist, minimum
        context, treating sources as data and server-side policy. It is in the corpus so that the
        layers are exercised against it and the miss is reported, not so that the layers are asked to
        carry a defence they do not have.
        """
        return self.channel == USER_MESSAGE

    @property
    def is_attack(self) -> bool:
        return self.kind == "attack"

    @property
    def is_benign(self) -> bool:
        """Cases where refusing or over-asking is a cost to a real patient."""
        return self.kind in {"benign", "short_reply_valid", "profanity_genuine"}

    def request(self) -> ScreeningRequest:
        return ScreeningRequest(
            text=self.text,
            workflow_state=self.workflow_state,
            short_reply_expected=self.short_reply_expected,
            synthetic=True,
            open_question=self.open_question,
        )


@dataclass(frozen=True, slots=True)
class LabelledCorpus:
    """The corpus, with the hash that identifies the revision a threshold was measured against."""

    version: str
    cases: tuple[CorpusCase, ...]
    content_hash: str

    @classmethod
    def load(cls, path: Path | None = None, *, repo_root: Path | None = None) -> LabelledCorpus:
        payload = json.loads((path or CORPUS_FILE).read_text(encoding="utf-8"))
        root = repo_root or REPO_ROOT
        cases = tuple(cls._case(entry, root) for entry in payload.get("cases", []))
        if not cases:
            raise ValueError("the labelled corpus is empty; a rate over no cases is not a rate")
        return cls(
            version=str(payload.get("version", "unversioned")),
            cases=cases,
            content_hash=canonical_hash([asdict(case) for case in cases]),
        )

    @staticmethod
    def _case(entry: Mapping[str, Any], repo_root: Path) -> CorpusCase:
        text = str(entry.get("text", ""))
        repeat = entry.get("repeat")
        if isinstance(repeat, int) and repeat > 1:
            text = text * repeat
        fixture_ref = entry.get("fixture_ref")
        if isinstance(fixture_ref, Mapping):
            text = LabelledCorpus._fixture_text(fixture_ref, repo_root)
        raw_category = entry.get("expected_category")
        return CorpusCase(
            case_id=str(entry["case_id"]),
            channel=str(entry.get("channel", USER_MESSAGE)),
            kind=str(entry["kind"]),
            expected=GuardrailDecision(str(entry["expected"])),
            language=str(entry.get("language", "en")),
            partition=str(entry.get("partition", "held_out")),
            workflow_state=str(entry.get("workflow_state", "INTAKE")),
            text=text,
            note=str(entry.get("note", "")),
            short_reply_expected=bool(entry.get("short_reply_expected", False)),
            open_question=entry.get("open_question") or None,
            expected_category=GuardrailCategory(str(raw_category)) if raw_category else None,
        )

    @staticmethod
    def _fixture_text(ref: Mapping[str, Any], repo_root: Path) -> str:
        """Resolve a case whose text is a committed fixture, so the corpus cannot drift from it.

        The injected referral letter is a real fixture with a real ``contains_injection_attempt``
        flag; copying its text into this file would be a second copy to keep in step, and a corpus
        that quietly stopped matching the fixture would still pass.
        """
        documents = json.loads((repo_root / str(ref["path"])).read_text(encoding="utf-8"))
        select = str(ref["select"])
        field = str(ref.get("field", "text"))
        for document in documents:
            if document.get(select) is True:
                return str(document[field])
        raise ValueError(f"no document in {ref['path']} has {select!r} set")

    def user_messages(self) -> tuple[CorpusCase, ...]:
        return tuple(case for case in self.cases if case.is_user_message)

    def in_channel(self, channel: str) -> tuple[CorpusCase, ...]:
        return tuple(case for case in self.cases if case.channel == channel)

    def in_partition(self, partition: str) -> tuple[CorpusCase, ...]:
        return tuple(
            case for case in self.cases if case.partition == partition and case.is_user_message
        )

    def by_kind(self, *kinds: str) -> tuple[CorpusCase, ...]:
        return tuple(case for case in self.cases if case.kind in kinds)

    def partitions(self) -> tuple[str, ...]:
        return tuple(sorted({case.partition for case in self.cases}))

    def counts(self) -> Mapping[str, int]:
        """How many cases of each kind, so a report can show its own class balance."""
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.kind] = counts.get(case.kind, 0) + 1
        return counts


@dataclass(frozen=True, slots=True)
class Reference:
    """One canonical utterance layer 3 compares against. A benign reference carries no category."""

    ref_id: str
    language: str
    text: str
    category: GuardrailCategory | None = None

    @property
    def benign(self) -> bool:
        return self.category is None


@dataclass(frozen=True, slots=True)
class ReferenceSet:
    """The versioned reference utterances and their hash (ADR-0005: a fixture with its own hash)."""

    version: str
    attacks: tuple[Reference, ...]
    benign: tuple[Reference, ...]
    content_hash: str

    @classmethod
    def load(cls, path: Path | None = None) -> ReferenceSet:
        payload = json.loads((path or REFERENCE_SET_FILE).read_text(encoding="utf-8"))
        attacks = tuple(
            Reference(
                ref_id=str(entry["ref_id"]),
                language=str(entry.get("language", "en")),
                text=str(entry["text"]),
                category=GuardrailCategory(str(entry["category"])),
            )
            for entry in payload.get("attack_references", [])
        )
        benign = tuple(
            Reference(
                ref_id=str(entry["ref_id"]),
                language=str(entry.get("language", "en")),
                text=str(entry["text"]),
            )
            for entry in payload.get("benign_references", [])
        )
        if not attacks or not benign:
            raise ValueError(
                "the reference set needs both sides; one-sided matching is not a decision"
            )
        return cls(
            version=str(payload.get("version", "unversioned")),
            attacks=attacks,
            benign=benign,
            content_hash=canonical_hash(
                {
                    "attacks": [asdict(reference) for reference in attacks],
                    "benign": [asdict(reference) for reference in benign],
                }
            ),
        )

    def all_references(self) -> tuple[Reference, ...]:
        return self.attacks + self.benign

    def text_of(self, references: Sequence[Reference]) -> tuple[str, ...]:
        return tuple(reference.text for reference in references)

    def categories(self) -> Iterable[str]:
        return sorted(
            {reference.category.value for reference in self.attacks if reference.category}
        )


@dataclass(frozen=True, slots=True)
class ArtifactPin:
    """One artifact, pinned by content hash rather than by name (ADR-0005)."""

    model: str
    revision: str
    file: str
    sha256: str
    bytes: int
    tokenizer_file: str
    tokenizer_git_blob_sha1: str
    adr: str

    @property
    def pin(self) -> str:
        """The form the environment names it in: ``<model>@<revision>``."""
        return f"{self.model}@{self.revision}"

    @classmethod
    def load_all(cls, path: Path | None = None) -> tuple[ArtifactPin, ...]:
        payload = json.loads((path or ARTIFACTS_FILE).read_text(encoding="utf-8"))
        pins = []
        for entry in payload.get("pins", []):
            tokenizer = entry.get("tokenizer") or {}
            pins.append(
                cls(
                    model=str(entry["model"]),
                    revision=str(entry["revision"]),
                    file=str(entry["file"]),
                    sha256=str(entry["sha256"]),
                    bytes=int(entry.get("bytes", 0)),
                    tokenizer_file=str(tokenizer.get("file", "")),
                    tokenizer_git_blob_sha1=str(tokenizer.get("git_blob_sha1", "")),
                    adr=str(entry.get("adr", "")),
                )
            )
        return tuple(pins)

    @classmethod
    def find(cls, pin: str, path: Path | None = None) -> ArtifactPin | None:
        """The pin an environment value names, or None when the value names no pinned artifact."""
        return next((entry for entry in cls.load_all(path) if entry.pin == pin), None)
