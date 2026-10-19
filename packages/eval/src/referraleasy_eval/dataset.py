"""Loading the committed dataset, and refusing one that has drifted from the fixtures (§14).

The dataset is **authored data**, not something derived at load time, because §14 asks for the
dataset *and* the rubric to be versioned inputs to a run — and a version is only meaningful if the
thing it names is a fixed artifact rather than a function of whatever the fixtures happen to say.
So the cases are written down, and this module's job is to check them against everything they make
claims about:

* every ``scenario_id`` exists in ``fixtures/scenarios.json``;
* every fixture id a case cites appears in *that scenario's* ``references`` — so a case cannot
  quietly attach itself to a fixture its scenario does not involve, which is how a case ends up
  asserting something about a patient it was never written for;
* every rule name in ``checks`` is registered, because an unknown name is a case that would
  otherwise score nothing while the run stays green;
* every case has at least one check, and no two cases share an id.

Each of those is a load error rather than a warning. A dataset that half-loads is worse than one
that does not: the report would carry a case count and a mean over a subset nobody chose.

The two hashes are what a report stamps and a later run compares. ``content_hash`` covers the cases
as loaded, so editing one expectation changes it. ``source_hash`` covers the scenario manifest the
cases were validated against, so a fixture change that invalidates a case's grounding is visible
even when the dataset itself did not move — §14 asks every result to carry the versions it was
evaluated under, and "the dataset has not changed" is not the same statement as "the fixtures have
not".
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from referraleasy_eval.models import Case
from referraleasy_eval.rules import RULES

__all__ = ["DATASET_DIR", "DEFAULT_DATASET", "SCENARIO_MANIFEST", "Dataset", "DatasetError"]

#: The repo root, derived from this file's location: `packages/eval/src/referraleasy_eval` is four
#: levels below it, never the cwd. Same rule as `referraleasy_guardrails.corpus`.
REPO_ROOT: Final = Path(__file__).resolve().parents[4]

#: §14's dataset directory, which the repository scaffolded before there was anything to put in it.
DATASET_DIR: Final = REPO_ROOT / "evaluations" / "datasets"
DEFAULT_DATASET: Final = DATASET_DIR / "offline-v1.json"

#: The scenario manifest every case is validated against. The fixture corpus is the demo's, and a
#: second copy of it here would be a second thing to keep in step.
SCENARIO_MANIFEST: Final = REPO_ROOT / "fixtures" / "scenarios.json"


class DatasetError(ValueError):
    """A dataset that cannot be trusted to score anything. Raised at load, never later."""


def _digest(payload: Any) -> str:
    """A stable hash of a JSON-shaped value: sorted keys, no incidental whitespace."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class Dataset:
    """The cases, plus the two identities a report has to carry."""

    dataset_id: str
    version: str
    rubric: str
    cases: tuple[Case, ...]
    content_hash: str
    source_hash: str
    path: Path

    def by_scenario(self) -> Mapping[str, tuple[Case, ...]]:
        grouped: dict[str, list[Case]] = {}
        for case in self.cases:
            grouped.setdefault(case.scenario_id, []).append(case)
        return {scenario: tuple(cases) for scenario, cases in grouped.items()}

    def metrics(self) -> tuple[str, ...]:
        return tuple(sorted({case.metric for case in self.cases}))

    def partitions(self) -> Mapping[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.partition] = counts.get(case.partition, 0) + 1
        return counts


