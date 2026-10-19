"""Canary cohorts and rollback routing, from the registry table and nothing else.

§13: *"canary cohorts and rollback routing can be done from a simple registry table; a separate
platform is not required"* — a separate platform is not required. So there is no flag service and no rollout
table in this suite either: every test builds plain ``RegisteredRelease`` rows and real
manifests, exactly what the registry stores, and reads the router's answer.

Three properties get most of the space here, because each of them is a way this goes wrong:

**The bucket is a hash, and it has to be stable across processes.** ``canary_bucket`` is
``sha256``, not ``hash()`` — whose string hashing is salted per interpreter — and not
``random``. A bucket that changed on restart would move a conversation between cohorts
mid-flight, which is the mid-conversation version change §13's pinning exists to prevent.
``test_the_bucket_is_the_same_in_another_process`` runs the function in two interpreters with
two different ``PYTHONHASHSEED`` values, which is the only way to test that without trusting
the source.

**The canary takes new conversations, never a pinned one.** A pinned conversation is routed to
its pin whatever the cohorts say. That is what stops a rollout from mixing two versions inside
one transcript, and it is why a pinned conversation whose release has since been rolled back is
*reported* (``pinned_rolled_back``) rather than moved.

**A rollback is a row, not a flag.** The last test reads the same registry twice with one event
added and asserts the traffic went home; nothing else about the deployment changes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from release_fixtures import (
    RELEASE_COMPONENT,
    REPO_ROOT,
    build_test_manifest,
    with_versions,
)

from referraleasy_release_registry.canary import (
    BUCKETS,
    CANARY_COHORT,
    ROLLED_BACK_COHORT,
    STABLE_COHORT,
    RolloutTarget,
    canary_bucket,
    rollout_targets,
    route,
)
from referraleasy_release_registry.effective import effective_versions
from referraleasy_release_registry.errors import ReleaseRefusedError
from referraleasy_release_registry.pinning import InMemoryPinStore, KillSwitch, pin_conversation
from referraleasy_release_registry.store import RegisteredRelease
from referraleasy_release_registry.versions import load_ledger

pytestmark = pytest.mark.contract

PACKAGE_SRC = REPO_ROOT / "packages" / "release-registry" / "src"
LEDGER_PATH = REPO_ROOT / "registry" / "versions.json"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
AGENT = "provider-matching"

#: Fixed ids, so every assertion below is exact rather than a property of a random sample.
CONVERSATIONS = tuple(f"00000000-0000-4000-8000-{index:012d}" for index in range(1, 2001))


def registered(
    release_manifest_id: str,
    *,
    status: str = "activated",
    component: str = RELEASE_COMPONENT,
) -> RegisteredRelease:
    return RegisteredRelease(
        release_code=release_manifest_id,
        component=component,
        version="0.1.0",
        environment="dev",
        status=status,
        status_actor="release-agent@example.test",
        status_at=NOW,
        manifest_hash="0" * 64,
    )


def json_round_trip(value) -> bool:
    """``to_payload`` is JSON primitives, not a live object a report could hold."""
    payload = value.to_payload()
    return json.loads(json.dumps(payload)) == payload


@pytest.fixture(scope="module")
def stable():
    return build_test_manifest()


@pytest.fixture(scope="module")
def canary_release():
    """The canary is a *different release*, not a different label on the same one.

    Two cohorts of one artifact would share a ``release_manifest_id`` — the artifact hash
    excludes the rollout block on purpose, because a canary moving from 5% to 25% is a
    deployment decision about an artifact and not a change *to* it. So a rollout that is meant
    to differ has to differ in content, and here it does: this release carries a new prompt
    version. Two manifests under one release id would collide on ``UNIQUE (release_id)`` in the
    registry, and the assertion at the end of this fixture is what keeps the tests below from
    silently comparing a release with itself.
    """
    ledger = with_versions(load_ledger(LEDGER_PATH), prompts={"provider_matching": "1.1.0"})
    manifest = build_test_manifest(cohort=CANARY_COHORT, cohort_percentage=0.05, ledger=ledger)
    assert manifest.release_manifest_id.root != build_test_manifest().release_manifest_id.root
    return manifest


def make_targets(stable, canary_release, *, canary_status: str = "activated"):
    """The registry's routing table: one stable release and one 5% canary, as rows."""
    return rollout_targets(
        [
            registered(stable.release_manifest_id.root),
            registered(canary_release.release_manifest_id.root, status=canary_status),
        ],
        {
            stable.release_manifest_id.root: stable,
            canary_release.release_manifest_id.root: canary_release,
        },
    )


