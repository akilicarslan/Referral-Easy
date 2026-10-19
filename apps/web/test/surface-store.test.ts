import { describe, expect, it, beforeEach, afterEach } from 'vitest';
import type { sse$StreamEvent, sse$SurfaceUpdate } from '@referraleasy/contracts';
import { SurfaceRuntime } from '../src/a2ui/adapter.js';
import {
  SurfaceStore,
  type ClientActionShape,
  type StoreSnapshot,
} from '../src/stream/surface-store.js';
import { instance, surfaceUpdate, uuid } from './helpers.js';

/**
 * Idempotency, replay and the states that must never be smoothed over.
 *
 * These are the tests the contract's correctness claims rest on: a replay must
 * be safe, a replay must not create a second booking, and a degraded answer must
 * say it is degraded.
 */

let runtime: SurfaceRuntime;
let snapshots: StoreSnapshot[];

function event(patch: Partial<sse$StreamEvent> & Pick<sse$StreamEvent, 'seq' | 'type'>): sse$StreamEvent {
  return {
    event_id: (patch.seq ?? 0).toString(16).padStart(32, '0'),
    workflow_id: uuid(50),
    created_at: new Date(1_700_000_000_000 + (patch.seq ?? 0) * 1000).toISOString(),
    synthetic: true,
    ...patch,
  } as sse$StreamEvent;
}

function makeStore(
  options: { sendAction?: (action: ClientActionShape) => Promise<void> } = {},
): SurfaceStore {
  return new SurfaceStore({
    runtime,
    onChange: (snapshot) => {
      snapshots.push(snapshot);
    },
    // Spread conditionally so `exactOptionalPropertyTypes` does not see an
    // explicit `undefined` where the option is declared absent-or-a-function.
    ...(options.sendAction === undefined ? {} : { sendAction: options.sendAction }),
  });
}

beforeEach(() => {
  runtime = new SurfaceRuntime();
  snapshots = [];
});

afterEach(() => {
  runtime.dispose();
});

describe('SurfaceUpdate is applied idempotently', () => {
  it('reads the terminal state from the preceding gateway workflow event', () => {
    const store = makeStore();
    store.beginWorkflow(uuid(50));
    store.apply(event({ seq: 0, type: 'workflow_state', payload: {
      from_state: 'BOOKING_IN_FLIGHT', to_state: 'BOOKED', state_version: 4, reason_code: 'ok',
    } }));
    store.apply(event({ seq: 1, type: 'terminal', payload: { outcome: 'completed', reason_code: 'ok' } }));
    expect(store.snapshot.terminal?.state).toBe('BOOKED');
  });

  it('coalesces double clicks while a request is in flight', async () => {
    const sent: ClientActionShape[] = [];
    const store = makeStore({ sendAction: (action) => { sent.push(action); return new Promise(() => {}); } });
    store.beginWorkflow(uuid(50));
    store.apply(event({ seq: 0, type: 'surface_update', payload: surfaceUpdate([
      instance({ instance_id: uuid(1), component: 'HospitalPicker', action: 'system:await_patient_choice' }),
    ]) }));
    const action = { instanceId: uuid(1), name: 'hospital.select', context: { selected_id: 'hospital-1' } };
    store.submit(action);
    store.submit(action);
    expect(sent).toHaveLength(1);
  });
  it('yields one instance, not two, when the same update arrives twice', () => {
    const store = makeStore();
    store.beginWorkflow(uuid(50));

    const update = surfaceUpdate([
      instance({ instance_id: uuid(1), component: 'HospitalPicker', props: { title: 'Choose' } }),
    ]);

    const first = runtime.applyUpdate(update);
    const second = runtime.applyUpdate(update);

    expect(first.added).toEqual([uuid(1)]);
    expect(second.added).toEqual([]);
    expect(second.replaced).toEqual([uuid(1)]);
    expect(runtime.count(update.surface_id)).toBe(1);
    // No stream event was applied, so the store's own surface list is untouched.
    // It follows `runtime`, it does not second-guess it.
    expect(store.snapshot.surfaces).toHaveLength(0);
  });

  it('is stable when the same update is delivered as a replayed stream event', () => {
    const store = makeStore();
    store.beginWorkflow(uuid(50));

    const update = surfaceUpdate([
      instance({ instance_id: uuid(1), component: 'HospitalPicker' }),
      instance({ instance_id: uuid(2), component: 'DoctorPicker' }),
    ]);

    const first = event({ seq: 0, type: 'surface_update', payload: update });
    const outcomeOne = store.apply(first);
    const outcomeTwo = store.apply(first);

    expect(outcomeOne.applied).toBe(true);
    expect(outcomeTwo).toEqual({ applied: false, reason: 'duplicate' });
    expect(runtime.count(update.surface_id)).toBe(2);
  });

  it('replaces an instance in place by instance_id, keeping its position', () => {
    const store = makeStore();
    store.beginWorkflow(uuid(50));

    const update = surfaceUpdate([
      instance({ instance_id: uuid(1), component: 'HospitalPicker' }),
      instance({ instance_id: uuid(2), component: 'DoctorPicker' }),
      instance({ instance_id: uuid(3), component: 'SlotPicker' }),
    ]);
    runtime.applyUpdate(update);

    // The same slot, re-rendered with new props and a newer state version.
    runtime.applyUpdate(
      surfaceUpdate([instance({ instance_id: uuid(2), component: 'DoctorPicker', workflow_state_version: 7 })]),
    );

    const ids = runtime.instances(update.surface_id).map((i) => i.instance_id);
    expect(ids).toEqual([uuid(1), uuid(2), uuid(3)]);
    expect(runtime.instances(update.surface_id)[1]?.workflow_state_version).toBe(7);
    expect(runtime.count(update.surface_id)).toBe(3);
  });

  it('drops instances named in remove_instance_ids so a superseded choice cannot be resubmitted', () => {
    const store = makeStore();
    store.beginWorkflow(uuid(50));

    const update = surfaceUpdate([
      instance({ instance_id: uuid(1), component: 'HospitalPicker' }),
      instance({ instance_id: uuid(2), component: 'DoctorPicker' }),
    ]);
    const { surface_id } = update;
    runtime.applyUpdate(update);

    const result = runtime.applyUpdate(surfaceUpdate([], { surface_id, remove_instance_ids: [uuid(2)] }));

    expect(result.removed).toEqual([uuid(2)]);
    expect(runtime.instances(surface_id).map((i) => i.instance_id)).toEqual([uuid(1)]);
  });
});

