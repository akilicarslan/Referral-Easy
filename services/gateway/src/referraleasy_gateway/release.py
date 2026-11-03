"""Which release this gateway dispatches under (§13).

A task's §5 envelope carries ``release_manifest_id``, and §13 makes it load-bearing: a conversation
pinned to a release is one whose evaluation can be compared with another's, and "latest" is never
sufficient provenance for a report. The gateway is the root caller of a task, so it is the process
that names the pin.

**Per-conversation pinning (§13).** A workflow is a conversation. On its first dispatch the
coordination worker asks :class:`WorkflowReleases` for its release: the registry's canary router
(`referraleasy_release_registry.canary.route`) picks one from the committed registry, and
`pinning.pin_conversation` records the pin in the workflow's payload (`release_pin`, with the
routing decision beside it as `release_routing`). Every later dispatch of that workflow carries the
pinned id, whatever the registry says by then — a canary takes *new* conversations only, which is
the router's own rule. The payload is the control store's, so the pin survives a restart under the
Postgres profile and every replica reads the same one; no process-local map is involved.

The catalog is `registry/releases/` (`index.json` plus each manifest), because that is the registry
that exists: nothing registers releases in the `release_registry` table yet. A release committed to
it is treated as activated, and its manifest's own `rollout` says its cohort and share — a release
whose cohort is `rolled_back` takes no new conversations.

The single startup id below remains the fallback for a deployment whose catalog cannot be read.
Resolution order, and the failure mode:

1. ``RELEASE_MANIFEST_ID`` in the environment — a deployment that pins explicitly.
2. ``registry/releases/latest.json`` — the manifest ``scripts/check_release_versions.py`` builds.
3. Empty — nothing to name. The dispatcher then reports itself unconfigured rather than sending a
   task with an invented id; a missing pin is a deployment fact, not a value to make up.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from referraleasy_contracts.generated.workflow import ReleaseManifest
from referraleasy_gateway.control import ControlState, WorkflowRow
from referraleasy_gateway.timing import Clock
from referraleasy_release_registry.canary import (
    ROLLED_BACK_COHORT,
    STABLE_COHORT,
    RolloutTarget,
    Routing,
    route,
)
from referraleasy_release_registry.effective import effective_versions
from referraleasy_release_registry.errors import ReleaseRefusedError
from referraleasy_release_registry.manifest import rollout_facts
from referraleasy_release_registry.pinning import ConversationPin, pin_conversation

__all__ = [
    "COORDINATOR_AGENT",
    "LATEST_MANIFEST",
    "RELEASES_DIR",
    "RELEASE_MANIFEST_ID_ENV",
    "ReleaseCatalog",
    "WorkflowReleases",
    "pinned_release",
    "resolve_release_manifest_id",
]

_log = logging.getLogger("referraleasy.gateway.release")

#: The variable a deployment can use to pin explicitly. Same name the registry's CLI documents.
RELEASE_MANIFEST_ID_ENV: Final = "RELEASE_MANIFEST_ID"

#: `services/gateway/src/referraleasy_gateway/release.py` -> repository root in a checkout. In the
#: built image the module lives at `/app/.venv/...`, so this resolves to `/app/.venv` — the
#: Dockerfile copies `registry/` there beside `fixtures/`, for the same reason and with the same
#: caveat as the fixtures copy.
REPO_ROOT: Final = Path(__file__).resolve().parents[4]

#: The committed registry: `index.json`, `latest.json` and one manifest per release.
RELEASES_DIR: Final = REPO_ROOT / "registry" / "releases"

#: The manifest `make eval` and the CI version gate read.
LATEST_MANIFEST: Final = RELEASES_DIR / "latest.json"

#: The agent a conversation's pin is recorded against: the root of every task the gateway sends.
COORDINATOR_AGENT: Final = "care-coordinator"

#: Who a pin names as its author. The router decides; nobody approved a first pin, and §13 only
#: requires an approval for a *migration*.
PINNED_BY: Final = "gateway-release-router"


def resolve_release_manifest_id(
    env: Mapping[str, str] | None = None,
    *,
    latest: Path = LATEST_MANIFEST,
) -> str:
    """The release id this process should dispatch under, or ``""`` when there is none.

    A malformed manifest is treated as no manifest: the caller reports "cannot name a release" and
    the dispatcher stays unconfigured. Raising here would take the gateway down for a file that is
    only needed when a coordinator is configured — and a gateway that cannot serve bookings because
    its release label is unreadable is the wrong failure.
    """
    explicit = (env or {}).get(RELEASE_MANIFEST_ID_ENV, "").strip()
    if explicit:
        return explicit
    try:
        document = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(document, dict):
        return ""
    value = document.get("release_manifest_id")
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class ReleaseCatalog:
    """The releases a conversation may be pinned to, and the router's targets over them."""

    manifests: Mapping[str, ReleaseManifest]
    targets: tuple[RolloutTarget, ...]

    @classmethod
    def from_directory(
        cls, directory: Path = RELEASES_DIR, *, explicit: str = ""
    ) -> ReleaseCatalog | None:
        """Read `index.json` and every manifest it lists, or ``None`` when there is nothing usable.

        ``explicit`` is a deployment's `RELEASE_MANIFEST_ID`: it narrows the catalog to that one
        release as the stable target, so an explicit pin is still a pin a conversation records
        rather than a label the router ignores. A listed release whose manifest cannot be read is
        refused, not skipped — a catalog that quietly drops a release is a router that quietly
        sends its traffic elsewhere.
        """
        try:
            index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        manifests: dict[str, ReleaseManifest] = {}
        for entry in index.get("releases") or ():
            path = directory / str(entry.get("file", ""))
            manifest = ReleaseManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
            manifests[manifest.release_manifest_id.root] = manifest
        if explicit:
            if explicit not in manifests:
                _log.warning(
                    "RELEASE_MANIFEST_ID=%s is not in %s, so conversations cannot be pinned to it "
                    "with their effective versions; dispatch falls back to the unpinned id",
                    explicit,
                    directory,
                )
                return None
            manifests = {explicit: manifests[explicit]}
        targets = []
        for release_id, manifest in manifests.items():
            facts = rollout_facts(manifest)
            cohort = STABLE_COHORT if explicit else facts.cohort
            targets.append(
                RolloutTarget(
                    release_manifest_id=release_id,
                    cohort=cohort,
                    share=1.0 if explicit else facts.share,
                    # A release committed to the file registry is one this deployment ships; the
                    # registry has no activation log, so there is nothing else to read.
                    active=True,
                    rolled_back=cohort == ROLLED_BACK_COHORT,
                )
            )
        if not targets:
            return None
        return cls(manifests=manifests, targets=tuple(targets))