@pytest.fixture
def targets(stable, canary_release):
    return make_targets(stable, canary_release)


# ── the bucket ─────────────────────────────────────────────────────────────────────


def test_the_bucket_is_inside_the_bucket_space_and_stable_within_a_process() -> None:
    """Every id lands in ``[0, 100)``, and the answer does not move between two calls.

    The spread matters as much as the range: a function that returned ``0`` for everything
    would satisfy "inside the space" and would put every conversation in every canary.
    """
    buckets = [canary_bucket(conversation_id) for conversation_id in CONVERSATIONS]
    assert all(0 <= bucket < BUCKETS for bucket in buckets)
    assert canary_bucket(CONVERSATIONS[0]) == buckets[0]
    assert len(set(buckets)) > BUCKETS * 0.9, (
        f"only {len(set(buckets))} of {BUCKETS} buckets are reachable; the bucket is not a hash"
    )


def test_the_bucket_is_the_same_in_another_process(tmp_path: Path) -> None:
    """The property that makes it a hash rather than ``hash()`` or ``random``.

    Python salts string hashing per process (``PYTHONHASHSEED``), so an implementation built on
    ``hash()`` would give a conversation a different bucket after every restart — moving it
    into or out of the canary mid-conversation. Two interpreters with two seeds is the only way
    to see that from a test.
    """
    program = (
        "from referraleasy_release_registry.canary import canary_bucket\n"
        f"print(canary_bucket({CONVERSATIONS[0]!r}), canary_bucket({CONVERSATIONS[1]!r}))\n"
    )
    outputs = []
    for seed in ("0", "1"):
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(tmp_path),
            env={
                **os.environ,
                "PYTHONHASHSEED": seed,
                "PYTHONPATH": str(PACKAGE_SRC),
            },
        )
        outputs.append(result.stdout.strip())
    assert outputs[0] == outputs[1], (
        f"the bucket moved between processes ({outputs}); it must not depend on the "
        "interpreter's hash seed"
    )
    expected = " ".join(
        str(canary_bucket(conversation_id)) for conversation_id in CONVERSATIONS[:2]
    )
    assert outputs[0] == expected


def test_a_salt_draws_a_different_sample() -> None:
    """Successive canaries must not hit the same conversations, or the cohort is not a sample.

    Each canary salts with its own manifest id. Without that, every canary is carried by the
    same small group of conversations and the rollout measures nothing new.
    """
    salt_one, salt_two = "rel-20261015-aaaaaaa", "rel-20261101-bbbbbbb"
    first = {c for c in CONVERSATIONS if canary_bucket(c, salt=salt_one) < 5}
    second = {c for c in CONVERSATIONS if canary_bucket(c, salt=salt_two) < 5}
    assert 0.02 < len(first) / len(CONVERSATIONS) < 0.09
    assert 0.02 < len(second) / len(CONVERSATIONS) < 0.09
    assert len(first & second) < len(first) * 0.5, (
        "two 5% samples drawn with different salts share most of their members, so the salt "
        "is not reaching the hash"
    )


def test_a_five_percent_canary_takes_about_five_percent() -> None:
    """The share, and the reason the ids are fixed: an exact assertion that cannot flake."""
    inside = [conversation for conversation in CONVERSATIONS if canary_bucket(conversation) < 5]
    ratio = len(inside) / len(CONVERSATIONS)
    assert 0.03 < ratio < 0.08, ratio
    assert 0 < len(inside) < len(CONVERSATIONS)
    # Ids increasing by one share a long prefix, so an implementation that hashed the prefix —
    # or ordered by the id — would put them all on the same side of the line.
    assert set(inside) != set(CONVERSATIONS[: len(inside)])
    with pytest.raises(ReleaseRefusedError):
        canary_bucket("   ")


# ── the routing table ──────────────────────────────────────────────────────────────


def test_percentage_rounds_rather_than_truncating() -> None:
    """``0.29 * 100`` is ``28.999999999999996``; truncation would route 28 buckets, not 29.

    Small, and it is the difference between a 5% canary and a 4% one, which is a difference
    nobody would notice until the metric did not move.
    """

    def target(share: float) -> RolloutTarget:
        return RolloutTarget(
            release_manifest_id="rel-20261015-aaaaaaa",
            cohort=CANARY_COHORT,
            share=share,
            active=True,
            rolled_back=False,
        )

    assert target(0.29).percentage == 29
    assert target(0.05).percentage == 5
    assert target(1.0).percentage == BUCKETS
    assert target(0.0).percentage == 0
    assert target(-1.0).percentage == 0 and target(9.0).percentage == BUCKETS


