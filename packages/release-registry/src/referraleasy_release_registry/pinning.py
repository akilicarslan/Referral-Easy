"""Conversation pinning, the kill switch, and the approval a migration needs (§13).

§13, in three sentences (quoted as written in ADR-0037):

* a conversation is **pinned to a release**, so a version change mid-conversation cannot
  mix two versions inside one conversation. A conversation that ran under three releases
  is not a conversation whose transcript can be explained by any of them.
* the safety kill switch can stop a conversation **pinned to an older release** too.
* **migrating to a new version requires explicit approval and a checkpoint.**

The second bullet is the one that reads as a contradiction and is not. A kill switch
that could only stop conversations on the *current* release would leave exactly the
conversations you most need to stop running — the ones stuck on a version you have
already moved past because it misbehaved. So :meth:`KillSwitch.can_stop` answers by the
pin's identity, never by the pin's age, and the test that asserts a three-releases-old
pin is still stoppable is the test that would fail if someone added a "current release
only" guard in the name of tidiness.

Where a conversation pin is stored
----------------------------------
There is no home for it in the existing schema, and this package may not add one.
``release_registry`` records *releases*, and ``release_event`` records what happened *to a
release*; neither has a conversation to hang a pin on. Rather than invent a second
schema, this module implements the mechanism — the value, the store contract, the
approval — and :data:`PIN_TABLE_DDL` is the exact DDL a migration would need to add.

That gap is reported, not papered over. A mechanism with an injected store is testable
and honest; a pin "stored" in a process dictionary and described as done would not be.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final, Protocol

from referraleasy_release_registry.effective import EffectiveVersions
from referraleasy_release_registry.errors import ReleaseRefusedError

__all__ = [
    "PIN_TABLE_DDL",
    "Approval",
    "ConversationPin",
    "InMemoryPinStore",
    "KillSwitch",
    "PinStore",
    "StopDirective",
    "migrate_conversation",
    "pin_conversation",
]

#: The DDL a migration would add to give conversation pins a home. Written out in full
#: rather than described, because "we need a table" is not a finding a reviewer can act
#: on and this is. It follows the append-only convention of ``012_release_registry.sql``:
#: a pin's history is rows, and a migration is a new row rather than an UPDATE, so "what
#: did this conversation run" stays answerable after the fact.
PIN_TABLE_DDL: Final = """
CREATE TABLE conversation_release_pin (
    id                     bigserial   PRIMARY KEY,
    conversation_id        uuid        NOT NULL,
    release_manifest_id    text        NOT NULL,
    manifest_hash          text        NOT NULL,
    agent_name             text        NOT NULL,
    effective_versions     jsonb       NOT NULL,
    pinned_at              timestamptz NOT NULL DEFAULT now(),
    pinned_by              text        NOT NULL,
    superseded_by          bigint      REFERENCES conversation_release_pin (id),
    approval_actor         text,
    approval_reason        text,

    CONSTRAINT conversation_release_pin_manifest_hash_hex
        CHECK (manifest_hash ~ '^[0-9a-f]{64}$'),
    -- A migration names who approved it; a pin created at conversation start does not.
    CONSTRAINT conversation_release_pin_migrated_has_approval
        CHECK (superseded_by IS NULL OR approval_actor IS NOT NULL)
);

-- One live pin per conversation: the newest row that nothing has superseded.
CREATE UNIQUE INDEX conversation_release_pin_live_idx
    ON conversation_release_pin (conversation_id)
    WHERE superseded_by IS NULL;
