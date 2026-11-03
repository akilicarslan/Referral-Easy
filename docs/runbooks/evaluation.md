# Runbook — running §14's evaluation

Symptom-first, like `local-demo.md`. Each heading is something you might see; each body says what it
means and what to do. The commands are the answer to "how does this work", so they come first.

```bash
make eval                                   # replay every scenario, then score (needs §1's manifest)
make eval-replay                            # only the replay → captures/replay.json
make eval RUNS=captures/replay.json         # score an existing replay without re-running it
```

`make eval` exits **1** on a §14 release blocker, **3** when it could not start, and **0** otherwise.

## 0. What a run needs, and what it can run without

Three inputs:

| Input | Where it comes from | Exists? |
| --- | --- | --- |
| Release manifest | `scripts/check_release_versions.py build` | **after §1** |
| Dataset + rubric | `evaluations/datasets/offline-v1.json`, committed | yes |
| Run records | `scripts/eval_replay.py` → `captures/replay.json` | yes — `make eval` runs it first |

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

## 2. How the replay works, and what the first measured run said

`scripts/eval_replay.py` runs each `(scenario_id, replay_variant)` the dataset names on **a fresh
mock API over a temporary SQLite file**, with the gateway (memory control store) and the seven agent
services in-process on the offline profile. The script supplies only what a patient would: the
opening message and each choice (the option the scenario prefers, else the first), approval, and
consent to a handoff. The scenario's fault is armed on the mock API at the point the autonomous E2E
journeys arm it. Nothing else is scripted — no specialist result, transition or booking.

Three things are recorded and nothing else: the gateway's admin view at the end, every delegation
through a recording `CoordinatorDispatch`, and every tool call at `ToolSet.invoke` (the agents' one
path to a handler). `referraleasy_eval.capture` maps a journey to one record per case; its module
docstring lists the three places a capture could flatter the system and what it does instead. A
journey that does not settle produces **no record**, so its cases show up as unmeasured, not as
low scores.

**Measured run (2026-10-20, `offline-v2`, `rel-20261016-8e00275`): 44/44 cases recorded, no critical
violation.** Where each scenario settles:

| Scenario | Settles in | Why |
| --- | --- | --- |
| standard_booking, version_regression | `BOOKED` | the straight path; both runs agree |
| multi_constraint_search | `BOOKED` | no Friday slot → evidence-backed alternative → the patient's choice is a recorded `ConstraintChange` |
| network_conflict | `BOOKED` | network status unknown → shown with freshness → the patient chose that doctor again |
| missing_document | `BOOKING_UNVERIFIED` | the patient attached their prior report; the re-check passed; the hospital's review-required adapter leaves the request `PENDING_REVIEW` |
| timeout_after_commit | `BOOKING_UNVERIFIED` | lost commit response, no second booking |
| timeout_after_commit / before_commit | `HUMAN_HANDOFF` | the lookup reports no appointment, so nothing is treated as committed |
| three_transient_errors | `HUMAN_HANDOFF` | the chosen hospital needs documents the patient does not hold |
| no_specialist_found | `HUMAN_HANDOFF` (`no_match`) | as the case expects |

Several cases still disagree with the dataset, and the disagreement is the finding rather than a defect
to edit away. For example, `multi_constraint_search` expects `no_match` while the patient's accepted
alternative completes the journey, and the timeout case expects a handoff that the reconciliation
design does not make.

Two measurement gaps are visible in the report rather than hidden in it. **Constraints:** the
matching agent returns `constraint_fit: []`, so constraint cases report `eval_error`/`n/a`.
**Cost:** the offline workflow path makes no model call, so cost is a measured `$0` over tool calls,
not a model cost.

The replay surfaced two fixture problems. **Fixed:** plan 006's network record and coverage result
named provider 001 (a cardiologist), while scenario 5's dermatology patient can only match provider
010, so the timeout scenario could never reach a booking. **Not fixed, worked around:** scenario 3's
conflict record (coverage 002, Lakeview) lies outside patient 005's hard 20-mile radius, so the replay
uses Prairie Ridge, whose network status for the plan is unknown.

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
mean, and the mean covers **only the scored cases** (§14: *"N/A and eval_error are not scored as
zero"*). A metric with two measurements and eight gaps is a thin measurement, not a bad score,
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
