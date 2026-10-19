# ADR-0037 — The release registry: a manifest that cannot be incomplete, a gate that can fail

- **Status:** Accepted
- **Date:** 2026-10-15
- **Phase:** Phase 3.5 — versioned agent/prompt/skill release registry (§13)
- **Depends on:** §13 (the immutable release manifest, the version-bump rule, effective versions,
  conversation pinning, the kill switch, canary/rollback), `migrations/012_release_registry.sql`,
  `referraleasy_contracts.generated.workflow` (`ReleaseManifest`, `Agent`, `Tools`, `Provider`,
  `GuardrailPolicy`, `Rag`, `Source`, `Rollout`), ADR-0028 (Agent Cards — the card hash this
  manifest deliberately reuses), ADR-0033 (the ADK runner the prompts belong to)

## Context

§13 asks for the thing that makes a version a fact rather than a habit: an immutable record of what
each agent *is* — its instructions, its tools and their implementation, its skills, its sub-agents,
its model, its guardrails, its knowledge index — plus a rule that no artifact may change without its
declared version rising, plus the machinery to pin work to a release and to stop or move it
deliberately.

The requirements below are translated into English. Python code refers to this section as the
canonical reference:

> §13: *"CI fails if the affected agent version has not been incremented. Even PATCH counts as a
> behavior change and triggers evaluation."*

> §13: *"if a sub-agent changes, the parent dependency hash and version are updated"*

> §13: *"Runtime tasks carry their effective versions; the `latest` label is insufficient for
> reporting."*

> §13: *"A conversation is pinned to a release"* · *"The safety kill switch can also stop a
> conversation pinned to an older version"* · *"migration to a new version requires explicit
> approval/a checkpoint"*

> §13: *"Canary cohorts and rollback routing can use a simple registry table; a separate platform is
> not required."*

What existed before this phase, and what did not: `migrations/012_release_registry.sql` already
defined `release_registry`, `release_event` and the `release_status` view, with append-only triggers
(`refuse_row_mutation()`, SQLSTATE `RE008`) and the grants that keep them append-only.
`packages/contracts` already defined `ReleaseManifest` and its children. `registry/` held two empty
directories and no `releases/`. `make eval` already read `registry/releases/latest.json`, which did
not exist, so that target could not run. No code built a manifest, nothing wrote a row, and §13's
"every runtime task carries its effective versions" had nothing to carry.

## Decision

### One manifest, built by reading the artifacts, with no field defaulted

`packages/release-registry` (`referraleasy_release_registry`) builds the document:

- `artifacts.py` reads the shipped objects — `cards.agent_hash`, `prompts.build_instruction` plus the
  assembler's sub-agent directive, `tools/specs.py` `ToolSpec`s with each handler's
  `inspect.getsource` digest, the skill packages through `referraleasy_skills`, `subagents.py` and
  each role's `__main__.py` — as the table at the top of that module records, one row per §13 field.
- `manifest.py` assembles `ReleaseManifest`, then `assert_complete` refuses it if any field §13 names
  is absent, empty where it must have content, or not the right shape. The contract types most fields
  as optional (it also describes documents a builder is still filling in), so the *reader* side asks
  through `required_field` and raises rather than substituting `""` or `None`: a manifest that omits a
  field is worse than one that errors, because the omission is invisible at report time.
- `versions.py` reads `registry/versions.json`, the one place a version is *declared*. A hash is
  derived and cannot be wrong; a version is declared and cannot be derived — no function can know
  whether a prompt edit is a PATCH. The gate is the join of the two.

### The hash is over content, and the metadata is excluded on purpose

`artifact_hash` is the digest of the manifest **minus** `release_manifest_id`, `created_at`,
`parent_release_manifest_id` and `rollout`. `manifest_hash` is the digest of the document as stored.
The release id is `rel-<YYYYMMDD>-<artifact_hash[:7]>`, so two builds of one tree on one day share an
id — and `release_registry_release_id_key` then refuses the second registration, which is correct:
it is the same release.

