"""Scoring a set of run records, and the report that says what was and was not measured (§14).

The harness has one job and three rules.

**Every case gets a verdict.** A case with no run record is not skipped — it is reported as
``eval_error`` naming the missing record, because "the replay did not produce this case" and "the
case passed" must not look the same in a report. §14 makes the same point about N/A and zero, one
level up.

**A rule that raises is a defect, not a failure of the system under test.** An exception inside a
rule is caught and becomes ``eval_error`` with the traceback's first line, so a broken rule cannot
be mistaken for a system that scored badly — and cannot take the whole run down while the other 43
cases had answers.

**Failures are attributed to the case's metric, never to a specialist.** §14: workflow scores are not
attributed to a single agent. The report groups by ``eval.metric``, which is a question, not a
component; the run's own role attribution lives in the telemetry the agent services emit, where it
belongs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from referraleasy_eval.dataset import Dataset
from referraleasy_eval.judge import JUDGE_METRIC_PREFIX, Judge, judge_verdict
from referraleasy_eval.models import (
    EVAL_ERROR,
    NOT_APPLICABLE,
    SCORED,
    Case,
    RunRecord,
    Verdict,
    eval_error,
)
from referraleasy_eval.rules import RULES

__all__ = [
    "CaseResult",
    "EvaluationRun",
    "MetricSummary",
    "case_result",
    "evaluation_run_id",
    "score_run",
]


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One case and everything the rules and the judge said about it."""

    case_id: str
    scenario_id: str
    metric: str
    partition: str
    verdicts: tuple[Verdict, ...]
    record_present: bool = True

    @property
    def scored(self) -> tuple[Verdict, ...]:
        return tuple(verdict for verdict in self.verdicts if verdict.status == SCORED)

    @property
    def unmeasured(self) -> tuple[Verdict, ...]:
        """Verdicts that carry no number, with the reason kept rather than discarded."""
        return tuple(
            verdict for verdict in self.verdicts if verdict.status in (NOT_APPLICABLE, EVAL_ERROR)
        )

    @property
    def errors(self) -> tuple[Verdict, ...]:
        return tuple(verdict for verdict in self.verdicts if verdict.status == EVAL_ERROR)

    @property
    def critical(self) -> tuple[Verdict, ...]:
        return tuple(verdict for verdict in self.verdicts if verdict.critical)

    @property
    def score(self) -> float | None:
        """The case's mean over its **scored** verdicts, or ``None`` when nothing was scored.

        ``None`` rather than ``0.0`` for a case whose rules all declined: §14 keeps N/A out of the
        distribution, and a case that could not be measured is not a case that failed.
        """
        scored = self.scored
        if not scored:
            return None
        assert all(verdict.score is not None for verdict in scored)
        return sum(verdict.score or 0.0 for verdict in scored) / len(scored)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "scenario_id": self.scenario_id,
            "metric": self.metric,
            "partition": self.partition,
            "score": self.score,
            "critical": bool(self.critical),
            "record_present": self.record_present,
            "verdicts": [
                {
                    "rule": verdict.rule,
                    "status": verdict.status,
                    "score": verdict.score,
                    "critical": verdict.critical,
                    "reason": verdict.reason,
                }
                for verdict in self.verdicts
            ],
        }


@dataclass(frozen=True, slots=True)
class MetricSummary:
    """One metric across every case that asked for it."""

    metric: str
    cases: int
    scored: int
    not_applicable: int
    errors: int
    mean: float | None
    critical: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "cases": self.cases,
            "scored": self.scored,
            "not_applicable": self.not_applicable,
            "errors": self.errors,
            "critical": self.critical,
            "mean": self.mean,
        }


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    """One evaluation, with the versions it ran under and what it found."""

    run_id: str
    dataset_id: str
    dataset_version: str
    dataset_hash: str
    source_hash: str
    rubric: str
    release_manifest_id: str
    judge: str
    synthetic: bool
    started_at: datetime
    results: tuple[CaseResult, ...] = field(default_factory=tuple)

    @property
    def critical_violations(self) -> tuple[CaseResult, ...]:
        """Cases holding a §14 release blocker. A non-empty tuple blocks."""
        return tuple(result for result in self.results if result.critical)

    def summaries(self) -> tuple[MetricSummary, ...]:
        """Per-metric counts, and this is where §14's 'N/A is not zero' becomes visible.

        Every metric reports ``scored``, ``not_applicable`` and ``errors`` beside its mean, so a
        reader can tell a metric with a low score from one with two measurements and eight gaps.
        """
        grouped: dict[str, list[CaseResult]] = {}
        for result in self.results:
            grouped.setdefault(result.metric, []).append(result)
        summaries: list[MetricSummary] = []
        for metric, results in sorted(grouped.items()):
            scores = [score for score in (r.score for r in results) if score is not None]
            summaries.append(
                MetricSummary(
                    metric=metric,
                    cases=len(results),
                    scored=sum(1 for result in results if result.score is not None),
                    not_applicable=sum(
                        1
                        for result in results
                        for verdict in result.verdicts
                        if verdict.status == NOT_APPLICABLE
                    ),
                    errors=sum(len(result.errors) for result in results),
                    mean=(sum(scores) / len(scores)) if scores else None,
                    critical=sum(1 for result in results if result.critical),
                )
            )
        return tuple(summaries)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dataset": {
                "id": self.dataset_id,
                "version": self.dataset_version,
                "content_hash": self.dataset_hash,
                "scenario_manifest_hash": self.source_hash,
            },
            "rubric": self.rubric,
            "judge": self.judge,
            "release_manifest_id": self.release_manifest_id,
            "synthetic": self.synthetic,
            "started_at": self.started_at.isoformat(),
            "cases": len(self.results),
            "critical_violations": [result.case_id for result in self.critical_violations],
            "metrics": [summary.as_dict() for summary in self.summaries()],
            "results": [result.as_dict() for result in self.results],
        }