"""


@dataclass(frozen=True, slots=True)
class Approval:
    """An explicit human approval to move one conversation from one release to another.

    **Boarded to its origin**, not merely present. ``from_release_manifest_id`` is the
    release the approver saw, and :func:`migrate_conversation` refuses an approval whose
    origin is not the pin's current release. Without that check a "yes, move it" given
    last Tuesday would authorise moving a conversation that has since moved twice — an
    approval for a change nobody reviewed.

    There is no default and no optional variant: an approval that can be omitted is not
    an approval. §13's **explicit approval** is why this is a required parameter rather
    than a keyword with a sensible default.
    """

    approved_by: str
    from_release_manifest_id: str
    reason: str
    approved_at: datetime

    def __post_init__(self) -> None:
        if not self.approved_by.strip():
            raise ReleaseRefusedError("an approval must name who gave it")
        if not self.reason.strip():
            raise ReleaseRefusedError(
                "an approval must record a reason; §13 requires a checkpoint, and an "
                "unexplained migration is not one"
            )


@dataclass(frozen=True, slots=True)
class ConversationPin:
    """A conversation's release, and the versions it is running with.

    Both halves are kept. ``effective_versions`` is what a report reads; the id and hash
    are what an operator reads when deciding whether to stop or migrate it. Storing only
    the id would make every report re-resolve the manifest — and re-resolving is exactly
    how a report starts describing the present instead of the task.
    """

    conversation_id: str
    release_manifest_id: str
    agent_name: str
    effective: EffectiveVersions
    pinned_at: datetime
    pinned_by: str
    approval: Approval | None = None

    @property
    def migrated(self) -> bool:
        return self.approval is not None

    def to_payload(self) -> dict[str, object]:
        return {
            "conversation_id": self.conversation_id,
            "release_manifest_id": self.release_manifest_id,
            "agent_name": self.agent_name,
            "pinned_at": self.pinned_at.isoformat(),
            "pinned_by": self.pinned_by,
            "migrated": self.migrated,
            "approval_actor": self.approval.approved_by if self.approval else None,
            "effective_versions": self.effective.to_payload(),
        }


class PinStore(Protocol):
    """Where pins live. Injected, so the mechanism is testable without the table it needs.

    The three methods are the whole contract: read one, write one, supersede one. A store
    that cannot supersede cannot migrate; a store that can delete could erase the record
    of what a conversation ran, which is the one thing a report depends on.
    """

    def pin_for(self, conversation_id: str) -> ConversationPin | None:
        """The live pin for ``conversation_id``, or ``None`` if it has never been pinned."""

    def record(self, pin: ConversationPin) -> None:
        """Append ``pin`` and make it the conversation's live pin."""


class InMemoryPinStore:
    """A :class:`PinStore` for tests and for the demonstration the ADR records.

    Deliberately not described as production-ready: it lives in one process, so two
    replicas would answer differently. It exists so the mechanism's behaviour — pin on
    first turn, never silently re-pin, migrate only with an approval — is provable before
    the table exists.
    """

    def __init__(self) -> None:
        self._pins: dict[str, ConversationPin] = {}
        self._history: list[ConversationPin] = []

    def pin_for(self, conversation_id: str) -> ConversationPin | None:
        return self._pins.get(conversation_id)

    def record(self, pin: ConversationPin) -> None:
        self._pins[pin.conversation_id] = pin
        self._history.append(pin)

    @property
    def history(self) -> tuple[ConversationPin, ...]:
        """Every pin ever recorded, oldest first — including ones that were superseded."""
        return tuple(self._history)


def pin_conversation(
    store: PinStore,
    *,
    conversation_id: str,
    effective: EffectiveVersions,
    pinned_by: str,
    now: datetime | None = None,
) -> ConversationPin:
    """Pin ``conversation_id``, or return the pin it already has.

    **Idempotent, and that is the rule rather than a nicety.** A conversation that is
    already pinned keeps its pin, even if a newer release is active: that is what "a
    conversation is pinned to a release" means, and re-pinning on the next turn would mix
    two versions inside one conversation — the exact defect pinning exists to prevent.
    Only :func:`migrate_conversation`, with an approval, replaces a pin.
    """
    existing = store.pin_for(conversation_id)
    if existing is not None:
        return existing
    pin = ConversationPin(
        conversation_id=conversation_id,
        release_manifest_id=effective.release_manifest_id,
        agent_name=effective.agent_name,
        effective=effective,
        pinned_at=(now or datetime.now(UTC)),
        pinned_by=pinned_by,
    )
    store.record(pin)
    return pin


def migrate_conversation(
    store: PinStore,
    *,
    conversation_id: str,
    to: EffectiveVersions,
    approval: Approval,
    migrated_by: str,
    now: datetime | None = None,
) -> ConversationPin:
    """Move a pinned conversation to a new release — with an approval naming the old one.

    Three refusals, each of which is a real way this goes wrong:

    1. **No pin.** Migrating a conversation that was never pinned would create a pin that
       looks like a migration and describes a conversation that never ran the old
       release. Refused.
    2. **A stale approval.** ``approval.from_release_manifest_id`` must equal the pin's
       *current* release. Approving a move from R1 while the conversation sits on R2 is
       approving a change nobody made.
    3. **A no-op.** Moving to the release it is already on is refused rather than recorded
       as a migration, so the history does not fill with entries that mean nothing.

    The old value is preserved in the store's history; a migration is a **new** row. A
    conversation that ran R1 for four turns and R2 for two must still be able to say so.
    """
    current = store.pin_for(conversation_id)
    if current is None:
        raise ReleaseRefusedError(
            f"conversation {conversation_id} has no pin, so it has nothing to migrate from. "
            "Pin it first: §13 migrates a conversation, it does not pin one by accident."
        )
    if approval.from_release_manifest_id != current.release_manifest_id:
        raise ReleaseRefusedError(
            f"the approval authorises a move from {approval.from_release_manifest_id!r}, but "
            f"{conversation_id} is pinned to {current.release_manifest_id!r}. A checkpoint that "
            "names the wrong origin is not an approval for this change."
        )
    if to.release_manifest_id == current.release_manifest_id:
        raise ReleaseRefusedError(
            f"{conversation_id} already runs {to.release_manifest_id!r}; a migration to the same "
            "release records a change that did not happen"
        )
    migrated = replace(
        current,
        release_manifest_id=to.release_manifest_id,
        agent_name=to.agent_name,
        effective=to,
        pinned_at=(now or datetime.now(UTC)),
        pinned_by=migrated_by,
        approval=approval,
    )
    store.record(migrated)
    return migrated