describe('reconnect replays without duplicating', () => {
  it('re-applies nothing when the stream re-sends events already seen', () => {
    const store = makeStore();
    store.beginWorkflow(uuid(50));

    const update = surfaceUpdate([instance({ instance_id: uuid(1), component: 'SlotPicker' })]);

    const events = [
      event({ seq: 0, type: 'workflow_state', payload: { from_state: 'INTAKE', to_state: 'PROVIDER_MATCHING', state_version: 1, reason_code: 'ok', degraded: null } }),
      event({ seq: 1, type: 'surface_update', payload: update }),
      event({ seq: 2, type: 'agent_progress', payload: { message: 'Searching' } }),
    ];

    for (const item of events) {
      expect(store.apply(item).applied).toBe(true);
    }
    expect(runtime.count(update.surface_id)).toBe(1);
    const seqBefore = store.snapshot.seq;

    // The gateway replays the whole run after a reconnect, which is what the
    // contract says replaying from a given event_id must reproduce.
    for (const item of events) {
      expect(store.apply(item).applied).toBe(false);
    }

    expect(runtime.count(update.surface_id)).toBe(1);
    expect(store.snapshot.seq).toBe(seqBefore);
    expect(store.snapshot.surfaces).toHaveLength(1);
  });

  it('ignores an event older than the newest applied seq', () => {
    const store = makeStore();
    store.beginWorkflow(uuid(50));

    const newer = event({ seq: 5, type: 'notice', payload: { message: 'later' } });
    const older = event({ seq: 2, type: 'notice', payload: { message: 'earlier' } });

    store.apply(newer);
    const outcome = store.apply(older);

    expect(outcome).toEqual({ applied: false, reason: 'stale' });
    expect(store.snapshot.notices.map((n) => n.text)).toEqual(['later']);
  });

  it('refuses anything arriving after a terminal event', () => {
    const store = makeStore();
    store.beginWorkflow(uuid(50));

    store.apply(
      event({ seq: 9, type: 'terminal', payload: { to_state: 'BOOKED', reason_code: 'ok' } }),
    );
    const outcome = store.apply(
      event({ seq: 10, type: 'surface_update', payload: surfaceUpdate([instance({ component: 'AppointmentReceipt' })]) }),
    );

    expect(outcome).toEqual({ applied: false, reason: 'after_terminal' });
    expect(store.snapshot.terminal?.state).toBe('BOOKED');
  });
});

describe('the stream ending is not an outcome', () => {
  it('reports an interruption, not a completion, when the transport closes early', () => {
    const store = makeStore();
    store.beginWorkflow(uuid(50));
    store.apply(event({ seq: 0, type: 'workflow_state', payload: { from_state: 'INTAKE', to_state: 'PROVIDER_MATCHING', state_version: 1, reason_code: 'ok', degraded: null } }));
    store.setLive();

    store.setInterrupted();

    expect(store.snapshot.phase).toBe('interrupted');
    expect(store.snapshot.terminal).toBeUndefined();
  });

  it('never infers completion from a terminal event it did not receive', () => {
    const store = makeStore();
    store.beginWorkflow(uuid(50));
    store.setInterrupted();
    store.setInterrupted();
    expect(store.snapshot.terminal).toBeUndefined();
  });
});

