"""The dataset refuses to load half-right, the judge refuses to invent a score, and the CLI refuses
to run without its inputs.

Three refusals, and they share a shape: each one guards a way that an evaluation could *appear* to
have happened. A dataset that dropped an unknown rule would run and score 43 of 44 cases while
reporting 44. A judge that fell back to a rule would answer the questions a rule could not. A CLI
that defaulted a missing manifest would produce a report nobody could compare with anything.

So the tests below are mostly about what happens when something is wrong, and each is paired with the
healthy path so that "refuses everything" cannot pass for "refuses the right things".
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from eval_fixtures import RELEASE_ID, dataset, healthy_records, write_manifest, write_runs

from referraleasy_eval.cli import EXIT_OK, EXIT_UNAVAILABLE, main
from referraleasy_eval.dataset import DatasetError, load_dataset
from referraleasy_eval.judge import UnconfiguredJudge, judge_verdict
from referraleasy_eval.models import NOT_APPLICABLE, Case
from referraleasy_eval.runner import score_run
from referraleasy_eval.runs import RunRecordError, records_from_payload


def _rewrite(tmp_path, mutate: Any) -> Any:
    """Write a deliberately-broken copy of the committed dataset and return its path."""
    payload = json.loads(dataset().path.read_text(encoding="utf-8"))
    mutate(payload)
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The dataset
# ---------------------------------------------------------------------------


def test_the_committed_dataset_loads_and_covers_every_scenario() -> None:
    """The happy path, asserted first: everything below is about a corpus that does load."""
    loaded = dataset()
    assert loaded.version == "offline-v1"
    assert len(loaded.cases) >= 40, "§14 asks for 40-60 cases"
    assert len(loaded.by_scenario()) == 8, "the dataset does not cover all eight §20 scenarios"
    assert set(loaded.partitions()) >= {"calibration", "held_out"}
    assert loaded.source_hash, "the scenario manifest hash is missing, so drift cannot be detected"


def test_an_unknown_rule_name_stops_the_dataset_loading(tmp_path) -> None:
    """The failure this exists for: a case that would silently score nothing."""

    def mutate(payload: dict[str, Any]) -> None:
        payload["cases"][0]["checks"] = ["a_rule_that_does_not_exist"]

    with pytest.raises(DatasetError, match="unknown rule"):
        load_dataset(_rewrite(tmp_path, mutate))


def test_a_case_naming_an_unknown_scenario_stops_the_dataset_loading(tmp_path) -> None:
    def mutate(payload: dict[str, Any]) -> None:
        payload["cases"][0]["scenario_id"] = "a_scenario_that_does_not_exist"

    with pytest.raises(DatasetError, match="not in the scenario manifest"):
        load_dataset(_rewrite(tmp_path, mutate))


def test_a_reference_id_from_another_scenario_stops_the_dataset_loading(tmp_path) -> None:
    """A case citing a fixture its scenario does not involve is asserting about the wrong patient.

    This is the check that catches a copy-paste between scenarios, which is otherwise invisible: the
    id exists, the case loads, and the failure appears as an unexplained low score.
    """

    def mutate(payload: dict[str, Any]) -> None:
        standard = next(c for c in payload["cases"] if c["scenario_id"] == "standard_booking")
        # A provider id that belongs to the network-conflict scenario, not to standard booking.
        standard["references"] = {"providers": ["30000000-0000-4000-8000-000000000006"]}

    with pytest.raises(DatasetError, match="not among scenario"):
        load_dataset(_rewrite(tmp_path, mutate))


def test_a_duplicate_case_id_stops_the_dataset_loading(tmp_path) -> None:
    def mutate(payload: dict[str, Any]) -> None:
        payload["cases"][1]["case_id"] = payload["cases"][0]["case_id"]

    with pytest.raises(DatasetError, match="duplicate case_id"):
        load_dataset(_rewrite(tmp_path, mutate))


def test_a_missing_version_stops_the_dataset_loading(tmp_path) -> None:
    """§14 stamps every result with the versions it ran under; an unversioned dataset is a guess."""

    def mutate(payload: dict[str, Any]) -> None:
        payload.pop("version")

    with pytest.raises(DatasetError, match="declares no `version`"):
        load_dataset(_rewrite(tmp_path, mutate))


def test_every_problem_is_reported_together(tmp_path) -> None:
    """One fix at a time would make a drifted dataset a sequence of failed runs."""

    def mutate(payload: dict[str, Any]) -> None:
        payload["cases"][0]["checks"] = ["nope"]
        payload["cases"][1]["scenario_id"] = "nope"
        payload["cases"][2]["checks"] = []

    with pytest.raises(DatasetError) as raised:
        load_dataset(_rewrite(tmp_path, mutate))
    message = str(raised.value)
    assert "unknown rule" in message and "not in the scenario manifest" in message
    assert "no checks" in message


# ---------------------------------------------------------------------------
# The judge seam
# ---------------------------------------------------------------------------


def test_an_unconfigured_judge_reports_unmeasured_and_names_the_adr() -> None:
    """Not a fallback rule and not a default pass — the third option, and the only safe one."""
    entry = dataset().cases[0]
    verdict = judge_verdict(entry, healthy_records()[0], UnconfiguredJudge(), checks=["judge:foo"])
    assert verdict is not None
    assert verdict.status == NOT_APPLICABLE
    assert verdict.score is None
    assert "ADR-0011" in verdict.reason


def test_a_case_with_no_judge_check_never_reaches_a_judge() -> None:
    """§14's "rules first" as a property of the dataset rather than a convention in the runner."""

    class ExplodingJudge:
        name = "should-never-run"

        def score(self, case: Case, record: Any) -> Any:  # pragma: no cover - it must not be called
            raise AssertionError("a judge was asked about a case that named no judge rule")

    entry = dataset().cases[0]
    checks = tuple(name for name in entry.checks if not name.startswith("judge:"))
    assert judge_verdict(entry, healthy_records()[0], ExplodingJudge(), checks=checks) is None


