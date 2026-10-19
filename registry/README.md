# `registry/` — the declared versions and the built releases

## What a human edits

`versions.json` is the **only** hand-written file here. It is the one place a version is
*declared*, and it is deliberately tiny: a version is a judgement (nobody can compute
whether a prompt edit is a PATCH), so it cannot be derived, while every hash in a release
manifest *is* derived and must never be typed by hand.

```
release   0.1.0     the release as a whole
agents    <role>    one entry per role in `referraleasy_agents.roles`
prompts   <role>    bumps independently of the agent whose instructions they are
skills    <name>    one entry per package under `packages/skills`
```

§13's rule, and the whole reason this file exists: **any** change to an agent's
description, instructions, tool descriptions, skills or sub-agents moves that agent's
artifact hash, and the agent's declared version must rise to match — even a PATCH. A
sub-agent's change also moves its parent's dependency hash and the parent's version.

## What `build` writes (do not edit)

| path | what it is |
| ---- | ---------- |
| `releases/<release_manifest_id>.json` | the immutable manifest — the artifact of record |
| `releases/latest.json` | the pointer `make eval` reads |
| `releases/index.json` | every release, newest first |
| `agents/<card-name>.json` | one agent's view: hashes, model, tools, sub-agents |
| `prompts/<role>.json` | one role's instruction hash and prompt version |

What *happened* to a release — `staged`, `activated`, `rolled_back`, `revoked` — is not a
file here. It is a row in the `release_event` table (§13), because it is an operational
fact about a deployed release and belongs next to the registry that serves it, not in a
checkout. This directory holds what a release *is*.

These are outputs of `referraleasy_release_registry`, not inputs. Do not hand-edit one:
the next `build` overwrites it, and a hand-edited manifest is a release whose hash
describes a tree that does not exist.

## Built, and how to rebuild it

```sh
uv run --no-sync python scripts/check_release_versions.py build \
  --git-sha "$(git rev-parse HEAD)" \
  --image-digest "$(docker image inspect referraleasy/agents:dev --format '{{.Id}}')"
```

The output directories are populated — `releases/rel-20261016-8e00275.json` and its
`latest.json` and `index.json`, beside the per-artifact views under `agents/` and `prompts/`
— and they are committed, because a release manifest is the artifact of record that
`make eval` and the CI gate both read. The command above regenerates them; the committed
copies are what a reviewer diffs.

Two things about it are deliberate and worth knowing before it surprises you:

- **`build` refuses without a git sha** (exit `3`). §13 records the commit in an append-only
  registry, and a substituted sha would be indistinguishable from a real one forever.
- **`--built-at` pins the build time, and the release id contains the date.** Without it,
  `build` uses the clock, and two builds of one tree on different days are two releases —
  `rel-20261015-<hash>` and `rel-20261016-<hash>`. The CI gate is written for that: a rebuild
  of an unchanged tree has a new id and still passes, because the comparison is per artifact
  and not by release id. Pin `--built-at` when you want byte-identical output to diff.

Note the image digest: this is a **locally built** `sha256:…` from `docker image inspect`, not
a registry digest, because no registry is configured. That is the honest value for a checkout
that has built its own images; a pushed image should supply its `RepoDigests` entry instead.

See `docs/adr/ADR-0037-release-registry-version-gate.md` for the design and the findings.
