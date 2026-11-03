"""Every dataset case can be replayed: its scenario and variant have a patient script.

A case whose `(scenario_id, replay_variant)` has no script produces a journey observation carrying
an error, and so no record — it would report as unmeasured forever while the suite stayed green.
"""

from __future__ import annotations

import json

import scripts_fixtures as fx

from referraleasy_eval.capture import variant_of
from referraleasy_eval.dataset import load_dataset


def test_every_dataset_scenario_variant_has_a_replay_script() -> None:
    replay = fx.load_module("eval_replay", fx.REPO_ROOT / "scripts" / "eval_replay.py")
    wanted = {(case.scenario_id, variant_of(case)) for case in load_dataset().cases}
    assert wanted - set(replay.SCRIPTS) == set()


def test_each_script_drives_the_scenarios_own_fixture_patient() -> None:
    """The scenario manifest names its patients; a script using another one tests another scenario."""
    replay = fx.load_module("eval_replay", fx.REPO_ROOT / "scripts" / "eval_replay.py")
    manifest = json.loads((fx.REPO_ROOT / "fixtures" / "scenarios.json").read_text())
    patients = {s["scenario_id"]: set(s["references"]["patients"]) for s in manifest["scenarios"]}
    wrong = {
        key: script.patient_tail
        for key, script in replay.SCRIPTS.items()
        if replay._PATIENT.format(tail=script.patient_tail) not in patients[key[0]]
    }
    assert wrong == {}
