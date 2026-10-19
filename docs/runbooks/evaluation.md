# Runbook — running §14's evaluation

Symptom-first, like `local-demo.md`. Each heading is something you might see; each body says what it
means and what to do. The commands are the answer to "how does this work", so they come first.

```bash
make eval                                   # needs registry/releases/latest.json (see §1)
make eval RUNS=captures/replay.json         # with a replay's run records
```

`make eval` exits **1** on a §14 release blocker, **3** when it could not start, and **0** otherwise.

## 0. What a run needs, and what it can run without

Three inputs, and only two of them exist today:

| Input | Where it comes from | Exists? |
| --- | --- | --- |
| Release manifest | `scripts/check_release_versions.py build` | **after §1** |
| Dataset + rubric | `evaluations/datasets/offline-v1.json`, committed | yes |
| Run records | a replay's captures | **no — the replay harness is not written** |

Without run records the command still runs, **exits 0**, and reports all 44 cases as *unmeasured*
with the reason. That is deliberate and pinned by a test: a run with no replay is not a failure, it
is a run that measured nothing, and the absence lives in the output and in the report rather than in
the exit code. So a gate that wants to refuse such a run has to read
`coverage.with_records` in the report rather than trust `$?` — nothing here will fail on its own
because it scored nothing.

## 1. `make eval` says "no release manifest at registry/releases/latest.json"

The CLI requires the §13 manifest rather than defaulting one, because a report whose header is
invented cannot be compared with another report — which is the only thing a report is for.

```bash
uv run python scripts/check_release_versions.py build \
  --git-sha "$(git rev-parse HEAD)" \
  --image-digest "$(docker image inspect referraleasy/agents:dev --format '{{.Id}}')"
```

`--image-digest` is required and is not derivable: a tag is mutable and a Dockerfile does not imply a
digest. If there is no built image, **the manifest cannot be built yet** — and that is the honest
state rather than a reason to write a placeholder. `registry/releases/` is empty precisely because
this has not been done.

## 2. Every case reports as unmeasured ("0/44 case(s) have a run record")

Expected until the replay harness exists. Run records are the *observations* the rules judge; the
dataset holds only the expectations. Without them there is nothing to judge, and the harness says so
rather than scoring the expectations against themselves.

A record is a JSON object per case — see `referraleasy_eval.runs.records_as_payload` for the exact
shape, and `tests/eval/eval_fixtures.py` for what a healthy one looks like. The fields that are easy
to get wrong:

* `case_id` must match a case in the dataset, or the record is reported in `unmatched_records`;
* `live_approval_ids` is what makes a booking *approved*. A confirmed booking whose `approval_id` is
  not in that list is a **release blocker**, not a quality miss;
* `constraints[].kind` must be `hard` or `soft`. §4's rule turns on which, so a third value is
  refused at load rather than scored.

## 3. "the dataset does not load: …"

Five causes, and the message names which:

* **unknown rule** — a `checks` entry that is not in `referraleasy_eval.rules.RULES`;
* **not in the scenario manifest** — a `scenario_id` that `fixtures/scenarios.json` does not list;
* **not among scenario …'s references** — a case citing a fixture its own scenario does not involve.
  This is the copy-paste check: the id exists, so only the scenario's own reference list can catch it;
* **duplicate case_id** — two cases, one name, and the score would depend on file order;
* **declares no `version`** — §14 stamps every result with the versions it ran under.

Every problem is reported in one message. Fix them together rather than one run at a time.

## 4. "RELEASE BLOCKER: n case(s) hold a critical violation"

An **unapproved** booking (a confirmed booking with no live approval) or a **double booking** (two
confirmed bookings on one slot). §14 makes both release blockers, and the exit code is 1 so a
pipeline reads it without parsing prose.

The report names the case ids and, in each verdict's reason, the booking ids and slot. This is the
one result that should stop everything: it is not a low score, it is a forbidden outcome.

## 5. A metric's mean looks wrong, or its `n/a` column is large

That is the design, not a bug. `scored`, `not_applicable` and `errors` are reported beside every
mean, and the mean covers **only the scored cases** (§14: *"N/A ve eval_error sıfır olarak
skorlanmaz"*). A metric with two measurements and eight gaps is a thin measurement, not a bad score,
and the columns are how a reader tells them apart.

To find out *why* something was not measured, read the case in the report: every unscored verdict
carries a reason. A large `err` column usually means the replay did not produce those cases; a large
`n/a` column means the dataset does not ask those questions (for example, `groundedness` on a case
that names no evidence ids).

## 6. "no judge model is pinned: ADR-0011 is UNRESOLVED"

Expected. `packages/eval` ships the judge *port* and no client, so a case naming a `judge:` rule
reports as unmeasured rather than being answered by a fallback rule or a default pass. When ADR-0011
pins a model, an implementation is passed as `judge=` and the same cases start scoring with no
dataset change.

## 7. Re-running produces the same `run_id`

Also the design. `run_id` is derived from the dataset hash, the release id and the case ids, so
§14's "a run is written once" holds: re-running the same inputs is the same run rather than a second
one to double-count. If you *want* a second run, change what is evaluated — a different dataset
version, a different release, or a different rubric — rather than the clock.

## 8. Adding a case

1. Add it to `evaluations/datasets/offline-v1.json` with a `scenario_id` and fixture ids that
   scenario already references.
2. Run `uv run pytest tests/eval -q`. The dataset's own tests catch a stray id, an unknown rule and a
   duplicate id before anything else runs.
3. Bump the dataset's `version` if the change alters what a score means — the version and the content
   hash both travel into the report, and a comparison across a version change is a comparison of two
   different measurements.