def test_the_report_names_the_judge_that_answered() -> None:
    """§14 never merges two judges into one series, so the name is an identity, not a label."""
    run = score_run(dataset(), healthy_records(), release_manifest_id=RELEASE_ID)
    assert run.judge == "none"
    run_with_judge = score_run(
        dataset(), healthy_records(), release_manifest_id=RELEASE_ID, judge=UnconfiguredJudge()
    )
    assert run_with_judge.judge == UnconfiguredJudge.name


# ---------------------------------------------------------------------------
# The run-record boundary
# ---------------------------------------------------------------------------


def test_a_record_with_no_case_id_is_refused() -> None:
    with pytest.raises(RunRecordError, match="no `case_id`"):
        records_from_payload([{"outcome": "succeeded"}])


def test_a_booking_missing_its_state_is_refused_rather_than_defaulted() -> None:
    """A defaulted field would score as though it were empty — a capture gap read as a property."""
    with pytest.raises(RunRecordError, match="no 'state'"):
        records_from_payload(
            [{"case_id": "c", "outcome": "succeeded", "bookings": [{"booking_id": "b"}]}]
        )


def test_a_constraint_of_an_unknown_kind_is_refused() -> None:
    """§4's rule turns on hard versus soft, so a kind that is neither cannot be scored."""
    with pytest.raises(RunRecordError, match="neither 'hard' nor 'soft'"):
        records_from_payload(
            [
                {
                    "case_id": "c",
                    "outcome": "succeeded",
                    "constraints": [{"code": "x", "kind": "medium", "satisfied": True}],
                }
            ]
        )


def test_a_duplicated_record_is_refused() -> None:
    """Two records for one case would make the score depend on file order."""
    with pytest.raises(RunRecordError, match="appears more than once"):
        records_from_payload(
            [{"case_id": "c", "outcome": "succeeded"}, {"case_id": "c", "outcome": "failed"}]
        )


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def _args(tmp_path, **overrides: Any) -> list[str]:
    args = [
        "--release-manifest",
        str(overrides.pop("manifest", write_manifest(tmp_path))),
        "--report-dir",
        str(tmp_path / "reports"),
        "--no-emit",
    ]
    for key, value in overrides.items():
        args += [f"--{key.replace('_', '-')}", str(value)]
    return args