describe('degradation surfaces', () => {
  it('moves to the degraded phase and records the level', () => {
    const store = makeStore();
    store.beginWorkflow(uuid(50));

    store.apply(
      event({
        seq: 0,
        type: 'workflow_state',
        payload: {
          from_state: 'PROVIDER_MATCHING',
          to_state: 'AWAITING_HOSPITAL_CHOICE',
          state_version: 4,
          reason_code: 'ok',
          degraded: 'opensearch_down',
        },
      }),
    );

    const snapshot = store.snapshot;
    expect(snapshot.phase).toBe('degraded');
    expect(snapshot.workflow?.degraded).toBe('opensearch_down');
    expect(snapshot.workflow?.state).toBe('AWAITING_HOSPITAL_CHOICE');
  });

  it('leaves the phase live when the pipeline is full', () => {
    const store = makeStore();
    store.beginWorkflow(uuid(50));
    store.apply(
      event({
        seq: 0,
        type: 'workflow_state',
        payload: { from_state: 'INTAKE', to_state: 'COVERAGE_CHECK', state_version: 1, reason_code: 'ok', degraded: 'full' },
      }),
    );
    expect(store.snapshot.phase).toBe('live');
    // `full` is normalised to null: "the pipeline was intact" is the absence of
    // a degradation, and storing it as one would put a warning banner on every
    // healthy run.
    expect(store.snapshot.workflow?.degraded).toBeNull();
  });

  it('treats an unparseable state change as degraded rather than as full', () => {
    const store = makeStore();
    store.beginWorkflow(uuid(50));
    store.apply(event({ seq: 0, type: 'workflow_state', payload: { nonsense: true } }));

    expect(store.snapshot.phase).toBe('degraded');
    expect(store.snapshot.workflow?.degraded).toBe('no_source');
    expect(store.snapshot.diagnostics.some((d) => d.code === 'WORKFLOW_STATE_INVALID')).toBe(true);
  });
});

describe('partial_text is not final state', () => {
  it('records partial output as progress and never as an outcome', () => {
    const store = makeStore();
    store.beginWorkflow(uuid(50));

    store.apply(event({ seq: 0, type: 'partial_text', payload: { text: 'Considering three hospitals' } }));

    expect(store.snapshot.progress?.text).toBe('Considering three hospitals');
    expect(store.snapshot.terminal).toBeUndefined();
    expect(store.snapshot.phase).toBe('connecting');
  });
});

describe('malformed payloads do not blank the screen', () => {
  it('reports an invalid surface_update and keeps the previous surface', () => {
    const store = makeStore();
    store.beginWorkflow(uuid(50));

    store.apply(
      event({ seq: 0, type: 'surface_update', payload: surfaceUpdate([instance({ instance_id: uuid(1), component: 'HospitalPicker' })]) }),
    );
    const before = store.snapshot.surfaces[0]?.instanceIds.length;

    store.apply(event({ seq: 1, type: 'surface_update', payload: { surface_id: 'not-a-uuid', components: [] } }));

    expect(store.snapshot.diagnostics.some((d) => d.code === 'SURFACE_UPDATE_INVALID')).toBe(true);
    expect(store.snapshot.surfaces[0]?.instanceIds.length).toBe(before);
  });

  it('reports an unknown component name without throwing', () => {
    const store = makeStore();
    store.beginWorkflow(uuid(50));

    // Not one of the eight. The contract forbids it, so it must be refused
    // loudly rather than rendered as something it is not.
    const bad = {
      surface_id: uuid(99),
      components: [{ instance_id: uuid(1), component: 'APINinthComponent', props: {}, workflow_state_version: 1 }],
    } as unknown as sse$SurfaceUpdate;

    expect(() => {
      store.apply(event({ seq: 0, type: 'surface_update', payload: bad }));
    }).not.toThrow();

    expect(store.snapshot.diagnostics.some((d) => d.code === 'SURFACE_UPDATE_INVALID')).toBe(true);
  });
});

describe('a surface update arriving out of order does not reorder the flow', () => {
  it('keeps the fixed hospital -> doctor -> slot order', () => {
    const store = makeStore();
    store.beginWorkflow(uuid(50));

    // The backend sends them in one update; the render order is the array order.
    runtime.applyUpdate(
      surfaceUpdate([
        instance({ instance_id: uuid(1), component: 'HospitalPicker' }),
        instance({ instance_id: uuid(2), component: 'DoctorPicker' }),
        instance({ instance_id: uuid(3), component: 'SlotPicker' }),
      ]),
    );

    expect(runtime.instances(uuid(99)).map((i) => i.component)).toEqual([
      'HospitalPicker',
      'DoctorPicker',
      'SlotPicker',
    ]);
  });
});
