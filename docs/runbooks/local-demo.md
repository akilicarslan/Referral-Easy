# Runbook — running the demo locally

Everything here is derived from problems actually encountered running this stack, not from a
prerequisites list written in advance. Each section names the symptom first, because that is what you
will have in front of you.

All data is synthetic (§1). Nothing here contacts a real hospital, insurer or patient.

---

## 0. Setup

```bash
make env          # copies .env.example → .env if absent
make bootstrap    # uv sync --all-packages + pnpm install
make up           # docker compose, core profile
make health       # probes every service the profile declares
```

`make up` needs `docker compose`. `make bootstrap` needs `uv` and `pnpm@12.10.1` (pinned in
`package.json`'s `packageManager`; `pnpm/action-setup` reads it, so the version cannot drift).

**The offline profile is the default and the honest one.** `MODEL_BACKEND=offline` gives
deterministic canned model responses, zero network calls, zero cost, and every artefact is stamped
`synthetic: true`. Anything that would be a *measurement* — latency, cost, guardrail rates, eval
scores — reports "no data" under it rather than a plausible number. Switching to `live` requires real
provider credentials and `scripts/validate_env.py` refuses to start a live backend while any
`CHANGEME_*` pin remains.

---

## 1. `make health` says postgres is DOWN and mentions "password authentication failed"

```
postgres   DOWN   connection to server at "127.0.0.1", port 5432 failed: FATAL: password authentication failed
```

**Port 5432 is already taken by something else on your machine**, and you are talking to *that*
database, not this one. This repo ships `POSTGRES_HOST_PORT=5432`, which is right for a clean
machine and wrong for a busy one — the `.env` comment says so.

Fix, in `.env`, changing both (they must agree):

```
DATABASE_URL=postgresql://referraleasy:referraleasy@localhost:55433/referraleasy
POSTGRES_HOST_PORT=55433
```

Then `make down && make up` so the published port matches. (`make down` does not delete volumes, so
your seeded data and OpenSearch indices survive this.)

**This is worth taking seriously rather than working around.** The failure above means an unrelated
project's Postgres answered your connection. Nothing in this repo writes on a read path, but a
`make seed` or `make migrate` pointed at the wrong server is a mistake you only want to make once.

---

## 2. `make health` says opensearch is DOWN, but the container is healthy

Symptom: `make health` reports DOWN while `docker compose ps` shows opensearch healthy, and so does
`curl http://localhost:9200`.

Cause: the shipped `.env` addresses services by their **compose service name**
(`http://opensearch:9200`), which resolves inside the compose network and not on your host.

`make health` already swaps known service names for `localhost`, so if you see this, the address is
one it does not know. Check with:

```bash
curl -s http://localhost:9200/_cluster/health
```

The RAG package solves the same problem independently and slightly more thoroughly — see
`HOST_FALLBACK_OPENSEARCH_URLS` in `packages/rag/src/referraleasy_rag/config.py`, which probes a list
of host-reachable candidates whenever the value came from `.env` rather than from the process
environment. That distinction matters: **a value in the process environment is taken as an explicit
choice and gets no fallback**, so if you export `OPENSEARCH_URL` you own whatever it says.

---

## 3. The test suite passes, then fails on a second run with "no bookable … slot"

`tests/e2e` is **not idempotent, by design**. It drives bookings against the mock API, which has no
reset endpoint, so slots it consumed on the first run are gone on the second and fixture discovery
fails with a message that reads like a product bug.

Use the target that arranges the precondition:

```bash
make e2e     # restarts the mock API on a clean database first
```

That is also why `e2e` is deselected from the default `pytest` run and has its own CI job. If you run
`pytest tests/e2e` directly, you are responsible for the state — pass `-m e2e`, because the default
`addopts` deselects the marker and without it pytest collects nothing and exits 5.

---

## 4. Integration tests "pass" in seconds when they should take longer

They did not run; they **skipped**. The `integration` marker gates on a service being reachable, and
a skip exits 0 — so a green run that skipped everything reads exactly like a green run that proved
everything.

```bash
make test-integration        # 330 tests, needs postgres. Refuses to start without TEST_DATABASE_URL
make test-integration-rag    # 13 tests, needs OpenSearch
```

`make test-integration` deliberately **refuses to run** when `TEST_DATABASE_URL` is unset rather than
reporting a green run over nothing, and CI fails its integration job if any of those tests skip.

The 13 OpenSearch-backed tests are the only integration tests with **no CI job** — a service
container for OpenSearch is fiddly enough that an unverifiable job would be worse than this
documented gap, so they run only where someone runs them.

---

## 5. `pnpm install` exits 1 with `ERR_PNPM_IGNORED_BUILDS`

pnpm blocks every dependency's install script unless it is allowed, and exits non-zero when it
blocked any. The allowlist lives in `pnpm-workspace.yaml`:

```yaml
allowBuilds:
  esbuild: true
```

The key is `allowBuilds` and it takes a **map**. The older `onlyBuiltDependencies` list is not read
by pnpm 12 — setting it changes nothing and install still exits 1. When a new dependency needs
approving, use the command that edits this file correctly rather than remembering the key name:

```bash
pnpm approve-builds <package> --yes
```

CI runs `pnpm install --frozen-lockfile`, so a package missing from this list fails the build **at
the install step**, which reads as a broken repo rather than as a pnpm policy.

---

## 6. A test passes alone but fails in the full suite (or vice versa)

Almost always one of two pytest collection footguns, both of which have guard tests so they cannot
come back:

- **A bare `from conftest import X`.** pytest puts every `conftest.py` on `sys.path` under the name
  `conftest`, so `sys.modules["conftest"]` is whichever one it imported *last* — an arbitrary choice
  that depends on the order directories were given on the command line. Put shared names in a
  uniquely named module instead (`tests/skills/skills_fixtures.py` is the pattern).
- **Two test modules with the same basename.** pytest's default import mode imports test modules by
  basename, so `tests/domain/test_policy_table.py` and `tests/gateway/test_policy_table.py` collide
  and pytest refuses to collect **either** — `import file mismatch`. That is a hard stop for the
  entire suite, not a failure in those two files. The hint it prints suggests deleting `__pycache__`;
  that works once and leaves the collision in place. Rename one.

Both are enforced by `tests/test_test_layout.py`, which fails with an explanation rather than letting
you find out from a confusing error.

---

## 7. `make spike` fails rather than skipping

That is deliberate and it is the point of the spike suite. A spike has exactly two honest outcomes:
it runs for real, or it fails loudly naming the environment variable that is missing. A **skipped**
spike is reported as neither pass nor fail, so a green build would hide the fact that Phase 0 never
completed.

Spikes need real provider credentials and a network. They are deselected from the default run and
from CI, and CI asserts they stay *collectible* so that deleting the marker cannot make the guard
pass by collecting nothing.

---

## 8. Teardown

```bash
make down     # stops the stack; named volumes are kept
```

`make down` removes orphans across all three profiles. It does **not** delete volumes — `pg-data` and
`opensearch-data` survive, so a restart keeps your seeded data and indices.

To stop one thing rather than the stack, prefer stopping a single service:
`docker compose stop <service>`. A partial `docker compose down` while another process is using the
same cluster produces failures that point at the wrong service.