def pinned_release(row: WorkflowRow) -> str | None:
    """The release a workflow is pinned to, or ``None`` before its first dispatch."""
    pin = row.payload.get("release_pin")
    if isinstance(pin, Mapping) and pin.get("release_manifest_id"):
        return str(pin["release_manifest_id"])
    return None


class _PayloadPinStore:
    """`pinning.PinStore` over one workflow's payload, in the control store the gateway already has.

    `pin_for` resolves the pinned release's effective versions from its (immutable) manifest
    rather than storing them twice; a pin naming a release that has left the catalog is refused,
    because re-pinning it would be a silent mid-conversation version change.
    """

    def __init__(
        self,
        control: ControlState,
        row: WorkflowRow,
        catalog: ReleaseCatalog,
        routing: Routing | None,
    ) -> None:
        self.control = control
        self.row = row
        self.catalog = catalog
        self.routing = routing

    def pin_for(self, conversation_id: str) -> ConversationPin | None:
        stored = self.row.payload.get("release_pin")
        if not isinstance(stored, Mapping):
            return None
        release_id = str(stored["release_manifest_id"])
        manifest = self.catalog.manifests.get(release_id)
        if manifest is None:
            raise ReleaseRefusedError(
                f"workflow {conversation_id} is pinned to {release_id}, which is no longer in the "
                "release catalog; it is not re-pinned, because that would change its versions "
                "mid-conversation (§13)"
            )
        return ConversationPin(
            conversation_id=conversation_id,
            release_manifest_id=release_id,
            agent_name=str(stored["agent_name"]),
            effective=effective_versions(manifest, agent_name=str(stored["agent_name"])),
            pinned_at=datetime.fromisoformat(str(stored["pinned_at"])),
            pinned_by=str(stored["pinned_by"]),
        )

    def record(self, pin: ConversationPin) -> None:
        self.row = self.control.update_payload(
            self.row.workflow_id,
            expected_state_version=self.row.state_version,
            patch={
                "release_pin": pin.to_payload(),
                "release_routing": self.routing.to_payload() if self.routing else None,
            },
        )


class WorkflowReleases:
    """Pin each workflow to a release on its first dispatch, through the registry's router."""

    def __init__(self, control: ControlState, catalog: ReleaseCatalog, *, clock: Clock) -> None:
        self.control = control
        self.catalog = catalog
        self.clock = clock

    def pin(self, row: WorkflowRow) -> WorkflowRow:
        """Return the row with its pin, recording one if the workflow has none."""
        conversation_id = str(row.workflow_id)
        if pinned_release(row) is not None:
            # Re-resolved rather than trusted: a pin whose release has left the catalog is refused
            # here, before a task goes out under a release nothing can describe.
            _PayloadPinStore(self.control, row, self.catalog, None).pin_for(conversation_id)
            return row
        routing = route(conversation_id=conversation_id, targets=self.catalog.targets)
        store = _PayloadPinStore(self.control, row, self.catalog, routing)
        pin_conversation(
            store,
            conversation_id=conversation_id,
            effective=effective_versions(
                self.catalog.manifests[routing.release_manifest_id], agent_name=COORDINATOR_AGENT
            ),
            pinned_by=PINNED_BY,
            now=self.clock.now(),
        )
        return store.row

    def describe(self) -> dict[str, Any]:
        return {
            "releases": sorted(self.catalog.manifests),
            "targets": [
                {
                    "release_manifest_id": t.release_manifest_id,
                    "cohort": t.cohort,
                    "share": t.share,
                    "routable": t.routable,
                }
                for t in self.catalog.targets
            ],
        }