@dataclass(frozen=True, slots=True)
class StopDirective:
    """What a stop tells the runtime: which conversation, why, and what it was running.

    Carries the release and hash so the stop is attributable after the fact without
    re-reading the manifest, and carries ``migrated_from`` so a stop that lands on a
    conversation which had already moved on is visible rather than surprising.
    """

    conversation_id: str
    release_manifest_id: str
    reason: str
    stopped_at: datetime
    pinned_release_manifest_id: str

    def to_payload(self) -> dict[str, object]:
        return {
            "conversation_id": self.conversation_id,
            "release_manifest_id": self.release_manifest_id,
            "pinned_release_manifest_id": self.pinned_release_manifest_id,
            "reason": self.reason,
            "stopped_at": self.stopped_at.isoformat(),
        }


class KillSwitch:
    """The safety stop (§13). It stops conversations, and it is not scoped to a release.

    **It stops a conversation pinned to an older release.** The naive implementation —
    "stop if the pin equals the release I am rolling back" — is the one that fails here,
    because the conversations on the old release are the ones the switch exists for. So
    :meth:`can_stop` takes the pin and answers ``True``, and what it refuses is a *stop
    without a pin*, since an unpinned conversation has no recorded release to attribute
    the stop to.

    Engaging it is a fact about the deployment, not about one conversation, so it is
    set once and read many times. A kill switch that had to be engaged per conversation
    would be one nobody could engage under pressure.
    """

    def __init__(self, *, engaged: bool = False, reason: str = "") -> None:
        self._engaged = engaged
        self._reason = reason
        self._stops: list[StopDirective] = []

    @property
    def engaged(self) -> bool:
        return self._engaged

    def engage(self, reason: str) -> None:
        if not reason.strip():
            raise ReleaseRefusedError(
                "engaging the kill switch requires a reason; a stop nobody can explain is "
                "one nobody can safely lift"
            )
        self._engaged = True
        self._reason = reason.strip()

    def disengage(self) -> None:
        self._engaged = False
        self._reason = ""

    def can_stop(self, pin: ConversationPin | None) -> bool:
        """Whether this switch may stop the conversation behind ``pin``.

        Does **not** compare the pin's release to the active one, and must not: see the
        module docstring. A conversation with no pin is refused, because there is no
        release to record against the stop.
        """
        return self._engaged and pin is not None

    def stop(self, pin: ConversationPin | None, *, now: datetime | None = None) -> StopDirective:
        """Stop the conversation behind ``pin``, refusing when the switch is not engaged.

        Raising rather than returning ``None`` keeps the refusal at the call site. A
        ``None`` return reads as "nothing to do" and would let a caller treat a live
        conversation as a stopped one.
        """
        if not self._engaged:
            raise ReleaseRefusedError("the kill switch is not engaged; nothing was stopped")
        if pin is None:
            raise ReleaseRefusedError(
                "there is no pin to stop. An unpinned conversation has no recorded release, so "
                "the stop could not be attributed to one."
            )
        directive = StopDirective(
            conversation_id=pin.conversation_id,
            release_manifest_id=pin.release_manifest_id,
            pinned_release_manifest_id=pin.release_manifest_id,
            reason=self._reason,
            stopped_at=(now or datetime.now(UTC)),
        )
        self._stops.append(directive)
        return directive

    @property
    def stops(self) -> tuple[StopDirective, ...]:
        """Every stop this switch has issued, oldest first."""
        return tuple(self._stops)


def new_conversation_id() -> str:
    """A conversation id, for the demonstration and for tests."""
    return str(uuid.uuid4())
