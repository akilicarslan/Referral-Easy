"""`make eval` — score a replay against the dataset and write a report that names its inputs (§14).

§14's requirement is that the CLI "requires release manifest + dataset/rubric version and writes
resolved versions into the report". Three of those are inputs and one is an output, and the shape of
this file follows from taking that seriously:

* **Nothing is defaulted that §14 names as an input.** There is no built-in release id, no implicit
  dataset version and no fallback rubric. A report whose header is partly invented cannot be compared
  with another report, which is the only thing a report is for.
* **The report carries what was *resolved*, not what was asked for.** A dataset path resolves to a
  content hash; a manifest belongs to a release id; the judge resolves to the name of whatever
  answered. A later run can then say whether the two are the same measurement rather than assuming
  it from a path that both happen to share.
* **A critical violation is a non-zero exit.** §14 makes an unapproved or double booking a release
  blocker, so the code returns 1 — the same signal the version gate uses — and a pipeline that
  ignores it has to ignore an exit code rather than a paragraph.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from referraleasy_eval.dataset import DEFAULT_DATASET, REPO_ROOT, DatasetError, load_dataset
from referraleasy_eval.judge import JUDGE_METRIC_PREFIX, RULES_JUDGE, Judge
from referraleasy_eval.runner import (
    EvaluationRun,
    coverage,
    score_run,
    unmatched_records,
)
from referraleasy_eval.runs import RunRecordError, load_records

__all__ = ["EXIT_CRITICAL", "EXIT_OK", "EXIT_UNAVAILABLE", "EXIT_USAGE", "main"]

EXIT_OK: Final = 0
#: A §14 release blocker. Distinct from a low score: a run can score badly and still ship, and a run
#: with an unapproved booking cannot.
EXIT_CRITICAL: Final = 1
EXIT_USAGE: Final = 2
#: An input the run could not be started on — a missing manifest, dataset or replay.
EXIT_UNAVAILABLE: Final = 3

#: The compose service names that do not resolve from the host. Mirrors `scripts/health._host_url`;
#: one list per entry point, because a package may not import a script.
CONTAINER_HOSTS: Final[tuple[str, ...]] = (
    "otel-collector",
    "opensearch",
    "data-prepper",
    "postgres",
    "gateway",
    "mock-api",
    "web",
)

#: Where reports land. §14's scaffolding put the directory there before there was anything to write.
REPORTS_DIR: Final = REPO_ROOT / "evaluations" / "reports"


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    """The release a run belongs to. Read, never constructed — a fabricated id is not a release."""

    release_manifest_id: str
    release_version: str
    git_sha: str
    image_digest: str


def _release_identity(path: Path) -> ReleaseIdentity:
    """Read the §13 manifest. A missing or unnamed release is a refusal, not a placeholder id."""
    if not path.is_file():
        raise FileNotFoundError(
            f"no release manifest at {path}. §14 requires the evaluation to name the release its "
            "results belong to, so a run without one would produce scores that cannot be attributed "
            "— and `read`-only runs against a missing file would silently invent an id. Build one "
            "with:\n"
            '  uv run python scripts/check_release_versions.py build --git-sha "$(git rev-parse HEAD)"'
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    release_id = str(payload.get("release_manifest_id") or "").strip()
    if not release_id:
        raise ValueError(
            f"{path} carries no `release_manifest_id`. An evaluation must be attributable to a "
            "release; a report with an empty id cannot be compared with any other"
        )
    source = payload.get("source") or {}
    return ReleaseIdentity(
        release_manifest_id=release_id,
        release_version=str(payload.get("release_version") or payload.get("version") or ""),
        git_sha=str(source.get("git_sha") or ""),
        image_digest=str(source.get("image_digest") or ""),
    )


def _emit(run: EvaluationRun) -> Mapping[str, Any]:
    """Emit one §15 evaluation event per case verdict, if the stack is configured.

    Through the telemetry package rather than a second writer, so §14's "N/A and eval_error are not
    zero" rule is enforced by the code that already enforces it — `eval_score` raises if a status and
    a score disagree, which is the check that keeps a gap out of a mean.

    Returns what happened rather than a boolean, because "we emitted 61 events" and "there was no
    collector to emit to" are different outcomes and only one of them belongs in a report.
    """
    from referraleasy_telemetry import Synthetic, configure_telemetry

    collector = _collector_endpoint()
    if collector is None:
        return {
            "emitted": 0,
            "reason": (
                "no OTEL_EXPORTER_OTLP_ENDPOINT in the environment or .env, so there is no "
                "collector to emit to. The report is still written"
            ),
        }
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = collector

    # §14 requires synthetic traffic to be labelled, and the label is an enum rather than a boolean
    # so "unlabelled" is not a third silent state. A run of the committed fixtures is `SYNTHETIC`;
    # the flag travels here as a bool because that is what a report needs to be JSON.
    synthetic = Synthetic.SYNTHETIC if run.synthetic else Synthetic.LIVE_DEMO
    telemetry = configure_telemetry("referraleasy-eval", version="0.1.0", set_global=False)
    if not telemetry.active:
        telemetry.shutdown()
        return {
            "emitted": 0,
            "reason": "the telemetry bundle came back inactive despite an endpoint being set",
        }

    emitted = 0
    try:
        for result in run.results:
            for verdict in result.verdicts:
                telemetry.emitter.eval_score(
                    run_id=run.run_id,
                    metric=result.metric,
                    score=verdict.score,
                    status=verdict.status,
                    # The case is what makes this measurement distinguishable from the other 43 in
                    # the same run: without it every metric derives one id and the sample count
                    # reads 1. Stored as `eval.case_id` below as well — the attribute is what a
                    # reader groups by, the argument is what the id is derived from.
                    case_id=result.case_id,
                    judge=(
                        run.judge if verdict.rule.startswith(JUDGE_METRIC_PREFIX) else RULES_JUDGE
                    ),
                    rubric_version=run.rubric,
                    agent_role=None,
                    synthetic=synthetic,
                    extra_fields={
                        "eval.case_id": result.case_id,
                        "eval.scenario_id": result.scenario_id,
                        "eval.partition": result.partition,
                        "eval.rule": verdict.rule,
                        "eval.critical": verdict.critical,
                        "eval.release_manifest_id": run.release_manifest_id,
                    },
                )
                emitted += 1
    finally:
        telemetry.shutdown()
    return {"emitted": emitted, "reason": ""}


def _collector_endpoint() -> str | None:
    """The OTLP endpoint, resolved the way the demo resolves its URLs.

    Two steps, and both are needed for `make eval` on a checkout to emit anything:

    * **The process environment is consulted first**, because that is how a container gets the value.
    * **`.env` is read as a fallback** — nothing in this repository exports it (see
      `scripts/health._read_dotenv`), and the CLI is run from a checkout by `make eval`, so without
      this the endpoint exists and is invisible.
    * **A compose service name becomes `localhost`**, because the shipped `.env` names services the
      way a *container* reaches them (`http://otel-collector:4318`) and that does not resolve on the
      host. Same rule as `scripts/health._host_url`, applied here rather than in the committed file
      so the file stays correct for containers.

    Returns `None` when there is nothing configured, and the caller reports that as a reason rather
    than as zero events with no explanation.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        env_file = REPO_ROOT / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                name, _, value = line.partition("=")
                if name.strip() == "OTEL_EXPORTER_OTLP_ENDPOINT":
                    endpoint = value.strip().strip('"').strip("'")
                    break
    if not endpoint:
        return None
    for container_host in CONTAINER_HOSTS:
        if f"//{container_host}:" in endpoint:
            return endpoint.replace(f"//{container_host}:", "//localhost:")
    return endpoint


