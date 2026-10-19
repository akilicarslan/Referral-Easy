"""Conversations pinned to a release, the kill switch, and the approval a migration needs.

§13, in three sentences, and each one has a test below that fails if it is deleted:

1. *"Conversation release'e sabitlenir"* — a conversation is pinned to a release.
2. *"the safety kill switch can stop a conversation pinned to an older release too"* — the kill
   switch can stop a conversation pinned to an **older** release.
3. *"migrating to a new version requires explicit approval and a checkpoint"* — migrating it to a new release
   requires explicit approval and a checkpoint.

Sentence 2 is the one a well-meaning implementation gets wrong. Stopping only conversations on
the *current* release is the tidy reading, and it leaves exactly the conversations the switch
exists for — the ones stuck on a version you moved past because it misbehaved — still running.
:meth:`KillSwitch.can_stop` therefore answers by the pin's identity and never by its age, and
``test_the_switch_stops_a_conversation_on_an_older_release`` arranges that case explicitly:
one conversation pinned three releases back, stopped by a switch engaged today.

The storage gap is a finding, not a detail
------------------------------------------
There is no table for a conversation pin. ``release_registry`` records releases and
``release_event`` records what happened to a release; neither has a conversation to hang a pin
on, and this package may not add a migration. So the mechanism is implemented against an
injected :class:`PinStore` and ``pinning.PIN_TABLE_DDL`` is the exact DDL the missing migration
would need — recorded in ``docs/adr/ADR-0036`` as outstanding, with :class:`InMemoryPinStore`
described as what it is rather than as production-ready.

The pin's *value*, by contrast, is complete and is asserted as such: everything a report needs
is copied into the record at task start, so reading a report later cannot re-resolve a label
into the present (``test_the_record_is_a_copy_and_carries_no_label``).
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest
from release_fixtures import BUILT_AT, build_test_manifest

from referraleasy_release_registry.effective import (
    LATEST_LABEL,
    EffectiveVersions,
    effective_versions,
    resolve_release,
)
from referraleasy_release_registry.errors import MissingArtifactError, ReleaseRefusedError
from referraleasy_release_registry.pinning import (
    PIN_TABLE_DDL,
    Approval,
    ConversationPin,
    InMemoryPinStore,
    KillSwitch,
    migrate_conversation,
    pin_conversation,
)

pytestmark = pytest.mark.contract

CONVERSATION = "00000000-0000-4000-8000-000000000001"
AGENT = "provider-matching"
APPROVED_AT = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)


@pytest.fixture
def release_one(manifest):
    return manifest


@pytest.fixture
def release_two():
    """A second release of the same tree on a later day: another id, another ``created_at``."""
    return build_test_manifest(built_at=BUILT_AT + timedelta(days=1))


@pytest.fixture
def running(manifest) -> EffectiveVersions:
    return effective_versions(manifest, agent_name=AGENT)


def approval_for(release_id: str, **overrides) -> Approval:
    fields = {
        "approved_by": "on-call@example.test",
        "from_release_manifest_id": release_id,
        "reason": "the previous prompt mis-classified network status; migrating this thread",
        "approved_at": APPROVED_AT,
    }
    return Approval(**{**fields, **overrides})


# ── what a task carries ─────────────────────────────────────────────────────────────


def test_the_record_carries_every_version_a_report_needs(manifest, running) -> None:
    """§13's *"runtime tasks carry their effective versions"*, field by field.

    Every one of these is read from the manifest the task actually started under, so a report
    can be answered months later from the task record alone.
    """
    agent = next(a for a in manifest.agents if a.agent_name == AGENT)
    assert running.release_manifest_id == manifest.release_manifest_id.root
    assert running.agent_name == AGENT
    assert running.agent_version == agent.version
    assert running.agent_hash == agent.agent_hash
    assert running.prompt_version == manifest.prompts[AGENT]
    assert running.instruction_hash == agent.instruction_hash
    assert running.tool_versions == dict(agent.tool_description_hashes)
    assert running.skill_versions == dict(agent.skill_hashes)
    assert running.sub_agent_manifest_hash == agent.sub_agent_manifest_hash
    assert running.model_provider == manifest.provider.agent_model_provider
    assert running.model_id == manifest.provider.agent_model_id
    assert running.guardrail_classifier_hash == manifest.guardrail_policy.classifier_hash
    assert running.knowledge_version == manifest.rag.knowledge_version
    assert running.git_sha == manifest.source.git_sha
    assert running.cohort == manifest.rollout.cohort
    assert len(dataclasses.fields(running)) == 16


def test_the_record_is_a_copy_and_carries_no_label(manifest, release_two, running) -> None:
    """``latest`` is refused, and nothing in the record is a pointer.

    Two releases of one tree, resolved for the same agent, differ in the values that are
    per-release — and neither record changed when the other was built. A record holding
    ``latest`` would have moved with it.
    """
    later = effective_versions(release_two, agent_name=AGENT)
    assert running.release_manifest_id != later.release_manifest_id
    assert running.manifest_hash != later.manifest_hash
    payload = running.to_payload()
    assert LATEST_LABEL not in json.dumps(payload)
    assert json.loads(json.dumps(payload)) == payload, "the record is JSON primitives"
    assert payload["cohort"] in {"stable", "canary", "rolled_back"}, (
        "the cohort is a resolved cohort, not a number that moved"
    )


def test_an_unknown_agent_is_refused_rather_than_half_resolved(manifest) -> None:
    """§13's failure mode is an omission that is invisible at report time, so it is refused here."""
    with pytest.raises(MissingArtifactError) as refusal:
        effective_versions(manifest, agent_name="provider-matching-shadow")
    assert AGENT in str(refusal.value)


