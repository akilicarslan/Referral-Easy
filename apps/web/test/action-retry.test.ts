import { expect, it } from 'vitest';
import { SurfaceRuntime, mountSurface } from '../src/a2ui/adapter.js';
import { SurfaceStore, type ClientActionShape } from '../src/stream/surface-store.js';
import { deepQuery, eventually, instance, surfaceUpdate, uuid } from './helpers.js';

it('re-enables a failed booking action and retries with the same command id', async () => {
  const sent: ClientActionShape[] = [];
  const runtime = new SurfaceRuntime({ onAction: (action) => store.submit(action) });
  const store = new SurfaceStore({ runtime, onChange: () => {}, sendAction: async (action) => {
    sent.push(action);
    if (sent.length === 1) throw new Error('connection dropped before the response');
  } });
  store.beginWorkflow(uuid(50));
  const update = surfaceUpdate([instance({ component: 'BookingReview', action: 'system:capture_consent', props: {
    booking_payload: { hospital_id: 'hospital-1' },
  } })]);
  store.apply({ event_id: '0'.repeat(32), seq: 0, type: 'surface_update', workflow_id: uuid(50),
    payload: update, created_at: '2026-10-18T00:00:00Z', synthetic: true });
  const host = document.createElement('div');
  document.body.append(host);
  const mount = mountSurface(host, runtime, update.surface_id);
  try {
    await eventually(() => deepQuery(host, 'input[type="checkbox"]') !== null, 'consent checkbox');
    const checkbox = deepQuery(host, 'input[type="checkbox"]') as HTMLInputElement;
    checkbox.checked = true;
    checkbox.dispatchEvent(new Event('change', { bubbles: true }));
    await eventually(() => !(deepQuery(host, 'button') as HTMLButtonElement).disabled, 'enabled submit');
    (deepQuery(host, 'button') as HTMLButtonElement).click();
    await eventually(() => store.snapshot.diagnostics.some((item) => item.code === 'ACTION_SEND_FAILED'), 'delivery failure');
    await new Promise((resolve) => setTimeout(resolve, 0));
    await eventually(() => sent.length === 1 && !(deepQuery(host, 'button') as HTMLButtonElement).disabled, 'retry after transport failure');
    (deepQuery(host, 'button') as HTMLButtonElement).click();
    await eventually(() => sent.length === 2, 'second delivery');
    expect(sent[0]!.command_id).toBe(sent[1]!.command_id);
  } finally { mount.unmount(); runtime.dispose(); host.remove(); }
});
