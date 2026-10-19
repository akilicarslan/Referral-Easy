import type { sse$ClientAction, sse$ComponentInstance } from '@referraleasy/contracts';

/**
 * Idempotency for patient actions.
 *
 * The rule from the contract is narrow and absolute: `command_id` is the
 * idempotency key, a resubmitted action **reuses** it, and that is what makes a
 * double-click or a retried request unable to become a second booking.
 *
 * Which means the naive implementation — mint a UUID inside the click handler —
 * is exactly wrong. It produces a *new* key per click, so the retry the rule was
 * written to protect against is precisely the case it fails.
 *
 * So a key is minted once per *intent*, not per *attempt*:
 *
 *   intent = (instance_id, action)
 *
 * Retrying the same intent reuses the key. A different instance, or a different
 * server-issued action on the same instance, is a different intent and gets a
 * fresh one. The key is released only when the workflow has moved on — which the
 * caller signals by applying a newer `workflow_state_version`. A server snapshot
 * may also re-offer a read-only booking lookup at the same state version; that
 * is a new status check against the original request.
 */

/** A stable key for "the patient is trying to do this one thing". */
function intentKey(instanceId: string, action: string): string {
  return `${instanceId}\u0000${action}`;
}

export interface PendingCommand {
  readonly commandId: string;
  readonly instanceId: string;
  readonly action: string;
  readonly workflowStateVersion: number;
  /** Monotonic per intent; the first attempt is 1. */
  readonly attempt: number;
}

export class CommandRegistry {
  readonly #pending = new Map<string, PendingCommand>();
  readonly #mint: () => string;

  /**
   * @param mint UUID source. Injectable so tests can assert key reuse without
   *   depending on random values.
   */
  constructor(mint: () => string = defaultMint) {
    this.#mint = mint;
  }

  /**
   * Start — or resume — the patient's intent against one component instance.
   *
   * Returns the existing command if this intent is already in flight, which is
   * what turns a second click into a replay rather than a new booking.
   */
  begin(instance: sse$ComponentInstance): PendingCommand {
    const action = actionOf(instance) ?? 'display_only';
    const key = intentKey(instance.instance_id, action);
    const existing = this.#pending.get(key);

    if (existing !== undefined) {
      const retried: PendingCommand = { ...existing, attempt: existing.attempt + 1 };
      this.#pending.set(key, retried);
      return retried;
    }

    const created: PendingCommand = {
      commandId: this.#mint(),
      instanceId: instance.instance_id,
      action,
      workflowStateVersion: instance.workflow_state_version,
      attempt: 1,
    };
    this.#pending.set(key, created);
    return created;
  }

  /** True when this instance already has an action in flight. */
  isInFlight(instanceId: string): boolean {
    for (const pending of this.#pending.values()) {
      if (pending.instanceId === instanceId) {
        return true;
      }
    }
    return false;
  }

  /**
   * Release every intent built against a superseded version.
   *
   * Called when the backend announces a new `workflow_state_version`: anything
   * the server has already moved past is finished, so its key must not be reused
   * for a future, genuinely different intent.
   */
  releaseUpTo(workflowStateVersion: number): readonly string[] {
    const released: string[] = [];
    for (const [key, pending] of this.#pending) {
      if (pending.workflowStateVersion < workflowStateVersion) {
        this.#pending.delete(key);
        released.push(key);
      }
    }
    return released;
  }

  /** Release one intent, e.g. after a refusal that cannot be retried. */
  release(instanceId: string, action: string): void {
    this.#pending.delete(intentKey(instanceId, action));
  }

  /** Drop everything. Used when the surface is torn down. */
  clear(): void {
    this.#pending.clear();
  }

  /** Snapshot for tests and diagnostics. */
  entries(): readonly PendingCommand[] {
    return [...this.#pending.values()];
  }
}

/**
 * Build the wire action.
 *
 * The `action` string comes from the instance — it is **server-issued** and the
 * client only echoes it. `instance.action === null` means the component is
 * display-only, and nothing can be submitted from it.
 */
export function toClientAction(
  instance: sse$ComponentInstance,
  command: PendingCommand,
  value: Record<string, unknown> | null,
): sse$ClientAction | undefined {
  const action = actionOf(instance);
  if (action === undefined) {
    return undefined;
  }
  return {
    instance_id: instance.instance_id,
    action,
    workflow_state_version: instance.workflow_state_version,
    command_id: command.commandId,
    value,
  };
}

/**
 * The server-issued action string, or `undefined` for a display-only instance.
 *
 * The generated contract type widens `action` to `unknown`: the schema validates
 * it with a refinement rather than a plain string, so the inferred type cannot
 * carry the string through. Narrowing here means the rest of the module never
 * has to guess, and a malformed action cannot reach the wire.
 */
function actionOf(instance: sse$ComponentInstance): string | undefined {
  return typeof instance.action === 'string' && instance.action.length > 0
    ? instance.action
    : undefined;
}

function defaultMint(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  // Only reached in an environment without WebCrypto. Still unique enough to
  // serve as an idempotency key, and flagged so it is not mistaken for a UUID.
  return `local-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
