import { sse$SurfaceUpdate, sse$WorkflowStateChange } from '@referraleasy/contracts';
import type { sse$StreamEvent } from '@referraleasy/contracts';
import type { AdapterAction, AdapterDiagnostic, SurfaceRuntime } from '../a2ui/adapter.js';
import { CommandRegistry, toClientAction, type PendingCommand } from './command.js';
import type { WorkflowSnapshot } from './patient-client.js';
import { PatientRequestError } from './patient-client.js';

/**
 * The application's view of the stream.
 *
 * Everything the UI needs in order to be honest about what it knows lives here,
 * in one object, because "what do we actually know" is the question this app
 * keeps asking and it must have exactly one answer.
 *
 * Two guarantees it provides:
 *
 *   - **Applying the same event twice changes nothing.** Events are deduplicated
 *     by `event_id`, and the surface update underneath is a reconcile keyed by
 *     `instance_id`. A replay is therefore a no-op, which is what makes replay
 *     safe to trigger on a `seq` gap.
 *
 *   - **The stream ending is not an outcome.** `terminal` carries the outcome
 *     and closes the workflow. If the connection drops without one, the store
 *     reports `reason: 'interrupted'` — it never infers completion, and it never
 *     infers failure either.
 */

export type ViewPhase = 'connecting' | 'live' | 'degraded' | 'complete' | 'interrupted' | 'error';

/** The outcome a terminal event carried, if one arrived. */
export interface TerminalOutcome {
  readonly state: string;
  readonly reasonCode: string;
  readonly at: string;
}

export interface DependencyState {
  readonly state: string;
  readonly stateVersion: number;
  readonly degraded: string | null;
  readonly reasonCode: string;
  readonly updatedAt: string;
}

export interface SurfaceState {
  readonly surfaceId: string;
  readonly instanceIds: readonly string[];
  readonly updatedAt: string;
}

export interface StoreSnapshot {
  readonly phase: ViewPhase;
  readonly workflowId: string | undefined;
  /** The newest applied `seq`, or -1. */
  readonly seq: number;
  /** `event_id` to replay from. */
  readonly lastEventId: string | undefined;
  readonly workflow: DependencyState | undefined;
  readonly surfaces: readonly SurfaceState[];
  readonly terminal: TerminalOutcome | undefined;
  /** Progressive output. Explicitly NOT final state (§2.1). */
  readonly progress: { readonly text: string; readonly at: string } | undefined;
  readonly notices: readonly { readonly text: string; readonly at: string }[];
  readonly diagnostics: readonly AdapterDiagnostic[];
  readonly error: string | undefined;
  /** True while any patient action is awaiting a server answer. */
  readonly awaitingServer: boolean;
}

export interface SurfaceStoreOptions {
  readonly runtime: SurfaceRuntime;
  readonly onChange: (snapshot: StoreSnapshot) => void;
  /**
   * Sends a client action to the gateway. Returns nothing: the answer arrives as
   * a new stream event. A rejection here is reported as a diagnostic, never as
   * an outcome — HTTP 200 is not confirmation either.
   */
  readonly sendAction?: (action: ClientActionShape) => Promise<void>;
}

/**
 * The client action as it goes on the wire.
 *
 * Declared structurally rather than imported from the contracts package, so the
 * store's public surface does not change when the contracts package reorganises.
 * `toClientAction` is what guarantees the two stay in step, and the contract's
 * own schema is what validates it at the boundary.
 */
export interface ClientActionShape {
  readonly instance_id: string;
  readonly action: string;
  readonly workflow_state_version: number;
  readonly command_id: string;
  readonly value?: Record<string, unknown> | null | undefined;
}

/**
 * Reduce a `DegradationLevel` to "nothing was reduced" or "say what was".
 *
 * `full` is the contract's way of saying the pipeline was intact, so it must not
 * put the view into a degraded phase — otherwise every healthy run wears a
 * warning banner. An unrecognised level is kept verbatim rather than dropped:
 * a reduction we do not have a sentence for is still a reduction, and hiding it
 * would be the same class of lie as showing an unconfirmed booking as confirmed.
 */
