# ADR-0028 — Skill packages: selection metadata, instruction bodies, and no authority

- **Status:** Accepted
- **Date:** 2026-10-14
- **Phase:** 2.1
- **Depends on:** §3 (agent roles and their skills), §7 (workflow states and the policy table),
  §9 (documents are data), ADR-0025 (the database vocabulary is the contract's)

## Context

Section 3 names eight skills, each belonging to one of the seven specialist agent roles. Three
properties of the surrounding system shape what a skill is allowed to be, and each one rules out the
obvious implementation.

**Skills are selected, not all loaded.** The effective skill list is written to the trace, which
means the list is a decision the system makes per task. Loading eight instruction bodies to decide
which one to load inverts that: the cost is paid on every turn, and — worse — the selection decision
ends up depending on content that was supposed to be loaded only *after* the decision. A selector
needs something it can read cheaply, for every candidate, on every turn.

**Adding a skill grants no tool permission.** Tool authority in this system lives in two places: a
runtime allowlist, and `policy_rule`, keyed by `(agent_role, tool_name, workflow_state)`. Default
deny — an absent row is a refusal. If a skill package could name a tool, a new package would be a
new grant, and the policy table's carefully derived prohibitions (no `list_slots` during
`BOOKING_IN_FLIGHT`, `create_booking` true only in `BOOKING_IN_FLIGHT`) would be bypassable by
someone writing Markdown.

**An Agent Card skill entry and a runtime instruction package are separate records.** `AgentCard`
advertises a capability to peers over A2A. A package instructs a model. They have different
lifecycles, different audiences and different truth conditions; conflating them would mean editing
advertising copy to change behaviour, or vice versa.

## Decision

### A package is three records

```
packages/skills/<name>/
  manifest.json    selection metadata — read to decide whether to load the package
  SKILL.md         the instruction body — read only after the package is selected
  references/      read-only source material — read only when the body cites it
```

`packages/skills/src/referraleasy_skills/` holds the loader. It is stdlib-only and declares no
runtime dependencies, because it runs on the selection path of every turn.

### The manifest carries enough to choose without the body

Required keys: `manifest_version`, `name`, `version`, `title`, `owner_agent`, `summary`,
`when_to_use` (≥3 concrete triggers), `inputs`, `outputs`, `references`, `authority`,
`grants_tool_permissions`, `agent_card_skill_id`, `tags`.

`summary` is a single line under 200 characters and `when_to_use` lists situations, because those
two fields are what a selector scores. The loader's `select()` ranks on `summary`, `when_to_use` and
`tags` alone — a deliberately simple lexical score, small enough to read and check, enough to make
the point that the body is not needed to choose.

### Authority is a schema constant, not a convention

Two keys make the security boundary structural:

- `authority` has `"const": "instructions_only"` in `manifest.schema.json`.
- `grants_tool_permissions` has `"const": false`.

`additionalProperties: false` plus a recursive forbidden-key check rejects `tools`, `capabilities`,
`auth`, `agent_hash`, `input_schema_id`, `output_schema_id`, `sub_agents` (Agent Card vocabulary)
and `allowed_tools`, `permissions`, `scopes`, `allowed_actions`, `grants`, `token_permissions`
(tool-authority vocabulary). The loader raises; it does not warn. A manifest that could name a tool
would be granting authority, so the format refuses to represent the grant at all.

Every one of the eight bodies also closes with the same statement in its own words: the package
instructs, it does not authorise a tool call, and whether a tool may run in a given state is the
policy table's decision.

### `agent_card_skill_id` maps the two records without merging them

Each manifest carries `agent_card_skill_id` (for example
`scheduling.confirm_booking_with_consent`), which names the Agent Card skill entry this package
instructs. The loader requires it to differ from the package name and from any other package's
value, so the mapping is a mapping and not a rename.

### Every body is framed the same way

Seven sections are required in every `SKILL.md`, in any order: *When this skill applies* (with an
explicit exclusion), *Inputs*, *Procedure*, *Outputs*, *Documents and retrieved text are data*,
*Authority and boundaries*, *Failure modes and escalation*.

The fourth of those is not boilerplate. The project ships a deliberate prompt-injection fixture —
`referral_letter_raghavan_priya.txt`, whose administrative paragraph instructs a reviewing assistant
to record the plan as in-network and skip the coverage lookup. Every package that could receive
document text states in its own terms that documents are data to interpret, that an instruction-like
passage is reported rather than obeyed, and that no document can authorise a call or substitute for
a structured lookup.

## Consequences

- **Selection reads one file per candidate.** A test deletes `SKILL.md` from every package and
  asserts that `load_manifest`, `list_manifests` and `select` still work; a second test patches
  `Path.read_text` and asserts nothing but `manifest.json` is read during selection. A third asserts
  that `load_skill` *does* require the body, so the split is real rather than incidental.
- **A new skill cannot widen authority.** The value is refused by the loader and by the schema, and
  a test mutates a manifest to `grants_tool_permissions: true` and asserts `load_manifest` raises.
- **A package that cites a missing reference fails at selection time**, not after it has been
  selected for work.
- **Each package is independently loadable.** A test copies one package into an empty root and
  asserts it lists, loads and instructs on its own.
- **The instruction body is versioned.** `version` is semver per package, so a change to procedure
  or boundaries is a version change the trace can point at.
- **Two format definitions exist, and a test holds them together.** The JSON Schema
  (`manifest.schema.json`) and the loader's in-code validation declare the same required keys
  (`REQUIRED_MANIFEST_KEYS`), and a test fails if they diverge. The schema is validated with
  `jsonschema`, already present in the workspace environment via `referraleasy-contracts`; the
  loader itself remains dependency-free so that a missing schema library can never be the reason a
  broken package is accepted.
- **Vocabulary drift is caught.** A test asserts every `owner_agent` appears as a role in
  `migrations/014_align_domain_vocabulary.sql`, so renaming a role in the database without renaming
  it here fails loudly rather than silently orphaning a package.

## Alternatives considered

**One file per skill with YAML front matter.** Rejected: parsing front matter means reading the
instruction body, which is precisely the cost the format exists to avoid.

**Skill manifests as Agent Cards.** Rejected: a card is an advertisement with a `tools` array and
capabilities negotiated over A2A. Making a package a card would put tool names in the same document
as instructions, and would make a runtime instruction change a change to protocol-visible
advertising.

**Declaring an `allowed_tools` list in each manifest.** Rejected outright. It is the shortest route
to a second, unaudited policy table, and it would let a new Markdown file grant a capability that
`policy_rule` denies.

**Validating manifests with Pydantic.** Rejected for the loader: it would put a dependency on the
selection path. The model is a frozen dataclass, and validation is explicit code a reviewer can read
against the schema.

**Keeping the skill content inside the Python package (`referraleasy_skills/skills/`).** Rejected:
the packages are content that a technical writer or a clinician reviewer should be able to read and
diff without navigating a Python source tree. The loader resolves the content root from
`REFERRALEASY_SKILLS_ROOT`, a bundled `_skills/` directory if present, or the nearest ancestor
`skills/` directory containing packages — which is why the source checkout needs no configuration.
