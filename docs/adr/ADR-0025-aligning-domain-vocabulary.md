# ADR-0025 — The database vocabulary is aligned to the contract, not the reverse

- **Status:** Accepted
- **Date:** 2026-10-14
- **Phase:** 1.4
- **Supersedes part of:** the vocabulary in migrations 003 and 005

## Context

Migration 003 (workflow states) and 005 (roles, tools, policy) introduced their own vocabulary:
**nine coarse workflow states** (`intake`, `triaged`, `awaiting_approval`, `booking_pending`,
`booked`, `human_required`, `resolved`, `failed`, `cancelled`), **six invented agent roles**
(`triage_agent`, `booking_agent`, …) and **ten invented tool names** (`find_slots`,
`extract_referral`, …).

None of them match the source of truth. `WorkflowState` in `packages/contracts/schemas/common.json`
carries **sixteen** states derived from §4's flow and §2.5's booking lifecycle, and §3 names seven
specific agents and their tools.

**The cause was procedural, and it was self-inflicted.** The agent that wrote those migrations was
instructed *not* to read `ReferralEasy_Architecture_v3.md` — a context-budget measure that was
necessary at the time (see the note on fan-out below) but which meant it had no state vocabulary to
work from and invented one. The transcribed brief it received named the tables and the invariants,
but not the states those tables would hold.

## Why coarse states were not merely differently named

The vocabulary was **too coarse to express the policy §4 requires**, and this is visible in the rows
it produced rather than being a matter of taste:

| Old row | Why it is wrong |
| --- | --- |
| `booking_agent` may `find_slots` in `awaiting_approval` **and** `booking_pending` | No state separated "choosing a slot" from "a booking is in flight", so re-searching for slots *during a booking* could not be denied. That is one of the ways a second booking gets started. |
| — | There was no `READINESS_CHECK` state at all, so §2.3's document-readiness step could not be policy-gated; any tool could run during it. |
| — | §4's ordering rule — a *provider-level* network check happens only after the doctor is known — had no state to hang on. |

A policy table is keyed by state. A vocabulary that cannot distinguish the states cannot enforce
the policy, however carefully the table is written.

## Decision

**The contract vocabulary wins**, and `migrations/014_align_domain_vocabulary.sql` replaces the
persisted one. The contract is derived from §4 and §2.5; the database vocabulary was derived from
nothing. States are now stored as the exact `WorkflowState` codes (`AWAITING_FINAL_APPROVAL`,
`BOOKING_IN_FLIGHT`, …), which also removes a case-mapping layer between the two.

Concretely, 014:

1. Replaces `workflow_state` with the 16 contract states and `workflow_transition` with §4's flow.
   Reaching `AWAITING_FINAL_APPROVAL` now requires passing hospital → doctor → readiness → slot.
   **There is no shortcut edge**, which is what makes the consent guard on `BOOKING_IN_FLIGHT`
   meaningful rather than decorative.
2. Replaces `agent_role` with §3's seven specialists, and `tool_registry` with the tools §3 names.
3. Re-derives `policy_rule` from §3 and §4 — including the two prohibitions the old table could not
   express, and asserting them at the end of the migration so a drift back to a coarse vocabulary
   fails loudly instead of silently relaxing policy.
4. Restates two functions whose logic gates on a state *name*, not merely a message:
   `approval_guard` (which requires the workflow to be in `AWAITING_FINAL_APPROVAL`) and
   `handoff_resolve` (which bounds an operator's return state by an allow-list). Both are
   `CREATE OR REPLACE`d with the new names and otherwise identical bodies — a diff against 007/011
   should read as "same rule, new names".

**Nothing relaxed a guard.** The constraints, triggers and `RE00x` codes from 002–013 are unchanged.

## Consequences

- The policy table can now state §4's rules. Verified against a live database:

  | state | rule |
  | --- | --- |
  | `check_provider_network` | permitted **only** in `AWAITING_DOCTOR_CHOICE` — §4's ordering rule, previously inexpressible |
  | `create_booking` | `false` in `AWAITING_FINAL_APPROVAL`, `true` only in `BOOKING_IN_FLIGHT` |
  | `list_slots` | `false` in `BOOKING_IN_FLIGHT` — the specific hole the coarse vocabulary created |

- `HUMAN_HANDOFF` has no edge to any booking state, so §11's rule ("resolving a ticket is not
  consent") holds structurally, on top of the separate guarantee that a human actor cannot insert
  an approval at all.
- 124 migration tests pass (104 integration against a real database, 20 unit). ~40 lines across six
  test files were vocabulary updates; three assertions needed judgement rather than substitution and
  are commented where they changed.
- **One test got stricter rather than being made to pass.** `test_moving_the_workflow_past_the_approved_epoch_invalidates_the_approval`
  previously relied on a `awaiting_approval → triaged → awaiting_approval` back edge that no longer
  exists. It now moves out to `AWAITING_SLOT_CHOICE` and back, so the workflow returns to the *same
  state at a new version* — a harder case, because nothing about the workflow's appearance has
  changed and the stale consent still must not apply.

## Note on the fan-out that produced this

This cost is worth recording honestly. To keep parallel agents within their context budget, the
workstreams were given transcribed briefs instead of the source document. That worked — it is why
five of six workstreams completed at all — but it meant each agent invented domain vocabulary where
it needed a specification. The contract layer and the fixtures, which *were* given the contract
schemas, did not have this problem.

**Corrective practice for later phases:** when an agent's output depends on the *content* of a
specification rather than merely its shape, transcribe the vocabulary explicitly in the brief —
states, roles, tool names, error classes — rather than the shapes alone.
