# 10 · Evaluation

Spec: §14 · ADR-0010, ADR-0011 · Code: `packages/eval/src/referraleasy_eval/`,
`evaluations/{datasets,reports}/`, `make eval`

[← back to the index](README.md)

## The offline evaluation run

```mermaid
flowchart LR
    subgraph Inputs["required — nothing defaulted"]
        RM["--release-manifest<br/>registry/releases/latest.json"]
        DS["dataset<br/>evaluations/datasets/offline-v1.json<br/>44 cases"]
        RUNS["--runs<br/>replay run records"]:::gap
    end

    DS --> Drift["dataset.py<br/>refuses a dataset that drifted from the fixtures"]
    RUNS --> Runs["runs.py<br/>observations a replay produced"]
    Drift --> Runner["runner.py"]
    Runs --> Runner
    RM --> Runner

    Runner --> Rules["rules.py — deterministic first"]
    Runner --> Judge["judge.py — only where a rule cannot reach"]:::unwired
    Rules --> Rep
    Judge --> Rep["report<br/>evaluations/reports/eval-*.json"]
    Rep --> Exit{"critical violation?"}
    Exit -- "unapproved / double booking" --> E1["exit 1 — release blocker"]
    Exit -- no --> E0["exit 0"]

    classDef unwired fill:#fff8e1,stroke:#f9a825,color:#1b1b1b,stroke-dasharray:4 3
    classDef gap fill:#f5f5f5,stroke:#9e9e9e,color:#616161,stroke-dasharray:2 4
```

> **Gap (README #3):** there is no replay harness yet, so `make eval` without `RUNS=` reports all
> 44 cases as *unmeasured*. That is the honest state, not a pass. There is no judge configured either
> (ADR-0011), so judge-scored cases report `not_applicable` with the reason, and are excluded from
> every average.

## The rules

```mermaid
flowchart TB
    subgraph Quality["quality (§14 core)"]
        q1["outcome_correctness"]
        q2["groundedness — tool/source snapshot"]
        q3["completeness — the state's expectations"]
    end
    subgraph Task["task outcome"]
        t1["correct_selection"]
        t2["constraints_satisfied"]
        t3["no_hard_constraint_relaxed"]
        t4["tool_arguments_match_the_outcome"]
        t5["recovery_succeeded"]
        t6["appropriate_escalation"]
    end
    subgraph Cost["efficiency"]
        c1["delegation_budget"]
        c2["latency_budget"]
        c3["cost_reported"]
    end
    subgraph Critical["critical — release blockers"]
        k1["unapproved_booking"]
        k2["double_booking"]
    end
```

`N/A` and `eval_error` never count as a quality score of zero. The rules accept different valid
tool orders; none of them imposes a single correct sequence.

## What a report records

```mermaid
classDiagram
    class EvalReport {
        run_id
        release_manifest_id   resolved, not "latest"
        dataset   path + content hash
        rubric   e.g. offline-rules-v1
        judge   name of what answered, or none
        synthetic
        cases · coverage · unmatched_records
        metrics   per metric: sample count, eligible, N/A, eval errors
        critical_violations
        results[]
        emission   §15 eval_score events
    }
```

## Planned, not built

```mermaid
flowchart LR
    Online["online sampling<br/>HMAC(seed, turn_id) Bernoulli 1%"]:::gap --> Batch["batch at 00:00 / 12:00<br/>America/Chicago"]:::gap
    Batch --> Smith["LangSmith experiments"]:::gap
    Batch --> OS[("OpenSearch evaluation index")]
    Risk["critical events:<br/>100% deterministic + risk-review queue"]:::gap

    classDef gap fill:#f5f5f5,stroke:#9e9e9e,color:#616161,stroke-dasharray:2 4
```

Nothing in `services/` or `packages/` imports LangSmith today. The admin Evaluations page reads
whatever `eval_score` events exist in OpenSearch.