def test_the_label_latest_is_not_a_release(manifest, release_two) -> None:
    """§13's *"the ``latest`` label is not enough for a report"*, enforced at resolution."""
    releases = {
        manifest.release_manifest_id.root: manifest,
        release_two.release_manifest_id.root: release_two,
    }
    with pytest.raises(ReleaseRefusedError) as refusal:
        resolve_release(LATEST_LABEL, releases)
    assert "not a release" in str(refusal.value)
    assert manifest.release_manifest_id.root in str(refusal.value), (
        "the refusal names the releases that can be pinned to, so it is actionable"
    )
    assert resolve_release(manifest.release_manifest_id.root, releases) is manifest
    with pytest.raises(ReleaseRefusedError):
        resolve_release("", releases)
    with pytest.raises(ReleaseRefusedError):
        resolve_release("rel-20260101-abcdefg", releases)


# ── pinning ────────────────────────────────────────────────────────────────────────


def test_a_conversation_is_pinned_on_its_first_turn_and_keeps_that_pin(
    manifest, release_two
) -> None:
    """The defect pinning exists to prevent: two versions inside one conversation.

    The second call passes the *newer* release's effective versions — a turn that happened
    after the release moved — and the pin does not budge. Re-pinning here would mean a
    transcript explaining itself with two prompts and one version number.
    """
    store = InMemoryPinStore()
    first = pin_conversation(
        store,
        conversation_id=CONVERSATION,
        effective=effective_versions(manifest, agent_name=AGENT),
        pinned_by="agent-runtime",
        now=APPROVED_AT,
    )
    assert first.release_manifest_id == manifest.release_manifest_id.root
    assert not first.migrated

    second = pin_conversation(
        store,
        conversation_id=CONVERSATION,
        effective=effective_versions(release_two, agent_name=AGENT),
        pinned_by="agent-runtime",
        now=APPROVED_AT + timedelta(hours=1),
    )
    assert second is first
    assert second.release_manifest_id == manifest.release_manifest_id.root
    assert len(store.history) == 1, "an idempotent pin is not a second row"


def test_pins_are_per_conversation(manifest) -> None:
    store = InMemoryPinStore()
    other = "00000000-0000-4000-8000-000000000002"
    effective = effective_versions(manifest, agent_name=AGENT)
    first = pin_conversation(
        store, conversation_id=CONVERSATION, effective=effective, pinned_by="runtime"
    )
    second = pin_conversation(
        store, conversation_id=other, effective=effective, pinned_by="runtime"
    )
    assert first is not second
    assert store.pin_for(other) is second
    assert store.pin_for("00000000-0000-4000-8000-000000000003") is None


def test_the_pin_records_who_pinned_it_and_when(manifest) -> None:
    """Attribution, not decoration: "what did this conversation run" has an owner."""
    store = InMemoryPinStore()
    pin = pin_conversation(
        store,
        conversation_id=CONVERSATION,
        effective=effective_versions(manifest, agent_name=AGENT),
        pinned_by="agent-runtime",
        now=APPROVED_AT,
    )
    assert pin.pinned_at == APPROVED_AT
    assert pin.pinned_by == "agent-runtime"
    payload = pin.to_payload()
    assert payload["migrated"] is False
    assert payload["approval_actor"] is None
    assert payload["conversation_id"] == CONVERSATION
    assert json.loads(json.dumps(payload)) == payload


# ── migration ──────────────────────────────────────────────────────────────────────


