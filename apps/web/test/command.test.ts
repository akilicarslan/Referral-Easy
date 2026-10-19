import { describe, expect, it, beforeEach, afterEach } from 'vitest';
import type { sse$ComponentInstance } from '@referraleasy/contracts';
import { CommandRegistry, toClientAction } from '../src/stream/command.js';
import { SurfaceRuntime } from '../src/a2ui/adapter.js';
import { SurfaceStore } from '../src/stream/surface-store.js';
import { instance, surfaceUpdate, uuid } from './helpers.js';

/**
 * `command_id` is the idempotency key, and one key belongs to one *intent*.
 *
 * The tests below pin the consequence that matters: a double click, and a
 * retried request after a failure, both reuse the key — so neither can become a
 * second booking. They also pin the opposite: a genuinely different intent gets a
 * fresh key, because collapsing those would silently drop a patient's answer.
 */

let runtime: SurfaceRuntime;

beforeEach(() => {
  runtime = new SurfaceRuntime();
});

afterEach(() => {
  runtime.dispose();
});

function component(patch: Partial<sse$ComponentInstance> = {}): sse$ComponentInstance {
  return instance({
    instance_id: uuid(1),
    component: 'HospitalPicker',
    action: 'system:await_patient_choice',
    ...patch,
  });
}

describe('one command_id per intent', () => {
  it('reuses the key when the same button is pressed again', () => {
    const registry = new CommandRegistry(() => 'cmd-1');
    const target = component();

    const first = registry.begin(target);
    const second = registry.begin(target);

    expect(second.commandId).toBe(first.commandId);
    expect(second.attempt).toBe(2);
    expect(registry.entries()).toHaveLength(1);
  });

  it('reuses the key across a retry after a failure', () => {
    let counter = 0;
    const registry = new CommandRegistry(() => `cmd-${++counter}`);
    const target = component();

    const attemptOne = registry.begin(target);
    const attemptTwo = registry.begin(target);
    const attemptThree = registry.begin(target);

    expect(new Set([attemptOne.commandId, attemptTwo.commandId, attemptThree.commandId]).size).toBe(1);
    expect(counter).toBe(1);
  });

  it('mints a fresh key for a different instance', () => {
    let counter = 0;
    const registry = new CommandRegistry(() => `cmd-${++counter}`);

    const a = registry.begin(component({ instance_id: uuid(1) }));
    const b = registry.begin(component({ instance_id: uuid(2) }));

    expect(a.commandId).not.toBe(b.commandId);
    expect(registry.entries()).toHaveLength(2);
  });

  it('mints a fresh key for a different server-issued action on the same instance', () => {
    let counter = 0;
    const registry = new CommandRegistry(() => `cmd-${++counter}`);

    const a = registry.begin(component({ action: 'system:await_patient_choice' }));
    const b = registry.begin(component({ action: 'scheduling:create_booking' }));

    expect(a.commandId).not.toBe(b.commandId);
  });

  it('releases keys built from a superseded workflow state version', () => {
    const registry = new CommandRegistry(() => 'cmd-1');
    registry.begin(component({ workflow_state_version: 3 }));
    registry.begin(component({ instance_id: uuid(2), workflow_state_version: 9 }));

    const released = registry.releaseUpTo(5);

    expect(released).toHaveLength(1);
    expect(registry.entries().map((entry) => entry.workflowStateVersion)).toEqual([9]);
  });
});

describe('the client echoes the server-issued action and never invents one', () => {
  it('builds an action from the instance', () => {
    const target = component({ action: 'scheduling:create_booking', workflow_state_version: 7 });
    const command = new CommandRegistry(() => 'cmd-1').begin(target);

    const payload = toClientAction(target, command, { slot_id: 's1' });

    expect(payload).toEqual({
      instance_id: target.instance_id,
      action: 'scheduling:create_booking',
      workflow_state_version: 7,
      command_id: 'cmd-1',
      value: { slot_id: 's1' },
    });
  });

  it('produces nothing at all for a display-only instance', () => {
    const target = component({ action: null });
    const command = new CommandRegistry(() => 'cmd-1').begin(target);

    expect(toClientAction(target, command, null)).toBeUndefined();
  });
});

describe('the store refuses actions it must not send', () => {
  function storeWithRecorder(): { store: SurfaceStore; sent: unknown[] } {
    const sent: unknown[] = [];
    const store = new SurfaceStore({
      runtime,
      onChange: () => {},
      sendAction: async (action) => {
        sent.push(action);
      },
    });
    return { store, sent };
  }

  it('does not send a second action for an instance the server marked submitted', async () => {
    const { store, sent } = storeWithRecorder();
    store.beginWorkflow(uuid(50));

    const target = component({ submitted: true });
    store.apply({
      event_id: '0'.repeat(32),
      workflow_id: uuid(50),
      seq: 0,
      type: 'surface_update',
      payload: surfaceUpdate([target]),
      created_at: new Date().toISOString(),
      synthetic: true,
    });

    store.submit({ instanceId: target.instance_id, name: 'hospital.select', context: { hospital_id: 'h1' } });

    expect(sent).toHaveLength(0);
    expect(store.snapshot.diagnostics.some((d) => d.code === 'ACTION_ON_SUBMITTED_INSTANCE')).toBe(true);
  });

  it('does not send an action for an instance the server disabled', async () => {
    const { store, sent } = storeWithRecorder();
    store.beginWorkflow(uuid(50));

    const target = component({ disabled: true });
    store.apply({
      event_id: '1'.repeat(32),
      workflow_id: uuid(50),
      seq: 0,
      type: 'surface_update',
      payload: surfaceUpdate([target]),
      created_at: new Date().toISOString(),
      synthetic: true,
    });

    store.submit({ instanceId: target.instance_id, name: 'hospital.select', context: {} });

    expect(sent).toHaveLength(0);
    expect(store.snapshot.diagnostics.some((d) => d.code === 'ACTION_ON_DISABLED_INSTANCE')).toBe(true);
  });

  it('uses one command_id for two rapid clicks on the same intent', async () => {
    const { store, sent } = storeWithRecorder();
    store.beginWorkflow(uuid(50));

    const target = component();
    store.apply({
      event_id: '2'.repeat(32),
      workflow_id: uuid(50),
      seq: 0,
      type: 'surface_update',
      payload: surfaceUpdate([target]),
      created_at: new Date().toISOString(),
      synthetic: true,
    });

    store.submit({ instanceId: target.instance_id, name: 'hospital.select', context: { hospital_id: 'h1' } });
    store.submit({ instanceId: target.instance_id, name: 'hospital.select', context: { hospital_id: 'h1' } });

    expect(sent).toHaveLength(1);
    const ids = new Set(
      sent.map((action) => (action as { command_id: string }).command_id),
    );
    expect(ids.size, 'both clicks must share one idempotency key').toBe(1);
  });
});