def _print_summary(run: EvaluationRun, counts: Mapping[str, int], unmatched: Sequence[str]) -> None:
    print(f"run              {run.run_id}")
    print(f"dataset          {run.dataset_id} {run.dataset_version}  ({run.dataset_hash[:12]}…)")
    print(f"scenario corpus  {run.source_hash[:12] or '(unavailable)'}…")
    print(f"release          {run.release_manifest_id}")
    print(f"rubric           {run.rubric}   judge  {run.judge}")
    print(f"coverage         {counts['with_records']}/{counts['cases']} case(s) have a run record")
    if unmatched:
        print(
            f"unmatched        {len(unmatched)} record(s) the dataset does not describe: {list(unmatched)}"
        )
    print()
    print(f"  {'metric':<28} {'cases':>5} {'scored':>6} {'n/a':>5} {'err':>5} {'mean':>7}")
    print(f"  {'-' * 28} {'-' * 5} {'-' * 6} {'-' * 5} {'-' * 5} {'-' * 7}")
    for summary in run.summaries():
        mean = "n/a" if summary.mean is None else f"{summary.mean:.3f}"
        print(
            f"  {summary.metric:<28} {summary.cases:>5} {summary.scored:>6} "
            f"{summary.not_applicable:>5} {summary.errors:>5} {mean:>7}"
        )
    print()
    print("A metric's mean covers only its *scored* cases: the `n/a` and `err` columns are counted")
    print("and excluded from it, so a low mean and a well-measured one cannot be confused (§14).")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m referraleasy_eval",
        description="§14's offline evaluation: score a replay against the committed dataset.",
    )
    parser.add_argument(
        "--release-manifest",
        type=Path,
        required=True,
        help="the §13 release manifest these results belong to (usually registry/releases/latest.json)",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET,
        help=f"the versioned dataset to score against (default: {DEFAULT_DATASET.name})",
    )
    parser.add_argument(
        "--runs",
        type=Path,
        default=None,
        help="a JSON file of run records produced by a replay. Without it every case reports as "
        "unmeasured, so it is required whenever a score is expected (see docs/runbooks).",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=REPORTS_DIR,
        help=f"where the report is written (default: {REPORTS_DIR.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--no-emit",
        action="store_true",
        help="write the report without emitting §15 events (for a machine with no collector)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        release = _release_identity(args.release_manifest)
        dataset = load_dataset(args.dataset)
        records = load_records(args.runs) if args.runs else ()
    except (FileNotFoundError, ValueError, DatasetError, RunRecordError) as problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_UNAVAILABLE

    judge: Judge | None = None
    run = score_run(dataset, records, release_manifest_id=release.release_manifest_id, judge=judge)
    counts = coverage(dataset, records)
    unmatched = unmatched_records(dataset, records)

    _print_summary(run, counts, unmatched)

    emission: Mapping[str, Any] = {"emitted": 0, "reason": "disabled by --no-emit"}
    if not args.no_emit:
        emission = _emit(run)
        print(f"\nemitted          {emission['emitted']} event(s) {emission['reason']}".rstrip())

    report = {
        **run.as_dict(),
        "release": {
            "version": release.release_version,
            "git_sha": release.git_sha,
            "image_digest": release.image_digest,
        },
        "coverage": dict(counts),
        "unmatched_records": list(unmatched),
        "emission": dict(emission),
    }
    args.report_dir.mkdir(parents=True, exist_ok=True)
    written = args.report_dir / f"{run.run_id}.json"
    written.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    # `--report-dir` may point outside the repository — a CI run writes to its own workspace — and
    # `relative_to` raises on a path that is not under the root. Printed relative when it is, because
    # a path inside the repo is easier to follow, and absolute when it is not.
    try:
        shown = written.relative_to(REPO_ROOT)
    except ValueError:
        shown = written
    print(f"report           {shown}")

    critical = run.critical_violations
    if critical:
        print(
            f"\nRELEASE BLOCKER: {len(critical)} case(s) hold a critical violation — "
            f"{[result.case_id for result in critical]}. §14 makes an unapproved or double booking "
            "a release blocker, and this exit code is the signal.",
            file=sys.stderr,
        )
        return EXIT_CRITICAL

    unmeasured = sum(summary.not_applicable + summary.errors for summary in run.summaries())
    if unmeasured:
        print(
            f"\nNOTE: {unmeasured} verdict(s) carry no score, each with its reason in the report. "
            "§14 keeps them out of the means; they are counted rather than hidden."
        )
    return EXIT_OK
