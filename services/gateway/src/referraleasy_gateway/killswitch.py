# ruff: noqa: RUF002  # §16 is quoted in Turkish, verbatim from the spec
"""§16's kill switch: an operator stops booking writes, and read-only guidance keeps answering.

    Admin kill switch booking yazmayı durdurabilir; salt okunur rehber devam eder.

**What it stops, and what it deliberately does not.** §16 says *booking* writes, and the second half
of the sentence is the constraint: the static guide stays available. So the switch is checked on the
one route that writes a booking and nowhere else — not on the stream, not on the help card, not on the
transitions a coordinator still needs to finish or unwind work already in flight. A switch that
stopped everything would be an outage with a friendlier name, and it would strand the very workflows
an operator needs to see while they decide what to do.

**Held in the process, and that is a real limitation.** One gateway replica honours its own switch; a
second replica would not see it until it restarted. For §18's demo that is one container and the
switch works. A deployment with more than one replica needs a shared store, and the seam for that is
this class: `engage` and `release` are the only two writes, so a durable implementation is a different
body for them rather than a different design. Stated here rather than discovered in production.

**Engaging is idempotent and keeps the first reason.** An operator who escalates a switch — "stopping
the canary" becoming "stopping everything" — should not have the earlier, more specific reason
overwritten by a later, vaguer one, because the reason is what the next reader has to act on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

__all__ = ["KillSwitch", "KillSwitchState"]


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    """What the switch is, for a page or an error message."""

    engaged: bool
    reason: str
    since: datetime | None
    #: Who engaged it. Free text, and not an identity claim: §9's admin session is a fixture in the
    #: demo profile, so this records what an operator typed and is presented as a note.
    by: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "engaged": self.engaged,
            "reason": self.reason,
            "since": self.since.isoformat() if self.since else None,
            "by": self.by,
        }


class KillSwitch:
    """The switch itself. Mutable by design — it is a control, not a value.

    Not thread-safe. The gateway runs one event loop and the two methods do not await, so a racing
    pair of calls cannot interleave; a threaded server would need a lock, and saying so here is
    cheaper than a lock nobody measures.
    """

    def __init__(self) -> None:
        self._state = KillSwitchState(engaged=False, reason="", since=None, by="")

    @property
    def state(self) -> KillSwitchState:
        return self._state

    @property
    def engaged(self) -> bool:
        return self._state.engaged

    def engage(self, *, reason: str, by: str, now: datetime) -> KillSwitchState:
        """Stop booking writes. Idempotent: engaging an engaged switch keeps the first reason."""
        if self._state.engaged:
            return self._state
        self._state = KillSwitchState(engaged=True, reason=reason, since=now, by=by)
        return self._state

    def release(self, *, by: str, now: datetime) -> KillSwitchState:
        """Allow booking writes again. Idempotent, and clears the reason with the flag.

        The released state keeps who released it and when, in the same fields, so the *next* reader
        can tell "never engaged" from "engaged and released" — a switch with no history reads as one
        that was never touched, and those are different facts about an incident.
        """
        if not self._state.engaged:
            return self._state
        self._state = KillSwitchState(
            engaged=False,
            reason=f"released (was: {self._state.reason})",
            since=now,
            by=by,
        )
        return self._state