def test_the_table_is_the_release_level_rows_only(stable, canary_release) -> None:
    """Eight copies of one rollout block would make "which cohort is this release in" an
    eight-answer question."""
    rows = [
        registered(stable.release_manifest_id.root),
        registered(stable.release_manifest_id.root, component="agent-provider_matching"),
        registered(canary_release.release_manifest_id.root),
    ]
    built = rollout_targets(
        rows,
        {
            stable.release_manifest_id.root: stable,
            canary_release.release_manifest_id.root: canary_release,
        },
    )
    assert [t.release_manifest_id for t in built] == [
        canary_release.release_manifest_id.root,
        stable.release_manifest_id.root,
    ], "smallest share first, so overlapping canaries resolve to the smaller one"


def test_a_release_with_no_manifest_is_refused_rather_than_skipped(stable) -> None:
    """A routing table that quietly omits a release is a router that sends its traffic
    elsewhere — the failure that looks like success."""
    with pytest.raises(ReleaseRefusedError) as refusal:
        rollout_targets([registered("rel-20260101-missing")], {})
    assert "rel-20260101-missing" in str(refusal.value)
    with pytest.raises(ReleaseRefusedError):
        rollout_targets([], {})


def test_a_rollback_in_the_manifest_alone_stops_the_traffic(canary_release) -> None:
    """A rollback is recorded twice — as an event and in the rollout block — by two actors.

    A rollback only one of them knows about must still stop the traffic, so both are read.
    """
    rolled_back = build_test_manifest(cohort=ROLLED_BACK_COHORT, cohort_percentage=1.0)
    built = rollout_targets(
        [registered(rolled_back.release_manifest_id.root)],
        {rolled_back.release_manifest_id.root: rolled_back},
    )
    assert built[0].rolled_back and not built[0].routable
    assert built[0].cohort == ROLLED_BACK_COHORT
    with pytest.raises(ReleaseRefusedError) as refusal:
        route(conversation_id=CONVERSATIONS[0], targets=built)
    assert "no stable release" in str(refusal.value)
    assert ROLLED_BACK_COHORT in str(refusal.value), (
        "the refusal says what it *is*, not merely that it is inactive"
    )


# ── routing one conversation ───────────────────────────────────────────────────────


def test_a_new_conversation_lands_in_the_canary_or_on_the_stable_release(
    targets,
) -> None:
    """Both answers exist, and the canary's share decides which one a given conversation gets.

    The split is asserted by counting over a fixed set of ids rather than by picking one, so
    the test says something about the share and not about a single lucky bucket.
    """
    canary_ids = [
        c for c in CONVERSATIONS if route(conversation_id=c, targets=targets).reason == "canary"
    ]
    stable_ids = [
        c for c in CONVERSATIONS if route(conversation_id=c, targets=targets).reason == "stable"
    ]
    assert len(canary_ids) + len(stable_ids) == len(CONVERSATIONS)
    assert 0.02 < len(canary_ids) / len(CONVERSATIONS) < 0.09
    picked = route(conversation_id=canary_ids[0], targets=targets)
    assert picked.cohort == CANARY_COHORT
    assert picked.bucket < 5
    assert picked.release_manifest_id == targets[0].release_manifest_id
    assert route(conversation_id=stable_ids[0], targets=targets).cohort == STABLE_COHORT
    assert json_round_trip(picked)


def test_a_rollback_event_returns_the_traffic_to_the_stable_release(stable, canary_release) -> None:
    """§13's *"rollback routing"*, shown by reading the same registry twice.

    Nothing about the canary manifest changes between the two readings: one ``release_event``
    row does, and that is the whole mechanism. This is the test that fails if a rollback is
    implemented as anything other than "the latest event wins".
    """
    activated = make_targets(stable, canary_release)
    rolled_back = make_targets(stable, canary_release, canary_status="rolled_back")
    canary_ids = [
        c
        for c in CONVERSATIONS[:400]
        if route(conversation_id=c, targets=activated).reason == "canary"
    ]
    assert canary_ids, "the fixture must have a canary taking traffic"
    for conversation_id in canary_ids:
        before = route(conversation_id=conversation_id, targets=activated)
        after = route(conversation_id=conversation_id, targets=rolled_back)
        assert before.reason == "canary"
        assert after.reason == "stable"
        assert after.release_manifest_id == stable.release_manifest_id.root
        assert after.cohort == STABLE_COHORT


