# Autonomous coordination completion record

Date: 2026-10-19. Decision: [ADR-0041](../adr/ADR-0041-autonomous-coordination.md).

The standard referral journey now runs from recorded source data. Patient choices trigger the next
step. The gateway projects specialist results into cards and transitions. Tests supply patient
commands, not specialist outputs or transition commands.

## Reproduce

Install workspace dependencies. Start PostgreSQL. Create a dedicated test database. Apply all
16 migrations to that database. Set `TEST_DATABASE_URL` to its connection string. These tests clear
test tables, so use a database reserved for tests.

```bash
.venv/bin/pytest -m 'not integration and not spike and not e2e' -q
.venv/bin/pytest -m integration --ignore=tests/rag -q
.venv/bin/python scripts/e2e.py --repeat 2
cd apps/web
node_modules/.bin/tsc -p tsconfig.json --noEmit
node_modules/.bin/vitest run
```

The E2E runner starts a fresh mock hospital API and a temporary SQLite database for each iteration.
When `TEST_DATABASE_URL` is set, the autonomous gateway and policy use PostgreSQL. The test calls
real agent ASGI apps through HTTP delegation. It uses no provider model.

## Acceptance evidence

| Requirement | Evidence |
| --- | --- |
| Independent coverage and matching start together | `tests/agents/test_workflow_execution.py`; a barrier requires both branches to start before either finishes |
| Each patient choice resumes the journey | `tests/e2e/test_autonomous_coordination.py` |
| A replacement worker resumes a stored answer | Restart E2E case; `tests/gateway/test_coordination_continuations.py` tests expired claims and stale completion |
| No booking before explicit review consent | Every autonomous booking case checks the booking table before consent |
| Duplicate commands do not repeat writes | Every patient card action is replayed in the autonomous E2E tests |
| Both hospital adapters retain the correct certainty | Instant confirmation and pending review E2E cases |
| Timeout after commit resolves the original booking | Same-key lookup confirms one existing booking row |
| Timeout before commit cannot trigger another send | Same-key absence leads to a handoff summary |
| Outages and missing documents have a usable exit | E2E cases require a summary, consent and a real queue record |
| The kill switch blocks the autonomous write | Kill switch E2E case requires zero booking rows |
| The patient approves the exact handoff summary | `tests/gateway/test_handoff_consent.py`, under memory and PostgreSQL |
| Constraints remain binding | `test_coordination_constraints.py` and `test_matching_constraints.py` |
| Remote calls retain deadline and quota limits | `tests/agent_runtime/test_inherited_budget.py` |
| The consent view works in a browser | Chromium check through Argent: approve summary, then display the queued view; no appointment confirmation |

Verification results:

- Python unit and contract tests: 2,934 passed. Two fixture checks skip because those scenarios
  have no availability-result dependency.
- Integration tests outside the RAG suite: 515 passed, with no skips. This run includes PostgreSQL
  checks and live telemetry export through the collector and search index.
- Complete E2E suite: 24 passed in each of two fresh iterations. Eight cases run the autonomous
  journey through the real agent services with PostgreSQL.
- Web tests: 168 passed. TypeScript type checking and the production web build passed.
- Mock hospital API tests: 94 passed. TypeScript contract tests: 33 passed.
- Python type checking: 176 source files passed. Repository lint and formatting checks passed.

After the final receipt fix, all 276 gateway unit tests passed. The handoff receipt remains visible
in a refreshed snapshot. It has no action and cannot submit another approval.
The final consent, continuation and surface-restart checks also passed in both control profiles
(10 tests).
All eight autonomous scenarios then passed twice with the final receipt fix, under memory control
and again under PostgreSQL control.

The database run also exposed a seed-verifier gap: it counted policy rows only from migration 014.
The verifier now includes the later additive migrations. All 19 database-backed script checks pass.

## Deployment boundary

These changes were tested locally. Existing Compose services were not redeployed. Apply migrations
015 and 016, then rebuild the gateway, agents and web client to use the changes. Keep the gateway in
a continuously running container so its continuation worker can poll. Build a release manifest from
the new image digest before rollout, and use that release pin. See ADR-0041 for the remaining
platform work and the serverless execution boundary.
