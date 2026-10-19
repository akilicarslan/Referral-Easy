"""The degradation ladder, written as a state machine instead of a chain of `try` blocks.

Retrieval has four things that can be missing — the reranker, the embedding provider, the index
itself, and any relevant source at all — and each one has a defined consequence. Expressing that
as nested exception handling produces code where "we silently answered from a shallower pipeline"
and "we answered from the full pipeline" look identical, which is how a degraded answer gets
presented as a confident one.

So: the rungs are ordered, the legal transitions are a table, and every descent is recorded as a
:class:`Degradation` with a reason. Anything that is not :data:`Rung.HYBRID_RERANK` sets
``degraded``. Callers are expected to surface that flag; the ladder's job is to make it
impossible to lose.

The ladder never climbs. A rung may only be replaced by one below it, and a state machine that
went back up would mean an answer assembled from a request that has already failed.

Two invariants are enforced rather than described:

* a descent to :data:`Rung.SEED_FALLBACK` requires that no call has been made — the seed rung is
  the one that must work with the network down, so it may not depend on a call having succeeded;
* :data:`Rung.HUMAN_HANDOFF` is terminal, and it is reached by *not finding anything*, never by
  inventing something.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Rung(StrEnum):
    """How much of the pipeline an answer actually came from."""

    HYBRID_RERANK = "hybrid_rerank"
    HYBRID_RRF = "hybrid_rrf"
    BM25_ONLY = "bm25_only"
    SEED_FALLBACK = "seed_fallback"
    HUMAN_HANDOFF = "human_handoff"


#: Best to worst. The order is the state machine's spine.
RUNG_ORDER: tuple[Rung, ...] = (
    Rung.HYBRID_RERANK,
    Rung.HYBRID_RRF,
    Rung.BM25_ONLY,
    Rung.SEED_FALLBACK,
    Rung.HUMAN_HANDOFF,
)

#: What each rung means, in the words the API and the UI should use.
RUNG_DESCRIPTIONS: Mapping[Rung, str] = {
    Rung.HYBRID_RERANK: "BM25 + vector, fused with RRF, reranked (full pipeline)",
    Rung.HYBRID_RRF: "BM25 + vector fused with RRF, ordered without a reranker",
    Rung.BM25_ONLY: "lexical (BM25) search only — embeddings unavailable",
    Rung.SEED_FALLBACK: (
        "versioned seed JSON, deterministically filtered — no index, no model call"
    ),
    Rung.HUMAN_HANDOFF: "no source found — the request is handed to a human",
}

#: Legal descents. Skipping a rung is allowed (a dead index goes straight to the seed); climbing
#: is not, and neither is leaving the terminal rung.
ALLOWED_TRANSITIONS: Mapping[Rung, frozenset[Rung]] = {
    Rung.HYBRID_RERANK: frozenset(
        {Rung.HYBRID_RRF, Rung.BM25_ONLY, Rung.SEED_FALLBACK, Rung.HUMAN_HANDOFF}
    ),
    Rung.HYBRID_RRF: frozenset({Rung.BM25_ONLY, Rung.SEED_FALLBACK, Rung.HUMAN_HANDOFF}),
    Rung.BM25_ONLY: frozenset({Rung.SEED_FALLBACK, Rung.HUMAN_HANDOFF}),
    Rung.SEED_FALLBACK: frozenset({Rung.HUMAN_HANDOFF}),
    Rung.HUMAN_HANDOFF: frozenset(),
}


class LadderViolation(RuntimeError):
    """A transition the ladder does not allow. A bug, not a runtime condition."""


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One outbound call, recorded so "no call was made" can be asserted rather than assumed."""

    kind: str
    detail: str = ""
    count: int = 1
    network: bool = True
    model: bool = False


class CallLedger:
    """Shared counter of everything a retrieval attempt sent outwards.

    The seed rung's whole value is that it cannot call anything. An assertion about that is only
    as good as the instrumentation behind it, so the adapters report here and the tests read it.
    """

    def __init__(self) -> None:
        self._records: list[CallRecord] = []

    def record(
        self,
        kind: str,
        *,
        detail: str = "",
        count: int = 1,
        network: bool = True,
        model: bool = False,
    ) -> None:
        self._records.append(
            CallRecord(kind=kind, detail=detail, count=count, network=network, model=model)
        )

    @property
    def records(self) -> tuple[CallRecord, ...]:
        return tuple(self._records)

    @property
    def model_calls(self) -> int:
        """Individual model invocations — one embed of 32 texts counts 32.

        The seed rung is required to make none of these. Counted per text rather than per
        request so that "no LLM call at all" cannot be satisfied by batching.
        """
        return sum(record.count for record in self._records if record.model)

    @property
    def network_calls(self) -> int:
        """Number of outbound requests of any kind, model calls included."""
        return sum(record.count for record in self._records if record.network)

    @property
    def requests(self) -> int:
        """Outbound HTTP requests actually issued — one per recorded network call.

        Distinct from :attr:`network_calls`, which counts the *units sent*: embedding 32 texts in
        one batched request is 32 network calls and 1 request. Both numbers are reported because
        they answer different questions ("how much text did we embed" vs "how many round trips did
        we cost"), and an operator reading a single number would misread one of them.
        """
        return sum(1 for record in self._records if record.network)

    @property
    def is_empty(self) -> bool:
        return not self._records

    def merge(self, other: CallLedger) -> None:
        """Fold another ledger's records into this one. Used per retrieval phase."""
        self._records.extend(other._records)

    def kinds(self) -> list[str]:
        return [record.kind for record in self._records]

    def summary(self) -> dict[str, Any]:
        return {
            "network_calls": self.network_calls,
            "model_calls": self.model_calls,
            "kinds": self.kinds(),
        }

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"CallLedger(network={self.network_calls}, model={self.model_calls})"