def _scenario_references(repo_root: Path) -> Mapping[str, Mapping[str, set[str]]]:
    """Every fixture id each scenario involves, read from the manifest §1.2 already maintains."""
    path = repo_root / SCENARIO_MANIFEST.relative_to(REPO_ROOT)
    if not path.is_file():
        raise DatasetError(
            f"{path} is missing, so no case can be checked against the scenario it names. The "
            "dataset is validated against that manifest rather than duplicating its ids."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise DatasetError(f"{path} carries no `scenarios` list")
    out: dict[str, Mapping[str, set[str]]] = {}
    for scenario in scenarios:
        references = scenario.get("references") or {}
        out[str(scenario["scenario_id"])] = {
            family: {str(value) for value in values}
            for family, values in references.items()
            if isinstance(values, list)
        }
    return out


def load_dataset(
    path: Path | None = None,
    *,
    repo_root: Path | None = None,
    require_dir: bool = True,
) -> Dataset:
    """Load and validate the dataset. Every problem is reported together, not one per attempt.

    ``repo_root`` exists for the tests, which build a dataset that is deliberately wrong and need it
    checked against the same manifest without writing into the repository.
    """
    root = repo_root or REPO_ROOT
    target = path or (root / DEFAULT_DATASET.relative_to(REPO_ROOT))
    if not target.is_file():
        raise DatasetError(
            f"no dataset at {target}. §14 requires a versioned dataset as an input to a run, so "
            "there is nothing to evaluate against rather than a default to fall back to."
        )
    if require_dir and not DATASET_DIR.is_dir():  # pragma: no cover - a scaffolded directory
        raise DatasetError(f"{DATASET_DIR} is missing; §14's layout expects the dataset there")

    payload = json.loads(target.read_text(encoding="utf-8"))
    scenarios = _scenario_references(root)

    declared_version = str(payload.get("version") or "").strip()
    problems: list[str] = []
    if not declared_version:
        problems.append(
            "the dataset declares no `version`. §14 stamps every result with the versions it ran "
            "under, and an unversioned dataset makes that stamp a guess"
        )

    cases: list[Case] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload.get("cases") or []):
        case_id = str(raw.get("case_id") or f"<case {index}>")
        where = f"case {case_id!r}"
        if case_id in seen:
            problems.append(f"{where}: duplicate case_id")
            continue
        seen.add(case_id)

        scenario_id = str(raw.get("scenario_id") or "")
        if scenario_id not in scenarios:
            problems.append(
                f"{where}: scenario {scenario_id!r} is not in the scenario manifest, so nothing "
                "about it can be checked"
            )
            continue

        checks = tuple(str(name) for name in raw.get("checks") or ())
        if not checks:
            problems.append(f"{where}: no checks, so the case would score nothing")
        unknown = [name for name in checks if name not in RULES]
        if unknown:
            problems.append(
                f"{where}: unknown rule(s) {unknown}. A case naming a rule that does not exist "
                "would be silently skipped otherwise"
            )

        references = {
            str(family): tuple(str(value) for value in values)
            for family, values in (raw.get("references") or {}).items()
        }
        known = scenarios[scenario_id]
        for family, values in references.items():
            in_scenario = known.get(family)
            if in_scenario is None:
                problems.append(
                    f"{where}: cites reference family {family!r}, which scenario {scenario_id!r} "
                    "does not have"
                )
                continue
            stray = [value for value in values if value not in in_scenario]
            if stray:
                problems.append(
                    f"{where}: {family} id(s) {stray} are not among scenario {scenario_id!r}'s "
                    "references, so the case is asserting about fixtures its scenario does not "
                    "involve"
                )

        cases.append(
            Case(
                case_id=case_id,
                scenario_id=scenario_id,
                metric=str(raw.get("metric") or ""),
                checks=checks,
                expectation=dict(raw.get("expectation") or {}),
                rubric=str(raw.get("rubric") or payload.get("rubric") or ""),
                references=references,
                partition=str(raw.get("partition") or "held_out"),
            )
        )

    if not cases:
        problems.append("the dataset contains no usable case; a mean over nothing is not a result")
    if problems:
        raise DatasetError("the dataset does not load: " + "; ".join(problems))

    source_path = root / SCENARIO_MANIFEST.relative_to(REPO_ROOT)
    return Dataset(
        dataset_id=str(payload.get("dataset_id") or target.stem),
        version=declared_version,
        rubric=str(payload.get("rubric") or ""),
        cases=tuple(cases),
        content_hash=_digest(
            [
                {
                    "case_id": case.case_id,
                    "scenario_id": case.scenario_id,
                    "metric": case.metric,
                    "checks": list(case.checks),
                    "expectation": case.expectation,
                    "references": {k: list(v) for k, v in sorted(case.references.items())},
                    "partition": case.partition,
                }
                for case in cases
            ]
        ),
        source_hash=hashlib.sha256(source_path.read_bytes()).hexdigest()
        if source_path.is_file()
        else "",
        path=target,
    )