def test_a_staged_or_inactive_canary_takes_no_traffic(stable, canary_release) -> None:
    """A release that was built but never activated is not a rollout."""
    staged = make_targets(stable, canary_release, canary_status="staged")
    assert {route(conversation_id=c, targets=staged).reason for c in CONVERSATIONS[:300]} == {
        "stable"
    }


def test_a_pinned_conversation_stays_where_it_is(targets, stable, canary_release) -> None:
    """The line that keeps a rollout from mixing two versions inside one conversation.

    The conversation is pinned to the stable release; a 5% canary is active. Its next turn
    goes to its pin, and the router says why — for every conversation, not only the ones the
    bucket would have sent elsewhere.
    """
    store = InMemoryPinStore()
    conversations = [
        c
        for c in CONVERSATIONS[:400]
        if route(conversation_id=c, targets=targets).reason == "canary"
    ][:3]
    assert conversations
    for conversation_id in conversations:
        pin = pin_conversation(
            store,
            conversation_id=conversation_id,
            effective=effective_versions(stable, agent_name=AGENT),
            pinned_by="agent-runtime",
            now=NOW,
        )
        decision = route(conversation_id=conversation_id, targets=targets, pinned=pin)
        assert decision.reason == "pinned"
        assert decision.pinned is True
        assert decision.release_manifest_id == stable.release_manifest_id.root
        assert decision.pinned_rolled_back is False


def test_a_pinned_conversation_whose_release_was_rolled_back_is_reported_not_moved(
    stable, canary_release
) -> None:
    """Stopping or migrating it is the pinning module's business, and doing it here would be a
    silent version change — the defect pinning exists to prevent.

    So the router keeps the conversation where it is and *says* the release was pulled.
    """
    store = InMemoryPinStore()
    pin = pin_conversation(
        store,
        conversation_id=CONVERSATIONS[0],
        effective=effective_versions(canary_release, agent_name=AGENT),
        pinned_by="agent-runtime",
    )
    after_rollback = make_targets(stable, canary_release, canary_status="rolled_back")
    decision = route(conversation_id=CONVERSATIONS[0], targets=after_rollback, pinned=pin)
    assert decision.reason == "pinned"
    assert decision.release_manifest_id == canary_release.release_manifest_id.root
    assert decision.pinned_rolled_back is True


def test_the_kill_switch_outranks_the_pin(targets, stable) -> None:
    """§13 gives the switch authority over conversations pinned to older releases.

    A check ordered after the pin would be a check the pin could veto, so the switch is asked
    first — and the routing record still names the release the conversation was pinned to, so
    the stop is attributable.
    """
    store = InMemoryPinStore()
    pin = pin_conversation(
        store,
        conversation_id=CONVERSATIONS[0],
        effective=effective_versions(stable, agent_name=AGENT),
        pinned_by="agent-runtime",
    )
    switch = KillSwitch()
    switch.engage("guardrail classifier regression under investigation")
    decision = route(
        conversation_id=CONVERSATIONS[0], targets=targets, pinned=pin, kill_switch=switch
    )
    assert decision.reason == "kill_switch"
    assert decision.stopped is True and decision.pinned is True
    assert decision.release_manifest_id == stable.release_manifest_id.root
    assert 0 <= decision.bucket < BUCKETS, (
        "the bucket is recorded even when nothing branches on it, so the cohort a stopped "
        "conversation would have been in is answerable after an incident"
    )
    assert json_round_trip(decision)


def test_a_stopped_conversation_with_no_pin_has_no_attribution_to_claim(targets) -> None:
    """Nothing to stop, and the record says so with empty fields rather than a guess."""
    switch = KillSwitch(engaged=True, reason="incident")
    decision = route(conversation_id=CONVERSATIONS[0], targets=targets, kill_switch=switch)
    assert decision.reason == "kill_switch" and decision.pinned is False
    assert decision.release_manifest_id == "" and decision.cohort == ""


def test_no_targets_at_all_is_a_refusal_not_a_default(canary_release) -> None:
    """A router that returned "no release" would let a caller treat it as "use the default",
    and there is no default: §13 routes to a registered, activated release."""
    with pytest.raises(ReleaseRefusedError) as refusal:
        route(conversation_id=CONVERSATIONS[0], targets=())
    assert "before it can take traffic" in str(refusal.value)

    staged_only = rollout_targets(
        [registered(canary_release.release_manifest_id.root, status="staged")],
        {canary_release.release_manifest_id.root: canary_release},
    )
    with pytest.raises(ReleaseRefusedError) as no_stable:
        route(conversation_id=CONVERSATIONS[0], targets=staged_only)
    assert "nowhere to route" in str(no_stable.value)
    assert canary_release.release_manifest_id.root in str(no_stable.value)