def test_migrating_needs_an_approval_that_names_the_origin(manifest, release_two) -> None:
    """§13's *"explicit approval and a checkpoint"*: three refusals, each a way this really
    goes wrong."""
    store = InMemoryPinStore()
    with pytest.raises(ReleaseRefusedError) as no_pin:
        migrate_conversation(
            store,
            conversation_id=CONVERSATION,
            to=effective_versions(release_two, agent_name=AGENT),
            approval=approval_for(manifest.release_manifest_id.root),
            migrated_by="on-call@example.test",
        )
    assert "no pin" in str(no_pin.value)

    pin_conversation(
        store,
        conversation_id=CONVERSATION,
        effective=effective_versions(manifest, agent_name=AGENT),
        pinned_by="agent-runtime",
        now=APPROVED_AT,
    )
    with pytest.raises(ReleaseRefusedError) as stale:
        migrate_conversation(
            store,
            conversation_id=CONVERSATION,
            to=effective_versions(release_two, agent_name=AGENT),
            approval=approval_for(release_two.release_manifest_id.root),
            migrated_by="on-call@example.test",
        )
    assert "wrong origin" in str(stale.value)

    with pytest.raises(ReleaseRefusedError) as noop:
        migrate_conversation(
            store,
            conversation_id=CONVERSATION,
            to=effective_versions(manifest, agent_name=AGENT),
            approval=approval_for(manifest.release_manifest_id.root),
            migrated_by="on-call@example.test",
        )
    assert "same release" in str(noop.value)
    assert len(store.history) == 1, "every refusal left the store untouched"


def test_an_approval_without_an_actor_or_a_reason_does_not_exist(manifest) -> None:
    """A checkpoint that names nothing is not a checkpoint, so it cannot be constructed."""
    with pytest.raises(ReleaseRefusedError):
        approval_for(manifest.release_manifest_id.root, approved_by="  ")
    with pytest.raises(ReleaseRefusedError) as refusal:
        approval_for(manifest.release_manifest_id.root, reason="")
    assert "reason" in str(refusal.value)


def test_a_migration_keeps_both_releases_in_the_history(manifest, release_two) -> None:
    """A conversation that ran R1 for four turns and R2 for two must still be able to say so.

    The old pin is *superseded*, never overwritten: a store that could only hold one value
    could not answer "what did this conversation run" for the turns before the migration.
    """
    store = InMemoryPinStore()
    pin_conversation(
        store,
        conversation_id=CONVERSATION,
        effective=effective_versions(manifest, agent_name=AGENT),
        pinned_by="agent-runtime",
        now=APPROVED_AT,
    )
    migrated = migrate_conversation(
        store,
        conversation_id=CONVERSATION,
        to=effective_versions(release_two, agent_name=AGENT),
        approval=approval_for(manifest.release_manifest_id.root),
        migrated_by="on-call@example.test",
        now=APPROVED_AT + timedelta(days=1),
    )
    assert migrated.migrated
    assert migrated.release_manifest_id == release_two.release_manifest_id.root
    assert migrated.approval is not None
    assert migrated.approval.approved_by == "on-call@example.test"
    assert store.pin_for(CONVERSATION) is migrated
    assert [pin.release_manifest_id for pin in store.history] == [
        manifest.release_manifest_id.root,
        release_two.release_manifest_id.root,
    ], "the history is append-only"
    assert migrated.effective is not None
    assert migrated.effective.release_manifest_id == release_two.release_manifest_id.root


def test_a_migrated_conversation_does_not_snap_back(manifest, release_two) -> None:
    """After a migration the pin is the *new* one; a later turn must not re-pin it."""
    store = InMemoryPinStore()
    pin_conversation(
        store,
        conversation_id=CONVERSATION,
        effective=effective_versions(manifest, agent_name=AGENT),
        pinned_by="agent-runtime",
    )
    migrated = migrate_conversation(
        store,
        conversation_id=CONVERSATION,
        to=effective_versions(release_two, agent_name=AGENT),
        approval=approval_for(manifest.release_manifest_id.root),
        migrated_by="on-call@example.test",
    )
    again = pin_conversation(
        store,
        conversation_id=CONVERSATION,
        effective=effective_versions(manifest, agent_name=AGENT),
        pinned_by="agent-runtime",
    )
    assert again is migrated
    assert len(store.history) == 2


# ── the kill switch ────────────────────────────────────────────────────────────────


