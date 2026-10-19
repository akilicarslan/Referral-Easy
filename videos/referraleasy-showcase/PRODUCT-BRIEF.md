# Product and claim selection

ReferralEasy is a runnable portfolio platform for referral coordination. It demonstrates controlled
multi-agent work. It does not accept real patients, make clinical decisions, or connect to real
insurers or hospitals. The showcase presents implemented safeguards without claiming a deployed,
production-certified healthcare product.

The business value is one guided path through a fragmented referral process. The patient retains
provider, appointment and consent choices. Evidence and uncertainty stay visible.

The architecture has a Lit/A2UI client, a FastAPI REST/SSE gateway, seven independent ADK/A2A agent
services, PostgreSQL authority, mock hospital adapters, recovery workers, hybrid OpenSearch
retrieval, and OTel/OpenSearch telemetry. Gateway commands own policy and writes. Specialists supply
typed results. Independent coverage and matching run together. Durable continuations resume patient
answers without holding open model calls. The routine verified path uses deterministic tool calls;
the film does not imply an LLM independently invents its orchestration.

## Features selected for the minute

| Claim | Source and evidence |
| --- | --- |
| Guided hospital → doctor → slot → review flow | `apps/web/src/components/`, `tests/e2e/test_autonomous_coordination.py` |
| Seven A2A roles and independent parallel work | `services/agents/referraleasy_agents/workflow.py`, `tests/agents/test_workflow_execution.py` |
| Policy and exact-payload consent | `services/gateway/src/referraleasy_gateway/commands.py`, `tests/gateway/test_approval_binding.py` |
| Same-key reconciliation after lost response | `tests/e2e/test_autonomous_coordination.py`, `tests/e2e/test_scripted_booking.py` |
| Durable claims, restart recovery, stale completion refusal | `services/gateway/src/referraleasy_gateway/continuations.py`, `tests/gateway/test_coordination_continuations.py` |
| Unknown coverage, missing documents, bounded outages, stale slots | `fixtures/scenarios.json`, autonomous and scripted E2E tests, `tests/rag/` |
| Consented handoff and human ownership | `tests/gateway/test_handoff_consent.py`, `tests/workers/test_hitl.py` |
| Distributed spans across the gateway and agents | `packages/telemetry/`, `docs/architecture/autonomous-coordination.md` |

Scope and completion sources: `README.md`, `ReferralEasy_Architecture_v3_EN.md`, ADR-0041,
`docs/architecture/autonomous-coordination.md`, `docs/architecture/workflow-surface-projection.md`.
Older demo-scenario narrative status is stale; current completion records take precedence.

Do not claim: measured speed/cost improvement, evaluation scores, a deployed cloud service,
end-to-end output-guardrail adoption, a working live reranker, real eligibility, or clinical safety
certification. These remain incomplete or unmeasured. Historical test counts are deliberately absent
from the film. Failure-path graphics illustrate code-backed behavior, not a new certification run.

## Presentation revision

Lead with the ReferralEasy name and explain what the platform does. Present the fragmented referral
process in everyday terms, then walk through hospital, specialist, appointment time and approval.
Use conversational enterprise presentation language. Introduce engineering detail through the
architecture and failure-path examples. The user waived ASD-STE100 for this video only.
Keep the business/engineering split at 18/42 seconds and retain the accepted steady techno bed.
The tiny technical reconstruction label is replaced by a fixed, readable synthetic-demo note.
