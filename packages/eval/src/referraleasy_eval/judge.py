"""The judge seam, and the rule that an unconfigured judge scores nothing (§14, ADR-0011).

§14 puts rules first and a judge second: a judge is a second model whose answers are expensive and
hard to audit, so it is asked only what a rule cannot answer. ADR-0011 is **UNRESOLVED** — no judge
model is pinned and `EVAL_JUDGE_MODEL_ID` is still a `CHANGEME_` sentinel — so this module ships the
port and no client, and that is the honest state rather than a stub.

The consequential decision is what a missing judge *means*. There were three ways to write this and
only one of them is safe:

* **Fall back to a rule.** The cases a judge exists for are exactly the ones a rule could not decide,
  so the fallback would answer a question nobody can answer with a rule — the definition of a
  fabricated score.
* **Default to a passing score.** Worse, and the reason §14's N/A rule exists at all.
* **Return `not_applicable` with the reason.** What this does. The case is still *visible* — it
  appears in the report's sample count and in the reason, naming ADR-0011 — and it is excluded from
  every average, which is the same treatment §14 gives an unscoreable case anywhere else.

So a dataset can hold judge-scored cases today, and a run reports them as unmeasured rather than
failing. When the ADR resolves, `--judge` supplies an implementation and the same cases start
scoring, with no dataset change.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from referraleasy_eval.models import Case, Verdict, eval_error, not_applicable

__all__ = [
    "JUDGE_METRIC_PREFIX",
    "RULES_JUDGE",
    "Judge",
    "JudgeUnavailable",
    "UnconfiguredJudge",
    "judge_verdict",
]

#: A judge-scored rule is named `judge:<question>`, so a dataset can ask for one without this module
#: having to enumerate every question a future judge might be asked.
JUDGE_METRIC_PREFIX = "judge:"

#: What a verdict's judge column carries when a **deterministic rule** produced it.
#:
#: §14 names two evaluators — *"deterministic rules first, judge second"* — and requires every
#: result to carry the dataset/rubric/evaluator/judge versions it was scored under. A rule verdict is
#: therefore not judge-less: it was evaluated, by the rules tier, and the column has to name which
#: evaluator it was. Blanking it is the state this module's contract already rules out — a run whose
#: judge column is blank cannot be compared with one whose judge is named — while filling it with the
#: *run's* judge would attribute a rule's score to a model and merge the two series §14 keeps apart.
RULES_JUDGE = "rules"


@runtime_checkable
class Judge(Protocol):
    """What a judge must be able to do. Deliberately tiny: one question, one answer.

    ``score`` receives the case's expectation as the question and the run record's artifacts, and
    returns a :class:`~referraleasy_eval.models.Verdict` — not a float. A judge that can only return
    a number cannot say "I could not judge this", and §14 requires that answer to be expressible by
    everything that scores.
    """

    #: Names the judge and its version, for `eval.judge`. §14 never merges two judges into one series,
    #: so this string is an identity rather than a label.
    name: str

    def score(self, case: Case, record: Any) -> Verdict: ...


class JudgeUnavailable(RuntimeError):
    """Raised by a judge that was asked to score without a model behind it."""


class UnconfiguredJudge:
    """The judge a run gets when none is supplied. It answers nothing, and says why.

    Not an error and not a pass: it exists so that ``--judge`` can be absent without the harness
    needing a branch, and so the report has a *name* to put in ``eval.judge`` — the absence of a
    judge is a fact about the run, and a run whose judge column is blank cannot be compared with one
    whose judge is named.
    """

    name = "unconfigured:ADR-0011-unresolved"

    def score(self, case: Case, record: Any) -> Verdict:
        rule = f"{JUDGE_METRIC_PREFIX}{case.metric}"
        return not_applicable(
            rule,
            "no judge model is pinned: ADR-0011 is UNRESOLVED and EVAL_JUDGE_MODEL_ID is still a "
            "CHANGEME_ sentinel, so this case is reported as unmeasured rather than scored by a "
            "fallback rule or a default pass",
        )


def judge_verdict(
    case: Case, record: Any, judge: Judge | None, *, checks: Sequence[str]
) -> Verdict | None:
    """Score a case with the judge when its checks ask for one, else return ``None``.

    A case asks for the judge by naming a ``judge:`` rule. Nothing is guessed: a case with no such
    check never reaches a model, which is §14's "rules first" as a property of the dataset rather
    than a convention in the runner.
    """
    wants = [name for name in checks if name.startswith(JUDGE_METRIC_PREFIX)]
    if not wants:
        return None
    if judge is None:
        return UnconfiguredJudge().score(case, record)
    try:
        verdict = judge.score(case, record)
    except JudgeUnavailable as unavailable:
        # A judge that was configured and could not answer is an *error*, unlike one that is absent:
        # something is wired up and broken, and reporting that as N/A would hide it in the same
        # bucket as the known gap.
        return eval_error(f"{JUDGE_METRIC_PREFIX}{case.metric}", str(unavailable))
    return Verdict(
        status=verdict.status,
        score=verdict.score,
        reason=verdict.reason,
        rule=verdict.rule or f"{JUDGE_METRIC_PREFIX}{case.metric}",
        critical=verdict.critical,
    )
