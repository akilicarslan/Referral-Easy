# 9 · Release registry and the version gate

Spec: §13 · ADR-0037 · Code: `packages/release-registry/src/referraleasy_release_registry/`,
`registry/{agents,prompts,releases,versions.json}`, `scripts/check_release_versions.py`,
`services/gateway/.../release.py`, `migrations/012_release_registry.sql`

[← back to the index](README.md)

## What a release manifest is built from

```mermaid
flowchart LR
    subgraph Sources["artifacts.py — read from where they live"]
        A1["agent cards + sub-agent manifests"]
        A2["prompts / instructions"]
        A3["tool specs + implementation digests"]
        A4["skill packages (SKILL.md + manifest)"]
        A5["provider / model / params"]
        A6["guardrail policy · classifier artifact · thresholds"]
        A7["RAG: embedding · index · reranker · knowledge version"]
        A8["git SHA · image digest · lockfile hash"]
    end
    V["versions.py<br/>registry/versions.json<br/>the only place a version is declared"]
    Sources --> H["hashing.py<br/>canonical, order-independent"]
    H --> M["manifest.py<br/>refuses an incomplete manifest"]
    V --> M
    M --> R[("registry/releases/<br/>rel-YYYYMMDD-sha.json<br/>latest.json · index.json")]
    M --> DB[("release_registry table<br/>store.py")]
```

The manifest's top-level keys are `agents`, `prompts`, `tools`, `skills`, `provider`, `rag`,
`guardrail_policy`, `rollout`, `source`, `release_manifest_id` and `parent_release_manifest_id`.

## The CI version gate

```mermaid
flowchart TD
    Base["baseline manifest<br/>registry/releases/latest.json"] --> Cmp
    Head["manifest built from HEAD"] --> Cmp["gate.py — compare component by component<br/>(unit = whole per-agent record,<br/>so sub-agent changes bubble to the parent)"]
    Cmp --> D{"hash moved?"}
    D -- "yes, version did not rise" --> Fail["FAIL — exit 1"]
    D -- "yes, version rose" --> Eval["evaluation required"]
    D -- "no, version rose" --> Eval
    D -- "no change" --> Pass["pass"]
```

> **Gap (README #2):** CI builds no image, so `RELEASE_IMAGE_DIGEST` is not set anywhere and the CI
> job reports a broken gate instead of actually gating.

## Who reads the registry

```mermaid
flowchart LR
    Reg[("registry/releases/latest.json")]
    Reg --> GW["gateway release.py<br/>RELEASE_MANIFEST_ID env → latest.json → empty"]:::built
    GW --> Env["§5 envelope.release_manifest_id<br/>on every coordinator dispatch"]:::built
    Env --> Child["copied unchanged into every<br/>delegated child envelope"]:::built
    Reg --> EvalCLI["make eval<br/>(required input)"]:::built
    Reg --> CI["CI version gate"]:::built

    Pin["pinning.py<br/>conversation pinned to a release"]:::unwired
    Can["canary.py<br/>cohort routing + rollback"]:::unwired
    KS["registry KillSwitch<br/>stops a pinned release, any age"]:::unwired
    Eff["effective.py<br/>effective versions on a task"]:::unwired

    classDef built fill:#e8f4ea,stroke:#2e7d32,color:#1b1b1b
    classDef unwired fill:#fff8e1,stroke:#f9a825,color:#1b1b1b,stroke-dasharray:4 3
```

The gateway names a release on every task, but it does not yet pin conversations. Per-conversation
pinning, canary cohorts and the registry's rollout kill switch are implemented and tested, with no
caller in `services/`. The rollout kill switch is a different control from the admin kill switch in
[11-alerts-killswitch.md](11-alerts-killswitch.md).

## Pinning rules (as implemented in `pinning.py`)

```mermaid
stateDiagram-v2
    [*] --> Pinned: conversation starts on release R
    Pinned --> Pinned: every turn runs under R — no mid-conversation mixing
    Pinned --> Stopped: kill switch on R (works for old pins too)
    Pinned --> Migrating: explicit approval + checkpoint
    Migrating --> Pinned: now on R'
    Stopped --> [*]
```
