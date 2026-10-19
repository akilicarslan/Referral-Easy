"""Canary cohorts and rollback routing, from the registry table (§13).

§13 asks for canary cohorts and rollback routing to be doable **from a simple registry
table**, and says a separate platform is not required. (The sentence as written is
quoted in ``docs/adr/ADR-0037-release-registry-version-gate.md``.) So there is no flag
service, no
progressive-delivery controller and no rollout table. There is the registry, and this
module reads it.

How the registry says "canary"
------------------------------
``ReleaseManifest.rollout`` already exists in the contract — ``cohort``,
``cohort_percentage``, ``kill_switch_engaged`` — and every row of ``release_registry``
stores the full manifest. So the registry already knows, per release, which cohort it
belongs to and how much of the traffic it should take. Nothing needs to be added to say
it; the table simply has to be read.

Where a rollback lives
----------------------
In ``release_event``: a rollback is a new ``rolled_back`` row, and ``release_status``
reduces the history to each release's latest event. That is why routing needs no
rollback logic of its own — a rolled-back release is one whose status is no longer
``activated``, and :func:`rollout_targets` drops it. The test that proves it reads the
same table twice with one event added and asserts the traffic went home.

Bucketing is a hash, never a counter or a random number
-------------------------------------------------------
:func:`canary_bucket` is ``sha256(salt + conversation_id)`` modulo 100. Three properties
follow, and each of them is one of the ways this goes wrong:

* **Stable.** The same conversation lands in the same bucket on every process, in every
  replica, forever. A bucketing that used ``random`` or ``hash()`` (which is salted per
  process for strings) would move a conversation between cohorts on restart, and a
  conversation that moves mid-flight is a mid-conversation version change — what §13's
  pinning exists to prevent.
* **Not sequential.** Conversation ids are UUIDs, so ordering by id would be ordering by
  nothing; but a first-turn prefix would correlate a cohort with the database's insert
  order, and the canary would then be "the most recent users" rather than 5% of them.
* **Salted by release.** ``salt`` defaults to the canary release's own manifest id, so
  two successive canaries do not hit the same 5% of conversations. Without it, the same
  small group carries every canary and the cohort is not a sample.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from referraleasy_contracts.generated.workflow import ReleaseManifest, RolloutCohort
from referraleasy_release_registry.errors import ReleaseRefusedError
from referraleasy_release_registry.hashing import sha256_hex
from referraleasy_release_registry.manifest import rollout_facts
from referraleasy_release_registry.pinning import ConversationPin, KillSwitch
from referraleasy_release_registry.store import RELEASE_COMPONENT, RegisteredRelease

__all__ = [
    "BUCKETS",
    "CANARY_COHORT",
    "ROLLED_BACK_COHORT",
    "STABLE_COHORT",
    "RolloutTarget",
    "Routing",
    "canary_bucket",
    "rollout_targets",
    "route",
]

#: The three cohorts the contract defines, taken from the contract rather than restated.
#: ``RolloutCohort`` is an enum in ``packages/contracts/schemas/workflow.json``, so a
#: fourth cohort name is not a thing this system can express and a router that invented
#: one would never match a manifest.
STABLE_COHORT: Final = RolloutCohort.stable.value
CANARY_COHORT: Final = RolloutCohort.canary.value
#: A cohort, not an event. The registry records a rollback twice and both are kept: as a
#: ``release_event`` (what an operator did) and in the manifest's own rollout (what the
#: release *is*). Routing reads the second because it is the one stored with the artifact.
ROLLED_BACK_COHORT: Final = RolloutCohort.rolled_back.value

#: The bucket space. 100 rather than 10_000: a cohort expressed as a whole percent is what
#: an operator can reason about, and a 0.01% cohort is not a thing this project deploys.
BUCKETS: Final = 100


@dataclass(frozen=True, slots=True)
class RolloutTarget:
    """One release the router may send a conversation to, as the registry records it.

    ``share`` is the fraction of conversations the release should take, straight from
    ``Rollout.cohort_percentage`` — which the contract bounds to ``0..1``, so the CLI
    receives a fraction and this class never has to decide whether ``5`` meant 5% or 500%.
    """

    release_manifest_id: str
    cohort: str
    share: float
    active: bool
    rolled_back: bool

    @property
    def percentage(self) -> int:
        """``share`` as whole buckets, for the comparison in :func:`route`.

        Rounded rather than truncated: a manifest saying ``0.05`` must take five buckets,
        and ``5 * 0.01`` is ``0.05000000000000001`` in binary floating point — a truncation
        would drop the last bucket and quietly route 4% of a 5% canary.
        """
        return min(BUCKETS, max(0, round(self.share * BUCKETS)))

    @property
    def routable(self) -> bool:
        """Whether this target may take new conversations.

        ``active`` and ``rolled_back`` cannot both be true — ``release_status`` reduces the
        event history to one row per (release, environment) — so the conjunction is not
        doing work here. The pair is kept apart anyway because they are *reported*
        differently: a release that was never activated has not failed, and one that was
        rolled back has. Collapsing them would make the refusal message below say
        "inactive" for a release an operator had deliberately pulled.
        """
        return self.active and not self.rolled_back


def rollout_targets(
    releases: Iterable[RegisteredRelease],
    manifests: Mapping[str, ReleaseManifest],
) -> tuple[RolloutTarget, ...]:
    """Reduce ``release_status`` rows and their manifests to the routing table.

    Only the release-level rows (``component = 'referraleasy'``) are considered. Agent
    rows carry the same manifest — the registry stores one document per component so any
    row can reconstruct the release — and eight copies of one rollout block would make
    "which cohort is this release in" a question with eight answers.

    A release row with no manifest supplied is refused rather than skipped: a routing
    table that quietly omits a release is a router that quietly sends its traffic
    elsewhere, which is the failure mode that looks like success.
    """
    targets: list[RolloutTarget] = []
    for release in releases:
        if release.component != RELEASE_COMPONENT:
            continue
        manifest = manifests.get(release.release_code)
        if manifest is None:
            raise ReleaseRefusedError(
                f"{release.release_code} is registered as {release.status!r} but no manifest "
                "was supplied for it, so its rollout cannot be read"
            )
        # `rollout_facts` refuses an absent rollout rather than reading a default: a release
        # whose cohort nobody wrote down is not a release in the stable cohort.
        rollout = rollout_facts(manifest)
        cohort = rollout.cohort
        targets.append(
            RolloutTarget(
                release_manifest_id=manifest.release_manifest_id.root,
                cohort=cohort,
                share=rollout.share,
                active=release.active,
                # Both places a rollback can be recorded, because they are written by
                # different actors: the event log by whoever ran the rollback, the rollout
                # block by whoever built the release. A rollback that only one of them knows
                # about must still stop the traffic.
                rolled_back=release.rolled_back or cohort == ROLLED_BACK_COHORT,
            )
        )
    if not targets:
        raise ReleaseRefusedError(
            "no release rows were supplied; routing cannot answer without a registry"
        )
    return tuple(
        sorted(targets, key=lambda target: (target.percentage, target.release_manifest_id))
    )


@dataclass(frozen=True, slots=True)
class Routing:
    """Where one conversation's next turn goes, and why.

    ``reason`` is part of the answer rather than a log line. An operator asking "why is
    this conversation on the canary" is asking a question the routing decision should be
    able to answer itself, and the reasons are few enough to enumerate:
    ``pinned``, ``kill_switch``, ``canary``, ``stable``.
    """

    release_manifest_id: str
    cohort: str
    bucket: int
    reason: str
    pinned: bool = False
    stopped: bool = False
    pinned_rolled_back: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "release_manifest_id": self.release_manifest_id,
            "cohort": self.cohort,
            "bucket": self.bucket,
            "reason": self.reason,
            "pinned": self.pinned,
            "stopped": self.stopped,
            "pinned_rolled_back": self.pinned_rolled_back,
        }


def canary_bucket(conversation_id: str, *, salt: str = "") -> int:
    """The conversation's bucket in ``[0, BUCKETS)`` — stable, and salted per canary.

    ``salt`` should be the canary release's manifest id, so each canary draws a fresh
    sample of conversations. See the module docstring for why this is a hash and not
    anything cheaper.
    """
    if not conversation_id.strip():
        raise ReleaseRefusedError("a conversation id is required to bucket a conversation")
    digest = sha256_hex(f"{salt}:{conversation_id}".encode())
    return int(digest[:8], 16) % BUCKETS


def route(
    *,
    conversation_id: str,
    targets: Iterable[RolloutTarget],
    pinned: ConversationPin | None = None,
    kill_switch: KillSwitch | None = None,
) -> Routing:
    """Decide where the conversation's next turn runs.

    The order of the four questions is the whole design, and each one is ahead of the next
    for a reason:

    1. **Is the switch engaged?** Ahead of everything, including the pin. §13 gives the
       kill switch authority over conversations pinned to older releases; a check ordered
       after the pin would be a check the pin could veto.
    2. **Is the conversation already pinned?** Then it stays where it is, whatever the
       cohorts say. This is the line that keeps a canary from moving a conversation
       mid-flight: the canary takes *new* conversations, because only a new conversation
       can be routed without mixing two versions inside one transcript. A pinned
       conversation whose release has since been rolled back is reported as such
       (``pinned_rolled_back``) and still routed to its pin — stopping or migrating it is
       :mod:`referraleasy_release_registry.pinning`'s business, and doing it here would be
       a silent version change.
    3. **Does the bucket land in a canary?** Canaries are tried smallest-percentage first,
       so two overlapping rollouts resolve to the smaller one rather than depending on the
       order the registries came back in.
    4. **Is there a stable release?** Otherwise there is nowhere to send the conversation
       and that is a refusal rather than a hint: a router that returned "no release" would
       let a caller treat it as "use the default", and there is no default.
    """
    routes = tuple(targets)
    if not routes:
        raise ReleaseRefusedError(
            "no rollout targets; a release must be registered and activated before it can take "
            "traffic (§13)"
        )
    if kill_switch is not None and kill_switch.engaged:
        # The bucket is computed even though nothing branches on it, so that a stopped
        # conversation's record says which cohort it *would* have been in. That is the
        # question asked after an incident, and an answer reconstructed later would be
        # reconstructed under a registry that has moved on.
        return Routing(
            release_manifest_id=(pinned.release_manifest_id if pinned else ""),
            cohort=pinned.effective.cohort if pinned else "",
            bucket=canary_bucket(conversation_id),
            reason="kill_switch",
            pinned=pinned is not None,
            stopped=True,
        )
    if pinned is not None:
        rolled_back = any(
            target.release_manifest_id == pinned.release_manifest_id and target.rolled_back
            for target in routes
        )
        return Routing(
            release_manifest_id=pinned.release_manifest_id,
            cohort=pinned.effective.cohort,
            bucket=canary_bucket(conversation_id),
            reason="pinned",
            pinned=True,
            pinned_rolled_back=rolled_back,
        )

    stable = next(
        (target for target in routes if target.cohort == STABLE_COHORT and target.routable), None
    )
    for target in routes:
        if target.cohort == STABLE_COHORT or not target.routable:
            continue
        bucket = canary_bucket(conversation_id, salt=target.release_manifest_id)
        if bucket < target.percentage:
            return Routing(
                release_manifest_id=target.release_manifest_id,
                cohort=target.cohort,
                bucket=bucket,
                reason="canary",
            )
    if stable is None:
        described = ", ".join(
            f"{target.release_manifest_id} ({target.cohort}, {target_status(target)})"
            for target in routes
        )
        raise ReleaseRefusedError(
            f"no stable release is activated, so there is nowhere to route {conversation_id}. "
            f"Registered targets: {described}"
        )
    return Routing(
        release_manifest_id=stable.release_manifest_id,
        cohort=STABLE_COHORT,
        bucket=canary_bucket(conversation_id),
        reason="stable",
    )


def target_status(target: RolloutTarget) -> str:
    """``rolled_back`` or ``inactive`` — for the refusal message, which should be readable."""
    return "rolled_back" if target.rolled_back else "inactive"