@dataclass(frozen=True, slots=True)
class Degradation:
    """One descent, with the reason it happened. Never summarised away."""

    rung: Rung
    reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"rung": str(self.rung), "reason": self.reason, "detail": self.detail}


@dataclass
class LadderState:
    """Where a single retrieval attempt stands on the ladder."""

    ledger: CallLedger = field(default_factory=CallLedger)
    rung: Rung = Rung.HYBRID_RERANK
    visited: list[Rung] = field(default_factory=lambda: [Rung.HYBRID_RERANK])
    degradations: list[Degradation] = field(default_factory=list)
    #: Ledger of the seed phase alone, when the seed rung was taken. The claim that the rung
    #: makes no model call is only worth something if it is observed rather than asserted.
    seed_ledger: CallLedger | None = None

    def attach_seed_ledger(self, ledger: CallLedger) -> None:
        """Record the calls made *by the seed rung*, and refuse to accept any model call."""
        if ledger.model_calls:
            raise LadderViolation(
                "the seed rung made a model call; it is required to answer without one: "
                f"{ledger.kinds()}"
            )
        self.seed_ledger = ledger

    @property
    def seed_is_silent(self) -> bool:
        """True when the seed rung was taken and issued no outbound request of any kind."""
        return self.seed_ledger is not None and self.seed_ledger.is_empty

    def can_descend_to(self, rung: Rung) -> bool:
        return rung in ALLOWED_TRANSITIONS[self.rung]

    def degrade(self, rung: Rung, reason: str, detail: str = "") -> None:
        """Move down the ladder, recording why.

        ``reason`` is short and stable (it is asserted on); ``detail`` carries the upstream
        message for a human reading a log.
        """
        if rung == self.rung:
            return
        if not self.can_descend_to(rung):
            raise LadderViolation(
                f"cannot move from {self.rung} to {rung}: "
                f"allowed descents are {sorted(str(item) for item in ALLOWED_TRANSITIONS[self.rung])}"
            )
        if rung is Rung.SEED_FALLBACK and self.ledger.model_calls != 0:
            raise LadderViolation(
                "the seed rung must answer without a model call, but "
                f"{self.ledger.model_calls} model call(s) were made: {self.ledger.kinds()}"
            )
        self.rung = rung
        self.visited.append(rung)
        self.degradations.append(Degradation(rung=rung, reason=reason, detail=detail))

    def hand_off(self, reason: str, detail: str = "") -> None:
        """Terminal: nothing was found, so a human decides. Never a synthesised answer."""
        self.degrade(Rung.HUMAN_HANDOFF, reason, detail)

    @property
    def degraded(self) -> bool:
        """True whenever the answer did not come from the full pipeline."""
        return self.rung is not Rung.HYBRID_RERANK

    @property
    def requires_human(self) -> bool:
        return self.rung is Rung.HUMAN_HANDOFF

    @property
    def description(self) -> str:
        return RUNG_DESCRIPTIONS[self.rung]

    @property
    def reasons(self) -> list[str]:
        return [degradation.reason for degradation in self.degradations]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rung": str(self.rung),
            "description": self.description,
            "degraded": self.degraded,
            "requires_human": self.requires_human,
            "visited": [str(rung) for rung in self.visited],
            "degradations": [degradation.to_dict() for degradation in self.degradations],
            "calls": self.ledger.summary(),
            "seed_made_no_call": self.seed_is_silent if self.seed_ledger is not None else None,
        }

    def summary_line(self) -> str:
        """One line for an operator: the rung, and why it is not the top one."""
        if not self.degraded:
            return f"rung={self.rung}: {self.description}"
        reasons = "; ".join(self.reasons) or "unspecified"
        return f"rung={self.rung} (DEGRADED): {reasons}"


__all__ = [
    "ALLOWED_TRANSITIONS",
    "RUNG_DESCRIPTIONS",
    "RUNG_ORDER",
    "CallLedger",
    "CallRecord",
    "Degradation",
    "LadderState",
    "LadderViolation",
    "Rung",
]
