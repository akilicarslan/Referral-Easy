"""Provenance-tagged tool results, stored apart from anything a model wrote (§6).

§6 asks for three things to be kept separately: the **conversation summary**, the
**structured preferences**, and the **provenance-tagged tool results**, with the
rule that a summary can never override source data. Two of those three live in the
gateway (it owns the conversation and the preferences). The third lives here,
because the tool results are produced inside a specialist's turn and the decision
that cites them is produced in the same turn.

The separation is enforced structurally rather than by discipline:

* **Only tools can write to the ledger.** :meth:`EvidenceLedger.record` takes a
  tool name and checks it against the §3 tool declarations, so a model's prose —
  a "summary" of what it thinks it retrieved — has no way in. There is no
  `record_summary`, no `add_note`, no free-text field to put one in.
* **Entries are frozen and carry the tool, the moment and the payload.** An entry
  a caller could edit after a decision cited it would make the citation
  worthless, so the dataclass is frozen and the payload is stored under a
  read-only mapping.
* **`:meth:`EvidenceLedger.ids` is the only way to get a citation,** and it returns
  ids in insertion order, so a decision's `evidence_ids` is a trace of what was
  actually retrieved in that turn rather than a list the model assembled.

This is turn-scoped by design. Durable storage of evidence is the gateway's job
(it owns the workflow's state and its audit trail); a specialist's turn is a pure
function of its envelope plus whatever its tools returned inside that turn.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from types import MappingProxyType
from typing import Any

from referraleasy_agents.canonical import id_text
from referraleasy_contracts.generated.common import EvidenceId

__all__ = [
    "EvidenceLedger",
    "ToolEvidence",
    "evidence_ids_in",
]


@lru_cache(maxsize=1)
def _known_tools() -> frozenset[str]:
    """The §3 tool names, imported at call time rather than at import time.

    There is a genuine cycle here: `referraleasy_agents.tools` re-exports the
    toolbox, the toolbox imports this module, and this module needs the tool
    declarations. Importing `referraleasy_agents.tools.specs` at module level
    executes the `tools` package's `__init__`, which imports the toolbox, which
    re-enters this half-built module — an `ImportError` that only appears when
    `evidence` happens to be imported first. The cycle is broken here, on the side
    that is a leaf, rather than by weakening the toolbox's public surface.
    """
    from referraleasy_agents.tools.specs import TOOL_SPECS

    return frozenset(TOOL_SPECS)


#: Keys a tool result may carry its provenance under. The two the mock API and the
#: contracts use, in the spellings they use: a singular `evidence_id` on a
#: retrieval result, a plural `evidence_ids` on anything contract-shaped.
_ID_KEYS = ("evidence_id", "evidence_ids")


def _walk(payload: Any) -> Iterator[str]:
    """Every evidence id anywhere in a tool result, in document order.

    Recursive because the ids do not sit at one level: `/availability` puts one at
    the top (`evidence_id`), `/documents/check` puts one per check inside `checks`,
    and a `CoverageResult` puts a list at the top and one per conflicting record.
    A tool result that carries an id is claiming provenance, and dropping part of
    it because it was nested one level deeper would quietly weaken every citation
    built from this ledger.
    """
    if isinstance(payload, Mapping):
        for key in _ID_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value:
                yield value
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, str) and item:
                        yield item
        for key, value in payload.items():
            if key not in _ID_KEYS and isinstance(value, (Mapping, list, tuple)):
                yield from _walk(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            yield from _walk(item)


def evidence_ids_in(payload: Any) -> tuple[EvidenceId, ...]:
    """The evidence ids a result carries, deduplicated, in document order."""
    seen: dict[str, None] = {}
    for raw in _walk(payload):
        seen.setdefault(raw, None)
    return tuple(EvidenceId(raw) for raw in seen)


@dataclass(frozen=True, slots=True)
class ToolEvidence:
    """One tool's result inside one turn, with where and when it came from."""

    tool: str
    evidence_id: EvidenceId
    payload: Mapping[str, Any]
    retrieved_at: str

    def as_trace_entry(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "evidence_id": id_text(self.evidence_id),
            "retrieved_at": self.retrieved_at,
        }


@dataclass(slots=True)
class EvidenceLedger:
    """The turn's retrieved records, keyed by evidence id.

    Not thread-safe and not meant to be: one ledger belongs to one turn, and a
    turn runs on one task.
    """

    _entries: dict[str, ToolEvidence] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)

    def record(self, tool: str, payload: Any) -> tuple[EvidenceId, ...]:
        """Record ``payload`` under ``tool``. Returns the ids it contributed.

        A tool name outside the §3 declarations is refused: this is the method that
        makes the ledger a record of *retrieval* rather than a scratchpad. A
        result with no evidence id at all is a finding worth keeping — it means the
        tool learned something it cannot cite — so it is returned as an empty tuple
        rather than raised, and `test_tool_surface.py` asserts the retrieval tools
        always cite something.
        """
        known = _known_tools()
        if tool not in known:
            raise KeyError(
                f"{tool!r} is not one of the §3 tools ({sorted(known)}); the evidence ledger "
                "records tool results, never text written by a model (§6)."
            )
        ids = evidence_ids_in(payload)
        if ids:
            stored = (
                MappingProxyType(dict(payload))
                if isinstance(payload, Mapping)
                else {"value": payload}
            )
            stamp = datetime.now(UTC).isoformat()
            for evidence_id in ids:
                key = id_text(evidence_id)
                if key in self._entries:
                    continue
                self._entries[key] = ToolEvidence(
                    tool=tool, evidence_id=evidence_id, payload=stored, retrieved_at=stamp
                )
                self._order.append(key)
        return ids

    def ids(self) -> list[EvidenceId]:
        """Every evidence id recorded, oldest first."""
        return [self._entries[key].evidence_id for key in self._order]

    def get(self, evidence_id: EvidenceId | str) -> ToolEvidence | None:
        """One entry, or ``None``. Absence is a real answer: "we never retrieved that"."""
        return self._entries.get(id_text(evidence_id))

    def require(self, evidence_id: EvidenceId | str) -> ToolEvidence:
        """One entry, or a :class:`KeyError` naming what was never retrieved."""
        entry = self.get(evidence_id)
        if entry is None:
            raise KeyError(
                f"evidence {evidence_id} was never retrieved in this turn; a decision may only "
                "cite what its tools returned (§6)."
            )
        return entry

    def by_tool(self, tool: str) -> list[ToolEvidence]:
        """Everything one tool contributed, for a trace or a test."""
        return [self._entries[key] for key in self._order if self._entries[key].tool == tool]

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, evidence_id: object) -> bool:
        return id_text(evidence_id) in self._entries

    def trace_entries(self) -> list[dict[str, Any]]:
        """The ledger as trace entries: ids and provenance, never payloads.

        Payloads are deliberately not traced — they are patient data, and a trace
        that carries them is a copy of the evidence in a place nobody audited.
        """
        return [self._entries[key].as_trace_entry() for key in self._order]