def test_the_switch_stops_a_conversation_on_an_older_release(manifest, release_two) -> None:
    """§13's sentence that reads as a contradiction and is the whole design.

    Two conversations: one pinned three releases back (a *third* manifest, a week earlier) and
    one pinned to today's. The switch was engaged today, and it must stop both — each stop
    recording the release that conversation was actually running.
    """
    older = build_test_manifest(built_at=BUILT_AT - timedelta(days=7))
    latest = build_test_manifest(built_at=BUILT_AT + timedelta(days=1))
    store = InMemoryPinStore()
    stuck = pin_conversation(
        store,
        conversation_id=CONVERSATION,
        effective=effective_versions(older, agent_name=AGENT),
        pinned_by="agent-runtime",
    )
    current = pin_conversation(
        store,
        conversation_id="00000000-0000-4000-8000-000000000002",
        effective=effective_versions(latest, agent_name=AGENT),
        pinned_by="agent-runtime",
    )
    assert stuck.release_manifest_id != current.release_manifest_id

    switch = KillSwitch()
    assert not switch.can_stop(stuck), "an unengaged switch stops nothing"
    switch.engage("guardrail classifier regression under investigation")
    assert switch.can_stop(stuck), (
        "the switch must stop a pin on an older release; the naive implementation compares the "
        "pin to the release being rolled back and leaves exactly these conversations running"
    )
    assert switch.can_stop(current)

    directive = switch.stop(stuck, now=APPROVED_AT)
    assert directive.conversation_id == CONVERSATION
    assert directive.release_manifest_id == older.release_manifest_id.root
    assert directive.pinned_release_manifest_id == older.release_manifest_id.root
    assert directive.reason.startswith("guardrail classifier")
    assert directive.stopped_at == APPROVED_AT
    assert json.loads(json.dumps(directive.to_payload())) == directive.to_payload()
    assert [stop.conversation_id for stop in switch.stops] == [CONVERSATION]


def test_the_switch_refuses_the_two_ways_a_stop_could_be_wrong(manifest) -> None:
    """No reason, no engagement; no pin, no attribution.

    Raising rather than returning ``None`` keeps the refusal at the call site: a ``None``
    return reads as "nothing to do" and would let a caller treat a live conversation as stopped.
    """
    switch = KillSwitch()
    with pytest.raises(ReleaseRefusedError) as refusal:
        switch.engage("   ")
    assert "reason" in str(refusal.value)
    assert not switch.engaged
    with pytest.raises(ReleaseRefusedError) as disengaged:
        switch.stop(None)
    assert "not engaged" in str(disengaged.value)

    switch.engage("rollback")
    assert not switch.can_stop(None)
    with pytest.raises(ReleaseRefusedError) as unpinned:
        switch.stop(None)
    assert "no pin" in str(unpinned.value)
    assert switch.stops == ()
    switch.disengage()
    assert not switch.engaged
    with pytest.raises(ReleaseRefusedError):
        switch.stop(None)


def test_the_switch_is_a_deployment_fact_not_a_per_conversation_one(manifest) -> None:
    """Engaged once, read many times — nobody can engage it per conversation under pressure."""
    switch = KillSwitch(engaged=True, reason="incident 4711")
    store = InMemoryPinStore()
    stops = []
    for index in range(3):
        pin = pin_conversation(
            store,
            conversation_id=f"00000000-0000-4000-8000-00000000000{index}",
            effective=effective_versions(manifest, agent_name=AGENT),
            pinned_by="agent-runtime",
        )
        assert switch.can_stop(pin)
        stops.append(switch.stop(pin))
    assert len(switch.stops) == 3
    assert {stop.reason for stop in stops} == {"incident 4711"}


# ── the recorded storage gap ───────────────────────────────────────────────────────


def test_the_recorded_ddl_can_hold_what_the_pin_is(manifest) -> None:
    """The missing migration is written out, and this keeps it honest against the dataclass.

    A "we need a table" note is not something a reviewer can act on; the exact DDL is. This
    test is what stops the note from drifting from the value it is supposed to store — a DDL
    that lost ``conversation_id`` would still read as a plan.
    """
    store = InMemoryPinStore()
    pin = pin_conversation(
        store,
        conversation_id=CONVERSATION,
        effective=effective_versions(manifest, agent_name=AGENT),
        pinned_by="agent-runtime",
    )
    for column in (
        "conversation_id",
        "release_manifest_id",
        "agent_name",
        "effective_versions",
        "pinned_at",
        "pinned_by",
        "superseded_by",
        "approval_actor",
        "approval_reason",
    ):
        assert column in PIN_TABLE_DDL, column
    assert "CREATE TABLE conversation_release_pin" in PIN_TABLE_DDL
    assert "~ '^[0-9a-f]{64}$'" in PIN_TABLE_DDL, "the hash column keeps the registry's own check"
    assert "WHERE superseded_by IS NULL" in PIN_TABLE_DDL, (
        "one live pin per conversation is an index, not a hope: two live pins would mean two "
        "answers to what the conversation is running"
    )
    assert "superseded_by IS NULL OR approval_actor IS NOT NULL" in PIN_TABLE_DDL, (
        "a migration must name its approver; a pin created at conversation start must not"
    )
    assert pin.effective.to_payload()["release_manifest_id"] == pin.release_manifest_id
    assert isinstance(pin, ConversationPin)
