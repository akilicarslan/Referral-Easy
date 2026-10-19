"""ReferralEasy evaluation — §14's offline harness, as a package.

§14 asks for an evaluation that is *auditable* rather than merely present, and four of its sentences
shape everything here:

    Offline replay on a separate fixture DB; **never recreate live bookings.**
    Offline dataset of ~40-60 cases covering all scenarios.
    **Deterministic rules first, judge second.**
    N/A and eval_error **are not scored as zero.**

Its parts, and what each refuses to do:

``models``   The three shapes — expectation, observation, judgement — kept apart so that "could not
             decide" has somewhere to live other than a zero.
``dataset``  Loading the authored dataset, and refusing one that has drifted from the fixtures it
             cites. Versioned, hashed, and validated against ``fixtures/scenarios.json``.
``rules``    §14's deterministic evaluators. Every one is a pure function; none calls a model.
``judge``    The judge port, with **no client**: ADR-0011 is unresolved, so a judge-scored case
             reports as unmeasured rather than being answered by a fallback rule.
``runs``     Reading run records: the only boundary where an observation arrives from outside, so
             the only place it can be malformed.
``runner``   Scoring, and the report that separates a low score from a thin measurement.
``cli``      ``make eval``. Requires the release manifest and a dataset; writes resolved versions.

What is deliberately **not** here yet, stated rather than implied:

* the **replay harness** that produces run records — §14's capture side. Until it exists, a run has
  to be given records or every case reports as unmeasured;
* **online sampling** (§14's 1% Bernoulli draw) and the **charts**, which need the admin app;
* **LangSmith**, blocked on ADR-0010, and the **judge model**, blocked on ADR-0011.
"""

from referraleasy_eval.dataset import (
    DEFAULT_DATASET,
    Dataset,
    DatasetError,
    load_dataset,
)
from referraleasy_eval.judge import Judge, JudgeUnavailable, UnconfiguredJudge
from referraleasy_eval.models import (
    Case,
    RunRecord,
    Verdict,
    critical_verdict,
    eval_error,
    not_applicable,
    scored,
)
from referraleasy_eval.rules import RULES, rule_names
from referraleasy_eval.runner import EvaluationRun, score_run
from referraleasy_eval.runs import RunRecordError, load_records, records_from_payload

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_DATASET",
    "RULES",
    "Case",
    "Dataset",
    "DatasetError",
    "EvaluationRun",
    "Judge",
    "JudgeUnavailable",
    "RunRecord",
    "RunRecordError",
    "UnconfiguredJudge",
    "Verdict",
    "__version__",
    "critical_verdict",
    "eval_error",
    "load_dataset",
    "load_records",
    "not_applicable",
    "records_from_payload",
    "rule_names",
    "score_run",
    "scored",
]