def test_the_cli_refuses_instead_of_inventing_a_release_id(tmp_path, capsys) -> None:
    missing = tmp_path / "nope.json"
    code = main(["--release-manifest", str(missing), "--no-emit"])
    assert code == EXIT_UNAVAILABLE
    assert "no release manifest" in capsys.readouterr().err


def test_the_cli_refuses_a_manifest_without_a_release_id(tmp_path, capsys) -> None:
    """A manifest that exists but names nothing is the same problem wearing a file extension."""
    hollow = tmp_path / "hollow.json"
    hollow.write_text(json.dumps({"source": {}}), encoding="utf-8")
    code = main(["--release-manifest", str(hollow), "--no-emit"])
    assert code == EXIT_UNAVAILABLE
    assert "release_manifest_id" in capsys.readouterr().err


def test_without_run_records_every_case_reports_as_unmeasured_and_the_run_still_succeeds(
    tmp_path, capsys
) -> None:
    """A run with no replay is not a failure — it is a run that measured nothing, and it says so.

    The exit code stays 0 because nothing was found to be wrong; the *output* is where the absence
    lives, and a caller that reads the report sees 44 unmeasured cases rather than a clean sheet.
    """
    code = main(_args(tmp_path))
    printed = capsys.readouterr().out
    assert code == EXIT_OK
    assert "0/44 case(s) have a run record" in printed
    report = json.loads(next((tmp_path / "reports").glob("*.json")).read_text())
    assert report["coverage"]["with_records"] == 0
    assert all(not case["record_present"] for case in report["results"])
    assert report["critical_violations"] == []


def test_a_healthy_replay_scores_and_writes_a_report_that_names_its_inputs(
    tmp_path, capsys
) -> None:
    runs = write_runs(tmp_path, healthy_records())
    code = main(_args(tmp_path, runs=runs))
    printed = capsys.readouterr().out
    assert code == EXIT_OK, printed

    report_path = next((tmp_path / "reports").glob("*.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))

    # §14: the report carries the versions it ran under, resolved rather than echoed.
    assert report["release_manifest_id"] == RELEASE_ID
    assert report["dataset"]["version"] == "offline-v1"
    assert len(report["dataset"]["content_hash"]) == 64
    assert report["dataset"]["scenario_manifest_hash"]
    assert report["rubric"] == "offline-rules-v1"
    assert report["judge"] == "none"
    assert report["coverage"]["with_records"] == 44
    assert report["run_id"].startswith("eval-")
    # And no critical violation, which is what the exit code already said.
    assert report["critical_violations"] == []


def test_the_report_counts_what_it_could_not_measure(tmp_path, capsys) -> None:
    """§14's rule, visible in the artifact rather than only in the code.

    Every metric carries `scored`, `not_applicable` and `errors` beside its mean, so a reader can
    tell a low score from a thin measurement — which is the whole reason the three are separate.
    """
    runs = write_runs(tmp_path, healthy_records())
    assert main(_args(tmp_path, runs=runs)) == EXIT_OK
    report = json.loads(next((tmp_path / "reports").glob("*.json")).read_text(encoding="utf-8"))

    by_metric = {summary["metric"]: summary for summary in report["metrics"]}
    assert by_metric, "the report carries no metric summaries"
    for summary in by_metric.values():
        assert {"scored", "not_applicable", "errors", "mean", "cases"} <= set(summary)
    # At least one metric must genuinely decline somewhere in this dataset, or the columns would be
    # decorative — `groundedness` on the no-specialist case has no evidence to check.
    assert any(summary["not_applicable"] for summary in by_metric.values()), (
        "no verdict in the whole dataset was N/A, so the N/A column is untested"
    )