def evaluation_run_id(dataset_hash: str, release_manifest_id: str, case_ids: Iterable[str]) -> str:
    """A deterministic id for one evaluation.

    Derived rather than random so that re-running the same dataset against the same release under
    the same rules produces the *same* ``eval.run_id`` — which is what makes the telemetry idempotent
    under §14's rule that a run is written once. A uuid would make every retry a new run and every
    aggregate double-count one.
    """
    digest = hashlib.sha256(
        "\x1f".join([dataset_hash, release_manifest_id, *sorted(case_ids)]).encode("utf-8")
    ).hexdigest()
    return f"eval-{digest[:16]}"


def case_result(
    case: Case,
    record: RunRecord | None,
    judge: Judge | None,
) -> CaseResult:
    """Score one case. Never raises: a rule's defect is a verdict, not a crash."""
    if record is None:
        missing = eval_error(
            "capture",
            f"no run record for {case.case_id!r}. The replay did not produce this case, so it is "
            "reported as unmeasured — which is not the same as passing",
        )
        return CaseResult(
            case_id=case.case_id,
            scenario_id=case.scenario_id,
            metric=case.metric,
            partition=case.partition,
            verdicts=(missing,),
            record_present=False,
        )

    verdicts: list[Verdict] = []
    for name in case.checks:
        rule = RULES.get(name)
        if rule is None:
            # A `judge:` check is the one name the rule registry does not hold, and it is resolved
            # below. Anything else reaching here was caught at load; this is the belt.
            if not name.startswith(JUDGE_METRIC_PREFIX):
                verdicts.append(
                    eval_error(name, f"{name!r} is not a registered rule, so it scored nothing")
                )
            continue
        try:
            verdicts.append(rule(case, record))
        # Broad on purpose: a rule that raises is a defect in this package, and it must be visible as
        # one case's `eval_error` rather than as a traceback that loses the other forty-three.
        except Exception as error:
            verdicts.append(eval_error(name, f"the rule raised {type(error).__name__}: {error}"))

    judged = judge_verdict(case, record, judge, checks=case.checks)
    if judged is not None:
        verdicts.append(judged)

    return CaseResult(
        case_id=case.case_id,
        scenario_id=case.scenario_id,
        metric=case.metric,
        partition=case.partition,
        verdicts=tuple(verdicts),
    )


def score_run(
    dataset: Dataset,
    records: Sequence[RunRecord],
    *,
    release_manifest_id: str,
    judge: Judge | None = None,
    started_at: datetime | None = None,
    synthetic: bool = True,
) -> EvaluationRun:
    """Score every case in the dataset against the records, in dataset order.

    A record whose ``case_id`` is not in the dataset is **not** silently dropped: it is the other
    half of the coverage question, and
    :func:`referraleasy_eval.runner.unmatched_records` reports it so a run can say that the replay
    produced something the dataset does not describe.
    """
    by_case = {record.case_id: record for record in records}
    results = tuple(case_result(case, by_case.get(case.case_id), judge) for case in dataset.cases)
    return EvaluationRun(
        run_id=evaluation_run_id(
            dataset.content_hash, release_manifest_id, (case.case_id for case in dataset.cases)
        ),
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        dataset_hash=dataset.content_hash,
        source_hash=dataset.source_hash,
        rubric=dataset.rubric,
        release_manifest_id=release_manifest_id,
        judge=getattr(judge, "name", None) or "none",
        synthetic=synthetic,
        started_at=started_at or datetime.now(UTC),
        results=results,
    )


def unmatched_records(dataset: Dataset, records: Sequence[RunRecord]) -> tuple[str, ...]:
    """Record ids the dataset does not describe, sorted.

    Reported rather than ignored: a replay that produced a case nobody scores is either a dataset
    that is missing a scenario or a capture that is mislabelling, and both are worth knowing before
    the numbers are read.
    """
    known = {case.case_id for case in dataset.cases}
    return tuple(sorted({record.case_id for record in records if record.case_id not in known}))


def coverage(dataset: Dataset, records: Sequence[RunRecord]) -> Mapping[str, int]:
    """How much of the dataset the replay actually covered. Printed before any score."""
    recorded = {record.case_id for record in records}
    return {
        "cases": len(dataset.cases),
        "with_records": sum(1 for case in dataset.cases if case.case_id in recorded),
        "without_records": sum(1 for case in dataset.cases if case.case_id not in recorded),
    }