function normaliseDegradation(level: string | null | undefined): string | null {
  if (typeof level !== 'string' || level.length === 0 || level === 'full') {
    return null;
  }
  return level;
}

export class SurfaceStore {
  readonly #runtime: SurfaceRuntime;
  readonly #options: SurfaceStoreOptions;
  readonly #commands = new CommandRegistry();

  #phase: ViewPhase = 'connecting';
  #workflowId: string | undefined;
  #seq = -1;
  #lastEventId: string | undefined;
  #workflow: DependencyState | undefined;
  #surfaces = new Map<string, SurfaceState>();
  #terminal: TerminalOutcome | undefined;
  #progress: { text: string; at: string } | undefined;
  #notices: { text: string; at: string }[] = [];
  #diagnostics: AdapterDiagnostic[] = [];
  #error: string | undefined;
  /** Event ids already applied. Bounded; a workflow is short-lived. */
  #applied = new Set<string>();
  #inFlight = new Set<string>();

  constructor(options: SurfaceStoreOptions) {
    this.#runtime = options.runtime;
    this.#options = options;

    // Actions raised inside the surface come back through the runtime's single
    // callback, which is where the server-issued action is resolved.
    this.#runtime.subscribe(() => {
      this.#emit();
    });
  }

  get snapshot(): StoreSnapshot {
    return {
      phase: this.#phase,
      workflowId: this.#workflowId,
      seq: this.#seq,
      lastEventId: this.#lastEventId,
      workflow: this.#workflow,
      surfaces: [...this.#surfaces.values()],
      terminal: this.#terminal,
      progress: this.#progress,
      notices: this.#notices,
      diagnostics: this.#diagnostics,
      error: this.#error,
      awaitingServer: this.#inFlight.size > 0,
    };
  }

  /** Restore current cards before opening a stream from the snapshot's cursor. */
  hydrate(snapshot: WorkflowSnapshot): void {
    if (snapshot.workflow_id !== this.#workflowId) throw new Error('Snapshot belongs to another workflow.');
    const previousIds = this.#runtime.instances(snapshot.surface_id).map((item) => item.instance_id);
    const currentIds = new Set(snapshot.components.map((item) => item.instance_id));
    for (const surfaceId of this.#runtime.surfaceIds()) {
      if (surfaceId !== snapshot.surface_id) this.#runtime.removeSurface(surfaceId);
    }
    for (const component of snapshot.components) {
      if (component.action === 'scheduling:lookup_request' && !component.submitted && !component.disabled
        && (snapshot.last_seq ?? -1) > this.#seq) {
        // A server re-offer is a new read intent against the same original booking.
        this.#commands.release(component.instance_id, component.action);
        this.#inFlight.delete(component.instance_id);
      }
      if (component.submitted || component.disabled) this.#inFlight.delete(component.instance_id);
    }
    this.#surfaces.clear();
    this.#runtime.applyUpdate({ surface_id: snapshot.surface_id, components: snapshot.components,
      remove_instance_ids: previousIds.filter((id) => !currentIds.has(id)) });
    this.#surfaces.set(snapshot.surface_id, {
      surfaceId: snapshot.surface_id, instanceIds: snapshot.components.map((item) => item.instance_id), updatedAt: '',
    });
    this.#seq = snapshot.last_seq ?? -1;
    this.#lastEventId = snapshot.last_event_id ?? undefined;
    this.#workflow = { state: snapshot.state, stateVersion: snapshot.state_version, degraded: null, reasonCode: 'ok', updatedAt: '' };
    this.#phase = 'live';
    this.#terminal = undefined;
    if (snapshot.terminal !== null) this.#applyTerminal(snapshot.terminal);
    this.#emit();
  }

  /** Begin a workflow. Clears everything, including the replay cursor. */
  beginWorkflow(workflowId: string): void {
    this.#workflowId = workflowId;
    this.#seq = -1;
    this.#lastEventId = undefined;
    this.#workflow = undefined;
    this.#terminal = undefined;
    this.#progress = undefined;
    this.#notices = [];
    this.#diagnostics = [];
    this.#error = undefined;
    this.#applied.clear();
    this.#inFlight.clear();
    this.#commands.clear();
    for (const surfaceId of this.#runtime.surfaceIds()) {
      this.#runtime.removeSurface(surfaceId);
    }
    this.#surfaces.clear();
    this.#phase = 'connecting';
    this.#emit();
  }

  /** Record that the transport is (re)connecting. */
  setConnecting(): void {
    if (this.#phase !== 'complete') {
      this.#phase = this.#workflow?.degraded ? 'degraded' : 'connecting';
      this.#emit();
    }
  }

  /** Record that the transport is open. */
  setLive(): void {
    if (this.#phase === 'complete') {
      return;
    }
    this.#phase = this.#workflow?.degraded ? 'degraded' : 'live';
    this.#emit();
  }

  /** Record that the transport closed without a `terminal` event. */
  setInterrupted(): void {
    if (this.#phase === 'complete') {
      return;
    }
    this.#phase = 'interrupted';
    this.#emit();
  }

  /** Record a client-side failure that is not an outcome of the workflow. */
  setError(message: string): void {
    this.#error = message;
    this.#phase = 'error';
    this.#emit();
  }

  /** Adapter diagnostics (rejected updates, dropped actions) are surfaced, not swallowed. */
  addDiagnostic(diagnostic: AdapterDiagnostic): void {
    this.#diagnostics = [...this.#diagnostics, diagnostic].slice(-20);
    this.#emit();
  }

  /**
   * Apply one stream event.
   *
   * Returns what happened, so tests can assert that a replay is a no-op rather
   * than merely trusting that it is.
   */
  apply(
    event: sse$StreamEvent,
  ): { readonly applied: boolean; readonly reason?: 'duplicate' | 'stale' | 'after_terminal' } {
    if (this.#applied.has(event.event_id)) {
      return { applied: false, reason: 'duplicate' };
    }
    if (event.seq <= this.#seq) {
      // Already covered by a newer event. Replay is allowed to resend.
      return { applied: false, reason: 'stale' };
    }

    // A terminal event closes the workflow. Anything arriving after it is a
    // server bug; applying it would resurrect a finished flow.
    if (this.#terminal !== undefined) {
      return { applied: false, reason: 'after_terminal' };
    }

    this.#applied.add(event.event_id);
    this.#seq = event.seq;
    this.#lastEventId = event.event_id;

    switch (event.type) {
      case 'surface_update':
        this.#applySurfaceUpdate(event);
        break;

      case 'workflow_state':
        this.#applyWorkflowState(event);
        break;

      case 'agent_progress':
        this.#applyProgress(event);
        break;

      case 'partial_text':
        // Explicitly not final state. Recorded as progress, and labelled as such
        // wherever it is shown, so it can never be mistaken for an answer.
        this.#applyPartialText(event);
        break;

      case 'notice':
        this.#applyNotice(event);
        break;

      case 'terminal':
        this.#applyTerminal(event);
        break;
    }

    this.#emit();
    return { applied: true };
  }

  /**
   * Patient action from a component.
   *
   * Called by the app once the adapter has told it which instance was touched.
   * This is where the server-issued `action` is looked up and the `command_id`
   * is taken from the registry, so a second click reuses the first key.
   */
  submit(action: AdapterAction): void {
    if (this.#inFlight.has(action.instanceId)) return;
    const surfaceId = this.#surfacesOfInstance(action.instanceId);
    const instance = surfaceId === undefined ? undefined : this.#instance(surfaceId, action.instanceId);

    if (instance === undefined) {
      this.addDiagnostic({
        code: 'ACTION_FOR_UNKNOWN_INSTANCE',
        message: `An action arrived for instance ${action.instanceId}, which is not on any live surface. It was dropped.`,
      });
      return;
    }
    if (instance.action === null || instance.action === undefined) {
      this.addDiagnostic({
        code: 'ACTION_ON_DISPLAY_ONLY_INSTANCE',
        message: `Instance ${action.instanceId} is display-only. The action was dropped.`,
      });
      return;
    }
    if (instance.disabled === true) {
      this.addDiagnostic({
        code: 'ACTION_ON_DISABLED_INSTANCE',
        message: `Instance ${action.instanceId} is disabled by the server. The action was dropped.`,
      });
      return;
    }
    if (instance.submitted === true) {
      this.addDiagnostic({
        code: 'ACTION_ON_SUBMITTED_INSTANCE',
        message: `Instance ${action.instanceId} has already been answered. The action was dropped rather than sent twice.`,
      });
      return;
    }

    const command = this.#commands.begin(instance);
    const value = this.#mergeValue(instance, action);

    this.#inFlight.add(instance.instance_id);
    this.#emit();

    const payload = toClientAction(instance, command, value);
    if (payload === undefined) {
      this.#inFlight.delete(instance.instance_id);
      this.#emit();
      return;
    }

    void this.#dispatch(payload, command);
  }

  /** True when this instance already has an action in flight. */
  isInFlight(instanceId: string): boolean {
    return this.#inFlight.has(instanceId);
  }

  /** Live instances for a surface, for the app shell to render around. */
  instances(surfaceId: string): ReturnType<SurfaceRuntime['instances']> {
    return this.#runtime.instances(surfaceId);
  }

  // ── event handlers ───────────────────────────────────────────────────────

  #applySurfaceUpdate(event: sse$StreamEvent): void {
    const parsed = sse$SurfaceUpdate.safeParse(event.payload);
    if (!parsed.success) {
      this.addDiagnostic({
        code: 'SURFACE_UPDATE_INVALID',
        message: `A surface_update did not match the contract and was skipped: ${parsed.error.issues
          .map((issue) => issue.path.join('.') || '(root)')
          .join(', ')}`,
      });
      return;
    }

    const update = parsed.data;
    for (const component of update.components) {
      if (component.submitted || component.disabled) this.#inFlight.delete(component.instance_id);
    }
    for (const id of update.remove_instance_ids ?? []) this.#inFlight.delete(id);
    this.#runtime.applyUpdate(update);

    this.#surfaces.set(update.surface_id, {
      surfaceId: update.surface_id,
      instanceIds: this.#runtime.instances(update.surface_id).map((instance) => instance.instance_id),
      updatedAt: event.created_at,
    });
  }

  #applyWorkflowState(event: sse$StreamEvent): void {
    const parsed = sse$WorkflowStateChange.safeParse(event.payload);

    if (parsed.success) {
      // A newer state version means everything built from older versions is
      // finished, so their idempotency keys must not be reused for a new intent.
      const released = this.#commands.releaseUpTo(parsed.data.state_version);
      if (released.length > 0) {
        this.#inFlight.clear();
      }
    } else {
      this.addDiagnostic({
        code: 'WORKFLOW_STATE_INVALID',
        message: 'A workflow_state event did not match the contract. Degradation for it is unknown.',
      });
    }

    // An unparseable state change is itself a reason to be unsure, so it is
    // recorded as degraded rather than as `full`.
    const degraded = parsed.success ? normaliseDegradation(parsed.data.degraded) : 'no_source';

    this.#workflow = {
      state: parsed.success ? parsed.data.to_state : 'UNKNOWN',
      stateVersion: parsed.success ? parsed.data.state_version : -1,
      degraded,
      reasonCode: parsed.success ? parsed.data.reason_code : 'upstream_malformed_response',
      updatedAt: event.created_at,
    };

    if (this.#phase !== 'complete') {
      this.#phase = degraded !== null ? 'degraded' : 'live';
    }
  }

  #applyProgress(event: sse$StreamEvent): void {
    const payload = event.payload ?? {};
    const text = typeof payload['message'] === 'string' ? payload['message'] : undefined;
    if (text !== undefined) {
      this.#progress = { text, at: event.created_at };
    }
  }

  #applyPartialText(event: sse$StreamEvent): void {
    // Kept separate from progress so the UI can label it "not final". The
    // contract is explicit that partial agent output is never committed.
    const payload = event.payload ?? {};
    const text =
      typeof payload['text'] === 'string'
        ? payload['text']
        : typeof payload['message'] === 'string'
          ? payload['message']
          : undefined;
    if (text !== undefined) {
      this.#progress = { text, at: event.created_at };
    }
  }

  #applyNotice(event: sse$StreamEvent): void {
    const payload = event.payload ?? {};
    const text =
      typeof payload['message'] === 'string'
        ? payload['message']
        : typeof payload['text'] === 'string'
          ? payload['text']
          : undefined;
    if (text !== undefined) {
      this.#notices = [...this.#notices, { text, at: event.created_at }].slice(-10);
    }
  }

  #applyTerminal(event: sse$StreamEvent): void {
    const payload = event.payload ?? {};
    this.#terminal = {
      state: typeof payload['to_state'] === 'string' ? payload['to_state'] : this.#workflow?.state ?? 'UNKNOWN',
      reasonCode: typeof payload['reason_code'] === 'string' ? payload['reason_code'] : 'other',
      at: event.created_at,
    };
    this.#phase = 'complete';
    this.#progress = undefined;
    this.#inFlight.clear();
  }

  // ── action plumbing ──────────────────────────────────────────────────────

  #surfacesOfInstance(instanceId: string): string | undefined {
    for (const surfaceId of this.#runtime.surfaceIds()) {
      if (this.#runtime.instances(surfaceId).some((i) => i.instance_id === instanceId)) {
        return surfaceId;
      }
    }
    return undefined;
  }

  #instance(surfaceId: string, instanceId: string) {
    return this.#runtime.instances(surfaceId).find((i) => i.instance_id === instanceId);
  }

  /**
   * Combine the component's own props with what the patient just did.
   *
   * Props win the merge only when the action did not supply the key, so a
   * component can carry pre-existing context without being able to overwrite the
   * patient's fresh answer.
   */
  #mergeValue(
    instance: { props: Record<string, unknown> },
    action: AdapterAction,
  ): Record<string, unknown> | null {
    const fromProps =
      instance.props['value'] !== null &&
      typeof instance.props['value'] === 'object' &&
      !Array.isArray(instance.props['value'])
        ? (instance.props['value'] as Record<string, unknown>)
        : {};
    const merged = { ...fromProps, ...action.context };
    return Object.keys(merged).length > 0 ? merged : null;
  }

  async #dispatch(payload: ClientActionShape, command: PendingCommand): Promise<void> {
    const send = this.#options.sendAction;
    if (send === undefined) {
      this.addDiagnostic({
        code: 'NO_TRANSPORT',
        message: `Action ${payload.action} could not be sent: no transport is configured.`,
      });
      this.#inFlight.delete(payload.instance_id);
      this.#emit();
      return;
    }

    try {
      await send(payload);
      // Success here means the request was accepted, not that the booking is
      // confirmed. The in-flight latch is released by the surface moving on, or
      // by an explicit failure below — never by a 200 on its own.
    } catch (error) {
      this.#inFlight.delete(payload.instance_id);
      // A definite refusal did no work, so a corrected answer is a new intent. An unknown
      // delivery outcome retains its command id, allowing the exact request to replay safely.
      if (error instanceof PatientRequestError && error.status >= 400 && error.status < 500) {
        this.#commands.release(payload.instance_id, payload.action);
      }
      const surfaceId = this.#surfacesOfInstance(payload.instance_id);
      const instance = surfaceId === undefined ? undefined : this.#instance(surfaceId, payload.instance_id);
      if (surfaceId !== undefined && instance !== undefined) {
        this.#runtime.applyUpdate({ surface_id: surfaceId, components: [instance] });
      }
      this.addDiagnostic({
        code: 'ACTION_SEND_FAILED',
        message: `Could not deliver ${payload.action} (attempt ${command.attempt}). Retrying will reuse the same idempotency key, so it cannot book twice. Reason: ${
          error instanceof Error ? error.message : String(error)
        }`,
      });
      this.#emit();
    }
  }

  #emit(): void {
    this.#options.onChange(this.snapshot);
  }
}
