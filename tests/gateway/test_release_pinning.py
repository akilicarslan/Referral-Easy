"""§13 in the running gateway: a workflow is pinned to a release on its first dispatch, by the
registry's canary router, and keeps that pin.

These drive `referraleasy_gateway.release` over a temporary copy of the committed registry with a
second release added, so routing has a real choice to make. The router and the pin are the
registry package's own (`canary.route`, `pinning.pin_conversation`) — what is tested here is that
the gateway calls them, stores the result where every replica reads it, and sends it on the wire.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from referraleasy_gateway.control import InMemoryControlState
from referraleasy_gateway.dispatch import CoordinatorDispatch
from referraleasy_gateway.events import EventLog
from referraleasy_gateway.release import (
    RELEASES_DIR,
    ReleaseCatalog,
    WorkflowReleases,
    pinned_release,
)
from referraleasy_gateway.timing import SystemClock
from referraleasy_release_registry.errors import ReleaseRefusedError

STABLE = "rel-20261016-8e00275"
CANARY = "rel-20261020-c0ffee1"
PATIENT = "b0000000-0000-4000-8000-000000000001"


def _registry(tmp_path: Path, *, canary_share: float, canary_cohort: str = "canary") -> Path:
    """The committed registry plus a second release, built from the first manifest."""
    directory = tmp_path / "releases"
    shutil.copytree(RELEASES_DIR, directory)
    index = json.loads((directory / "index.json").read_text())
    stable_entry = next(e for e in index["releases"] if e["release_manifest_id"] == STABLE)
    manifest = json.loads((directory / stable_entry["file"]).read_text())
    manifest["release_manifest_id"] = CANARY
    manifest["rollout"] = {
        "cohort": canary_cohort,
        "cohort_percentage": canary_share,
        "kill_switch_engaged": False,
    }
    (directory / f"{CANARY}.json").write_text(json.dumps(manifest))
    index["releases"] = [
        stable_entry,
        {**stable_entry, "release_manifest_id": CANARY, "file": f"{CANARY}.json"},
    ]
    (directory / "index.json").write_text(json.dumps(index))
    return directory


def _releases(directory: Path, control: InMemoryControlState, **kw: Any) -> WorkflowReleases:
    catalog = ReleaseCatalog.from_directory(directory, **kw)
    assert catalog is not None
    return WorkflowReleases(control, catalog, clock=SystemClock())


def test_the_committed_registry_pins_a_workflow_to_its_stable_release() -> None:
    control = InMemoryControlState()
    row = control.create(PATIENT)
    catalog = ReleaseCatalog.from_directory()
    assert catalog is not None
    pinned = WorkflowReleases(control, catalog, clock=SystemClock()).pin(row)
    assert pinned_release(pinned) == STABLE
    assert pinned.payload["release_routing"]["reason"] == "stable"
    assert pinned.payload["release_pin"]["agent_name"] == "care-coordinator"
    # The pin is in the control store, not only on the returned row.
    stored = control.load(row.workflow_id)
    assert stored is not None and pinned_release(stored) == STABLE


def test_a_canary_takes_its_share_of_new_workflows_and_the_split_is_stable(tmp_path: Path) -> None:
    directory = _registry(tmp_path, canary_share=0.5)
    control = InMemoryControlState()
    releases = _releases(directory, control)
    rows = [releases.pin(control.create(PATIENT)) for _ in range(200)]
    chosen = [pinned_release(row) for row in rows]
    assert set(chosen) == {STABLE, CANARY}
    assert 60 <= chosen.count(CANARY) <= 140  # a hash split, not a coin per call
    # A second router over the same store gives every workflow the release it already has.
    again = _releases(directory, control)
    assert [pinned_release(again.pin(control.load(r.workflow_id))) for r in rows] == chosen  # type: ignore[arg-type]


def test_a_pinned_workflow_keeps_its_release_when_the_registry_moves(tmp_path: Path) -> None:
    """The canary takes *new* conversations. One already running must not change version."""
    control = InMemoryControlState()
    first = _releases(_registry(tmp_path / "a", canary_share=0.0), control).pin(
        control.create(PATIENT)
    )
    assert pinned_release(first) == STABLE
    everything_to_canary = _releases(_registry(tmp_path / "b", canary_share=1.0), control)
    assert pinned_release(everything_to_canary.pin(first)) == STABLE
    assert pinned_release(everything_to_canary.pin(control.create(PATIENT))) == CANARY


def test_a_rolled_back_release_takes_no_new_workflows(tmp_path: Path) -> None:
    directory = _registry(tmp_path, canary_share=1.0, canary_cohort="rolled_back")
    control = InMemoryControlState()
    releases = _releases(directory, control)
    assert {pinned_release(releases.pin(control.create(PATIENT))) for _ in range(50)} == {STABLE}


def test_an_explicit_release_narrows_the_catalog_to_itself(tmp_path: Path) -> None:
    directory = _registry(tmp_path, canary_share=1.0)
    control = InMemoryControlState()
    releases = _releases(directory, control, explicit=STABLE)
    assert {pinned_release(releases.pin(control.create(PATIENT))) for _ in range(20)} == {STABLE}
    assert ReleaseCatalog.from_directory(directory, explicit="rel-20990101-0000000") is None


def test_a_pin_to_a_release_that_left_the_catalog_is_refused_not_repinned(tmp_path: Path) -> None:
    control = InMemoryControlState()
    row = _releases(_registry(tmp_path, canary_share=1.0), control).pin(control.create(PATIENT))
    assert pinned_release(row) == CANARY
    without_canary = _releases(RELEASES_DIR, control)
    with pytest.raises(ReleaseRefusedError, match="no longer in the release catalog"):
        without_canary.pin(row)
    stored = control.load(row.workflow_id)
    assert stored is not None and pinned_release(stored) == CANARY


def test_the_dispatched_envelope_carries_the_workflows_pin() -> None:
    sent: list[dict[str, Any]] = []

    class Client:
        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> Any:
            sent.append(json)

            class Response:
                def raise_for_status(self) -> None: ...

                def json(self) -> dict[str, Any]:
                    return {"result": {"outcome": "completed"}}

            return Response()

    from referraleasy_gateway.timing import SystemClock as Clock

    dispatch = CoordinatorDispatch(
        base_url="http://coordinator",
        token="t",
        release_manifest_id=STABLE,
        log=EventLog(synthetic=True),
        clock=Clock(),
        client=Client(),  # type: ignore[arg-type]
    )
    common: dict[str, Any] = {
        "workflow_id": uuid4(),
        "workflow_state": "INTAKE",
        "state_version": 1,
        "command_id": "c-1",
        "specialist_inputs": {},
        "execution_plan": [],
    }
    dispatch.execute(**common, release_manifest_id=CANARY)
    dispatch.execute(**common)
    envelopes = [body["payload"]["task_envelope"]["release_manifest_id"] for body in sent]
    assert envelopes == [CANARY, STABLE]


def test_no_readable_catalog_means_no_router_rather_than_a_guess(tmp_path: Path) -> None:
    assert ReleaseCatalog.from_directory(tmp_path / "missing") is None