The consequence that surprised a test and is now asserted: **a canary cannot be a second label on one
artifact.** Because `rollout` is excluded, a stable and a canary manifest built from one tree have the
same `release_manifest_id`; a release that is genuinely a canary differs in *content* (a bumped prompt
version) and therefore has its own id. `tests/release/test_canary_routing.py` builds its canary that
way and asserts the two ids differ.

The gate's unit is the **whole per-agent record** (`agent_artifact_hash`), including `version` — not
the card's published `agent_hash`. That choice is what makes §13's parent/sub-agent rule true by
construction: the card hash never reads the sub-agent directive or the sub-agents' own instructions
(`artifacts.AGENT_HASH_GAP`), so a gate keyed on it would let a sub-agent's instruction change while
its parent's version stood still. `tests/release/test_subagent_parent_bump.py` proves the chain by
mutating a sub-agent instruction and asserting the parent's `sub_agent_manifest_hash`,
`agent_artifact_hash` and release hash all move.

### The gate fails as two findings, and that is not a bug

A change to any agent also changes the release document that carries it, so the gate reports both
`agent-<role>` and `referraleasy`. A person who bumps only the agent meets the second finding next.
`tests/release/test_version_gate.py` and `test_subagent_parent_bump.py` assert the pair, including the
case where bumping only the agent is *not enough*.

### Storage: the existing schema, no second one

`store.py` writes the eight rows a release needs (seven agents plus `referraleasy`) into
`release_registry`, each carrying the full manifest, and records `release_event` rows for
`staged|activated|rolled_back|revoked`; `release_status` reads the latest event per release and
environment. Re-registering an identical release is a no-op; registering a moved hash against an
existing `(component, version)` raises `RegistryConflictError` naming the component and saying "bump
the version". The writer's assumptions are asserted against the migration text itself — every table,
column, constraint, view and trigger — because a writer that drifts from its schema is discovered in
production otherwise.

### The remaining §13 mechanisms, one module each

- `effective.py`: the resolved record a runtime task carries — sixteen fields, every one a version, a
  hash or an id that was true when the task started. `resolve_release` refuses the label `latest`.
- `pinning.py`: conversation pinning, the kill switch, and the approval a migration requires. Pinning
  is idempotent **even when handed a newer release** — the anti-mixing rule — and the kill switch
  answers by the pin's identity, never its age, so a conversation three releases back is still
  stoppable.
- `canary.py`: routing from the registry table. The bucket is `sha256(salt|conversation_id)`, stable
  across processes (asserted by running it in two subprocesses with different `PYTHONHASHSEED`s), a
  rollback is honoured whether it was recorded as an event or in the rollout block, and a pinned
  conversation that is also rolled back is *reported*, not silently moved.
- `gate.py` / `cli.py`: the check, and its four exit codes.

### `scripts/check_release_versions.py`: four exit codes, because two are not enough

| code | meaning |
| ---- | ------- |
| 0 | the gate passed |
| 1 | the gate failed — an artifact changed and its version did not increase |
| 2 | usage error |
| 3 | the gate could not run: a missing artifact, an unset environment value, an incomplete manifest, or no baseline |

`3` is not `0`. A missing baseline means the gate did not run, and a CI job that read "could not run"
as "passed" would pass forever after someone deleted the file; `--initial` exists for the genuinely
first release and must be asked for.

### The gate is shown to fail, not asserted to

`tests/release/test_check_release_versions_script.py` runs the script as CI runs it, in a `tmp_path`
copy of the tree (three other agents share this working tree), and mutates real shipped text four
ways — a prompt sentence, a tool description, a sub-agent directive, a skill body — asserting exit 1
with `version_not_advanced` each time. Then it bumps the agent (still exit 1, now naming only the
release), then the release (exit 0, with `evaluation required for: agent-provider_matching,
referraleasy`). A check that cannot fail is not a check.

## Two defects this work found, and fixed

1. **`cli._artifact_views` called `roles.CARD_NAME_BY_ROLE[role]`, which does not exist** — the
   mapping lives in `referraleasy_agents.cards`. Every `build` failed with an `AttributeError` (exit
   3) and no unit test behind the CLI saw it, because the tests called the library, not the entry
   point. Found by the subprocess test; fixed to `card_name(role)`.
