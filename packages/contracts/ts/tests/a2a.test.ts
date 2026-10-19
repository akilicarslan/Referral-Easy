/**
 * The application-result -> A2A protocol-state adapter (§1.1, §5).
 *
 * The mapping is normative: it is declared in the `$comment` on `A2ATaskState` in
 * `packages/contracts/schemas/common.json`, and these tests fail if the adapter drifts from it.
 * The TypeScript and Python adapters must agree — same table, same trap.
 */

import { describe, expect, it } from 'vitest';

import {
  A2A_TASK_STATE_BY_EVENT,
  A2A_TASK_STATE_BY_OUTCOME,
  TERMINAL_A2A_STATES,
  projectEvent,
  projectOutcome,
  type ResultOutcome,
} from '../src/index.js';

describe('the mapping table', () => {
  it('covers every application outcome', () => {
    expect(Object.keys(A2A_TASK_STATE_BY_OUTCOME).sort()).toEqual(
      [
        'completed',
        'needs_input',
        'no_match',
        'outcome_unknown',
        'retryable_error',
        'terminal_error',
      ].sort(),
    );
  });

  it('covers every protocol event', () => {
    expect(Object.keys(A2A_TASK_STATE_BY_EVENT).sort()).toEqual([
      'auth_failed',
      'patient_cancelled',
      'policy_refused',
    ]);
  });

  it.each([
    ['completed', 'completed'],
    ['needs_input', 'input-required'],
    ['no_match', 'completed'],
    ['retryable_error', 'failed'],
    ['terminal_error', 'failed'],
    ['outcome_unknown', 'failed'],
  ] as const)('projects %s to %s', (outcome, expected) => {
    expect(projectOutcome(outcome).a2aState).toBe(expected);
  });

  it.each([
    ['patient_cancelled', 'canceled'],
    ['policy_refused', 'rejected'],
    ['auth_failed', 'auth-required'],
  ] as const)('projects event %s to %s', (event, expected) => {
    expect(projectEvent(event).a2aState).toBe(expected);
  });

  it('treats no_match as a success, not an error (§8)', () => {
    const result = projectOutcome('no_match');
    expect(result.isSuccess).toBe(true);
    expect(result.a2aState).toBe('completed');
  });
});

describe('the trap: outcome_unknown is protocol-terminal with unresolved semantics', () => {
  it('is terminal yet still requires reconciliation', () => {
    const result = projectOutcome('outcome_unknown');
    expect(result.a2aState).toBe('failed');
    expect(result.protocolIsTerminal).toBe(true);
    expect(result.requiresReconciliation).toBe(true);
    expect(result.mayHaveCommittedSideEffects).toBe(true);
  });

  it('is the only outcome that claims unresolved side effects', () => {
    const outcomes: ResultOutcome[] = [
      'completed',
      'needs_input',
      'no_match',
      'retryable_error',
      'terminal_error',
      'outcome_unknown',
    ];
    for (const outcome of outcomes) {
      expect(projectOutcome(outcome).requiresReconciliation).toBe(outcome === 'outcome_unknown');
    }
  });

  it('does not make every terminal state an unresolved one', () => {
    expect(projectOutcome('completed').protocolIsTerminal).toBe(true);
    expect(projectOutcome('completed').requiresReconciliation).toBe(false);
  });
});

describe('terminal states', () => {
  it('is exactly the four protocol-terminal states', () => {
    expect([...TERMINAL_A2A_STATES].sort()).toEqual(
      ['canceled', 'completed', 'failed', 'rejected'].sort(),
    );
  });

  it('does not treat input-required or working as terminal', () => {
    expect(TERMINAL_A2A_STATES).not.toContain('input-required');
    expect(TERMINAL_A2A_STATES).not.toContain('working');
  });
});

describe('results are immutable', () => {
  it('freezes the returned object', () => {
    const result = projectOutcome('completed');
    expect(Object.isFrozen(result)).toBe(true);
  });
});
