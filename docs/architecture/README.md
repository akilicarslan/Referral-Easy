# Architecture

The source of truth is [`../../ReferralEasy_Architecture_v3_EN.md`](../../ReferralEasy_Architecture_v3_EN.md)
(v3.0, 2026-10-14, English). It is not duplicated here — a copy would drift.

Section references used throughout the code (`§7`, `§9`, …) point into it.

The derived, checkable work plan is
[`../../IMPLEMENTATION_TODO.md`](../../IMPLEMENTATION_TODO.md).

Mermaid diagrams of the system as built — a system overview and one detailed page per feature
(guardrails, agents/A2A, workflow state machine, booking and reconciliation, HITL, RAG, telemetry,
release registry, evaluation, alerts, web/A2UI) — are in [`diagrams/`](diagrams/README.md).

## Spec section → code

A section maps to more than one module whenever the layer boundary runs through it, which is most of
the time. The left column is a spec heading, not a package.

| Section | Topic | Code |
| --- | --- | --- |
| §1 | Synthetic data only | enforced in the schemas (`synthetic` is `const: true`); `fixtures/` |
| §3 | Agent boundaries, the forbidden-authority table | `services/agents/referraleasy_agents/forbidden.py` + `tests/agents/test_forbidden_capabilities.py` |
| §4 | Collaboration flow, workflow states, policy table | `services/gateway/src/referraleasy_gateway/coordination.py`, `services/agents/referraleasy_agents/workflow.py`, migrations 014–016, `packages/domain/` |
| §5 | A2A task contract, Agent Cards | `packages/contracts/schemas/task.json`, `packages/contracts/python/.../a2a.py`, `services/agents/referraleasy_agents/cards.py` |
| §6 | Context minimization, skills | `packages/skills/`, `packages/agent-runtime/skills.py` |
| §7 | Workflow durability, ownership, leases | `migrations/` (the triggers), `packages/domain/` (the client), `services/workers/` (the workers) |
| §8 | Deadlines, retries, budgets, providers | `packages/agent-runtime/` (`budget.py`, `retries.py`, `model.py`) |
| §9 | Three-layer guardrails, authorization | `packages/guardrails/` (the layers), `services/gateway/guardrails.py` (the seam), `packages/agent-runtime/tools.py` (the allowlist) |
| §10 | Hybrid RAG and the degradation ladder | `packages/rag/`, `infra/opensearch/index-templates/referraleasy-directory-vector.json` |
| §11 | Human in the loop | `services/workers/hitl.py`, `packages/domain/handoff.py`, `services/gateway` (the queue) |
| §12 | A2UI components, SSE, admin | `apps/web/`, `packages/contracts/schemas/sse.json`, `services/gateway/{events,surfaces,projection}.py` |
| §13 | Versioning and the release registry | `packages/release-registry/`, `registry/`; the gateway resolves the dispatch release at startup, but per-conversation pinning remains incomplete |
| §15 | OTel/OpenSearch analytics | `packages/telemetry/`, `infra/otel/`, `infra/data-prepper/pipelines.yaml` |
| §17 | Mock hospital API and fault injection | `services/mock-api/`, `fixtures/fault-scenarios/` |
| §18 | Compose, env contract | `compose.yaml`, `.env.example`, `scripts/{health,seed,validate_env}.py` |
| §19 | Environment contract | `.env.example` is the tracked copy; `.env` never is |
| §20 | The eight demo scenarios | `fixtures/scenarios.json` (the data), [`../demo-scenarios/README.md`](../demo-scenarios/README.md) (the narrative) |
| §22 | Repository layout | the tree in the root README |

## Cross-cutting

Two rules are enforced in more than one place, and it is worth knowing where, because a fix in one
alone is not a fix:

- **"PostgreSQL is the primary store" (§7)** is enforced by the schema's triggers *and* by
  `packages/domain`, which only translates the refusals. `services/gateway` runs the same test suite
  against an in-memory backend and a Postgres one, so a rule that exists in only one of them fails CI.
- **"A skill grants no tool permission" (§6)** is enforced by the manifest schema (there is no field
  that could carry a grant), by the loader (it raises on a mutated manifest), and by a test requiring
  every skill body to repoint authority to the runtime allowlist.

## Decisions

Decisions taken while implementing are recorded in [`../adr/`](../adr/), each dated with its
consequences — including the negative ones. Where an ADR and the plan disagree, the ADR is what was
actually built, and it says why.

## Operational

[`../runbooks/`](../runbooks/) has the symptom-first troubleshooting guide: the failures that
actually occurred running this stack, and what causes them.

Current integration review and verification: [2026-10-18 review](review-2026-10-18.md).

Standard coordination completion: [2026-10-19 evidence](autonomous-coordination.md) and
[ADR-0041](../adr/ADR-0041-autonomous-coordination.md).

Card lifecycle and restart repair: [workflow surface projection](workflow-surface-projection.md).

Continuation claims, expiry and recovery: [durable continuation lifecycle](durable-continuation-lifecycle.md).