2. **The skills a build hashed were not the skills under the root it was describing.**
   `referraleasy_skills` resolves `packages/skills` from **its own installed location** and takes no
   root argument, so `build --repo-root <copy>` hashed the original tree's `SKILL.md` files — a
   manifest that names skills it never read, and a gate blind to a skill edit in the tree it checks.
   `skill_artifacts(root)` now points the loader at `<root>/packages/skills` through the loader's own
   documented override (set and restored around the reads, so importing the module has no side
   effects), and **refuses** when `<root>/packages/skills` is absent rather than falling back.

## Findings — what §13 asks for that this phase could not fill, and why

1. **`source.git_sha` is unfillable in this checkout, and `build` refuses without it.** There is no
   commit anywhere in this repository (`git rev-parse HEAD` fails), so there is no honest value for
   the field: a substituted one would be indistinguishable from a real sha in every registry row.
   `build` therefore exits 3 with an explicit message. The one command that produces
   `registry/releases/latest.json` once a commit exists:
   `uv run --no-sync python scripts/check_release_versions.py build --git-sha "$(git rev-parse HEAD)"`
   (plus `--image-digest` when an image has been built). Consequently **`registry/releases/latest.json`
   is not written**, and `make eval` cannot read it — which was already true for a second reason:
   the `referraleasy_eval` package the target invokes does not exist in this tree. `source.image_digest`
   *is* fillable and was checked against the image this machine has built
   (`referraleasy/agents:dev`, id `sha256:74480376a2485ad1afba621da83adc88d660ae6703d237ffb72a77f71213eb33`);
   a *registry* digest still requires a pushed image, since a locally built one has no `RepoDigests`.
2. **The conversation pin has no table, and this package may not add one.**
   `release_registry` records releases and `release_event` records what happened to a release; neither
   has a conversation to hang a pin on. Rather than invent a second schema, `pinning.py` implements the
   mechanism — value, store contract, approval — and `PIN_TABLE_DDL` is the exact DDL a migration
   would need. A mechanism with an injected store is testable; a hand-rolled table would have been a
   second schema.
3. **`AGENT_HASH_GAP`.** The Agent Card's published `agent_hash` does not cover the sub-agent directive
   or the sub-agents' own instructions. The manifest closes the gap (`instruction_hash`,
   `sub_agent_manifest_hash`) and the gate keys on the record, but a *card* consumer outside this
   repository still sees the narrower hash. Named in `artifacts.AGENT_HASH_GAP` so the next reader
   meets it in the code, not in a ticket.
4. **`COMPONENT_REMOVED` is not reachable from the CLI today.** The gate can report a component that a
   manifest no longer contains, and `test_version_gate.py` covers that branch by building manifests
   directly; the CLI builds both sides from one ledger, so a removed role currently fails earlier, at
   `load_ledger`/`assert_complete` (exit 3). Stated in the test's docstring rather than left as a
   branch that looks exercised.

## Consequences

- **The root `pyproject.toml` and `Makefile` need four edits**, which this phase did not make because
  other agents own those files: add `packages/release-registry` to `[tool.uv.workspace] members`, to
  `[tool.uv.sources]`, to the project's `dependencies`, and to `[tool.ruff] src`; and add its
  `src` directory to `PYTHON_SOURCE_ROOTS` in the `Makefile` (which `make typecheck-python` passes to
  `mypy` — the package is `strict`-clean today). Until then, `scripts/check_release_versions.py` and
  `tests/release/release_fixtures.py` carry the same documented `sys.path` bridge.
- **A CI job is the user's to add.** `scripts/check_release_versions.py` is written to be that job:
  exit 1 fails the build, exit 3 fails it too (and says the environment is wrong, not the change).
- **Determinism is a property, not a hope.** Canonical JSON (`sort_keys`, `(",", ":")`, no timestamps
  from the filesystem), two builds byte-identical, a key-order permutation that does not move a hash,
  and a canary bucket identical in two processes with different hash seeds. All asserted in
  `tests/release/test_hashing_determinism.py`.
