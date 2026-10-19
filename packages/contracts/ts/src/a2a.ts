/**
 * Application result -> A2A protocol task state (§1.1, §5).
 *
 * TypeScript counterpart of `referraleasy_contracts.a2a` in the Python package: same table, same
 * rule, and the table is declared normatively in the `$comment` on `A2ATaskState` in
 * `schemas/common.json`. If you change it, change it there first.
 *
 * The sharp edge
 * --------------
 * `outcome_unknown` projects to the protocol state `failed` — which is terminal — while the booking
 * is still unresolved and reconciliation is in flight (§7). A caller reading only the protocol state
 * concludes "no booking exists" and may attempt a second booking with a new key. That is precisely
 * the double-booking failure the project forbids.
 *
 * So this module never hands out a bare `A2ATaskState`. The only way to obtain one is
 * {@link AgentTaskResult}, which always carries the application outcome beside it and exposes
 * {@link AgentTaskResult.requiresReconciliation}. The mapping objects are declared `satisfies
 * Record<ResultOutcome, …>`, so adding an outcome to the schema becomes a compile error here rather
 * than a silent runtime hole.
 */

import type { common$A2ATaskState, common$ResultOutcome } from './generated/common.js';

export type ResultOutcome = common$ResultOutcome;
export type A2ATaskState = common$A2ATaskState;

/**
 * Triggers that produce a protocol state without producing an application result.
 *
 * `ResultOutcome` describes what an agent call returned. These three protocol states instead
 * describe why no agent call result exists at all: the patient cancelled, a server-side policy
 * refused the request before an agent ran, or service authentication failed (§5, §9).
 */
export type ProtocolEvent = 'patient_cancelled' | 'policy_refused' | 'auth_failed';

/** The normative outcome -> protocol-state table. `satisfies` proves totality at compile time. */
export const A2A_TASK_STATE_BY_OUTCOME = {
  completed: 'completed',
  needs_input: 'input-required',
  // A valid empty result is a *successful* call whose answer is "nothing matched" (§8) —
  // not an error, and explicitly not retried as one.
  no_match: 'completed',
  retryable_error: 'failed',
  terminal_error: 'failed',
  // Terminal at the protocol level, unresolved at the application level. See the module docstring.
  outcome_unknown: 'failed',
} as const satisfies Record<ResultOutcome, A2ATaskState>;

/** Protocol states reachable from a non-result event. */
export const A2A_TASK_STATE_BY_EVENT = {
  patient_cancelled: 'canceled',
  policy_refused: 'rejected',
  auth_failed: 'auth-required',
} as const satisfies Record<ProtocolEvent, A2ATaskState>;

/** Protocol states from which no further transition is expected. */
export const TERMINAL_A2A_STATES: readonly A2ATaskState[] = [
  'completed',
  'canceled',
  'failed',
  'rejected',
];

/** An application outcome together with its protocol projection. Built only by the factories. */
export interface AgentTaskResult {
  readonly outcome: ResultOutcome;
  /** Terminal at the protocol level — does NOT mean no side effect occurred. */
  readonly protocolIsTerminal: boolean;
  /**
   * Whether the workflow must still resolve an unknown external outcome (§7).
   * True *only* for `outcome_unknown`. The caller must `lookup_request` with the **same**
   * idempotency key — never mint a second booking key.
   */
  readonly requiresReconciliation: boolean;
  /** Whether an external system may have committed a booking we cannot see. */
  readonly mayHaveCommittedSideEffects: boolean;
  /** A call that ran correctly — including a valid empty `no_match` result. */
  readonly isSuccess: boolean;
  /** Protocol projection of the outcome or event. */
  readonly a2aState: A2ATaskState;
}

function build(outcome: ResultOutcome, a2aState: A2ATaskState): AgentTaskResult {
  const requiresReconciliation = outcome === 'outcome_unknown';
  return Object.freeze({
    outcome,
    a2aState,
    protocolIsTerminal: TERMINAL_A2A_STATES.includes(a2aState),
    requiresReconciliation,
    mayHaveCommittedSideEffects: requiresReconciliation,
    isSuccess: outcome === 'completed' || outcome === 'no_match',
  });
}

/** Project an agent call result onto the A2A protocol. */
export function projectOutcome(outcome: ResultOutcome): AgentTaskResult {
  return build(outcome, A2A_TASK_STATE_BY_OUTCOME[outcome]);
}

/** Project a cancellation / policy refusal / auth failure onto the A2A protocol. */
export function projectEvent(event: ProtocolEvent): AgentTaskResult {
  return build(event === 'patient_cancelled' ? 'no_match' : 'terminal_error', A2A_TASK_STATE_BY_EVENT[event]);
}
