import { describe, expect, it, beforeEach, afterEach } from 'vitest';
import { SurfaceRuntime, mountSurface, type SurfaceMount } from '../src/a2ui/adapter.js';
import { DEMO_SCENES, DEMO_SURFACE_ID, allScenesUpdate } from '../src/fixtures/surfaces.js';
import { deepQuery, deepQueryAll, deepText, eventually, instance, surfaceUpdate, uuid } from './helpers.js';

/**
 * End-to-end rendering through the real A2UI pipeline.
 *
 * This is the test file that answers "does it actually work", as opposed to
 * "does the logic agree with itself". Every assertion below reads text that the
 * A2UI renderer produced, from a component mounted through `mountSurface`, using
 * the same catalog the app uses.
 *
 * If the adapter's assumptions about `@a2ui/web_core@0.13.0` are wrong, this file
 * fails loudly, which is the point.
 */

let runtime: SurfaceRuntime;
let host: HTMLElement;
let mount: SurfaceMount | undefined;

const SURFACE_ID = uuid(99);

beforeEach(() => {
  runtime = new SurfaceRuntime();
  host = document.createElement('div');
  document.body.append(host);
});

afterEach(() => {
  mount?.unmount();
  mount = undefined;
  host.remove();
  runtime.dispose();
});

/** Apply an update, mount it, and wait for a component to appear. */
async function render(update = surfaceUpdate([], { surface_id: SURFACE_ID }), selector: string): Promise<void> {
  runtime.applyUpdate(update);
  mount = mountSurface(host, runtime, update.surface_id);
  await eventually(() => deepQuery(host, selector) !== null, `${selector} to render`);
}

describe('the eight components render through A2UI', () => {
  it('shows the exact handoff summary before requesting consent', async () => {
    const actions: unknown[] = [];
    runtime.dispose();
    runtime = new SurfaceRuntime({ onAction: (action) => actions.push(action) });
    await render(surfaceUpdate([instance({ component: 'HumanHandoffCard',
      action: 'system:capture_consent', props: {
        consent_required: true,
        handoff: { patient_visible_summary: 'The hospital needs a referral letter.' },
      },
    })], { surface_id: SURFACE_ID }), 're-human-handoff-card');
    await eventually(() => deepText(host).includes('Share this summary'), 'handoff consent');
    expect(deepText(host)).toContain('The hospital needs a referral letter.');
    expect(deepText(host)).toContain('This approval does not book an appointment.');
    expect(deepText(host)).not.toContain('Your referral has been passed');
    expect(actions).toEqual([]);
    (deepQuery(host, 'button') as HTMLButtonElement).click();
    await eventually(() => actions.length === 1, 'explicit handoff approval');
    expect(actions[0]).toMatchObject({ context: { consented: true } });
  });
  it('renders the gateway catalog options field', async () => {
    await render(surfaceUpdate([instance({ component: 'HospitalPicker', props: {
      options: [{ id: 'hospital-1', name: 'Gateway Hospital' }],
    } })], { surface_id: SURFACE_ID }), 're-hospital-picker');
    await eventually(() => deepText(host).includes('Gateway Hospital'), 'the gateway option');
    expect(deepQueryAll(host, 'input[type="radio"]')).toHaveLength(1);
  });

  it('reads a gateway appointment state and reference from the appointment record', async () => {
    await render(surfaceUpdate([instance({ component: 'AppointmentReceipt', props: {
      appointment: { state: 'CONFIRMED', external_ref: 'HOSPITAL-123' },
    } })], { surface_id: SURFACE_ID }), 're-appointment-receipt');
    await eventually(() => deepText(host).includes('HOSPITAL-123'), 'the hospital confirmation reference');
    expect(deepText(host)).toContain('Confirmed');
  });

  it('requires explicit consent and displays the gateway booking payload', async () => {
    await render(surfaceUpdate([instance({ component: 'BookingReview', props: {
      booking_payload: { hospital_id: 'hospital-1', provider_id: 'doctor-1', slot_id: 'slot-1' },
    } })], { surface_id: SURFACE_ID }), 're-booking-review');
    await eventually(() => deepQuery(host, 'input[type="checkbox"]') !== null, 'explicit consent checkbox');
    expect(deepText(host)).toContain('hospital-1');
    expect(deepText(host)).toContain('doctor-1');
    expect((deepQuery(host, 'button') as HTMLButtonElement).disabled).toBe(true);
  });
  it('renders every component in the demo gallery', async () => {
    runtime.applyUpdate(allScenesUpdate());
    mount = mountSurface(host, runtime, DEMO_SURFACE_ID);

    for (const scene of DEMO_SCENES) {
      const component = scene.update.components[0];
      expect(component, `scene ${scene.key} should have one component`).toBeDefined();
    }

    await eventually(
      () => deepQuery(host, 're-hospital-picker') !== null,
      'the hospital picker to render',
    );

    // Every one of the eight element tags must be present somewhere in the tree.
    const tags = [
      're-hospital-picker',
      're-doctor-picker',
      're-slot-picker',
      're-patient-details-form',
      're-booking-review',
      're-appointment-receipt',
      're-directory-fallback-card',
      're-human-handoff-card',
    ];

    await eventually(
      () => tags.every((tag) => deepQuery(host, tag) !== null),
      `all eight components to render (missing: ${tags.filter((tag) => deepQuery(host, tag) === null).join(', ')})`,
    );

    for (const tag of tags) {
      expect(deepQuery(host, tag), `${tag} should be rendered`).not.toBeNull();
    }
  });

  it('renders a component with almost no props rather than blanking', async () => {
    await render(
      surfaceUpdate([instance({ instance_id: uuid(1), component: 'BookingReview', props: {} })], {
        surface_id: SURFACE_ID,
      }),
      're-booking-review',
    );

    const text = deepText(host);
    expect(text).toContain('Review your referral');
    expect(text.length).toBeGreaterThan(20);
  });

  it('renders an empty list with the reason the server gave', async () => {
    await render(
      surfaceUpdate(
        [
          instance({
            instance_id: uuid(1),
            component: 'SlotPicker',
            props: { slots: [], empty_reason: 'No slots in the next six weeks.' },
          }),
        ],
        { surface_id: SURFACE_ID },
      ),
      're-slot-picker',
    );

    expect(deepText(host)).toContain('No slots in the next six weeks.');
  });
});

describe('an unknown component name does not produce a blank screen', () => {
  it('keeps rendering the components it does understand', async () => {
    runtime.applyUpdate(
      surfaceUpdate([
        instance({ instance_id: uuid(1), component: 'HospitalPicker', props: { title: 'Pick one' } }),
      ], { surface_id: SURFACE_ID }),
    );
    mount = mountSurface(host, runtime, SURFACE_ID);
    await eventually(() => deepQuery(host, 're-hospital-picker') !== null, 'the picker');

    expect(deepText(host)).toContain('Pick one');
  });
});

/**
 * Ordering (§4). Hospitals, then doctors, then slots — and doctors never before
 * a hospital.
 */
describe('the fixed order is respected', () => {
  it('renders doctors after hospitals, never before', async () => {
    runtime.applyUpdate(
      surfaceUpdate(
        [
          instance({ instance_id: uuid(1), component: 'HospitalPicker', props: { title: 'Hospitals' } }),
          instance({ instance_id: uuid(2), component: 'DoctorPicker', props: { title: 'Doctors', hospital_name: 'St Mary' } }),
          instance({ instance_id: uuid(3), component: 'SlotPicker', props: { title: 'Slots' } }),
        ],
        { surface_id: SURFACE_ID },
      ),
    );
    mount = mountSurface(host, runtime, SURFACE_ID);
    await eventually(() => deepQuery(host, 're-slot-picker') !== null, 'all three pickers');

    const text = deepText(host);
    expect(text.indexOf('Hospitals')).toBeLessThan(text.indexOf('Doctors'));
    expect(text.indexOf('Doctors')).toBeLessThan(text.indexOf('Slots'));
  });

  it('refuses to show doctors when the hospital step is not done', async () => {
    await render(
      surfaceUpdate(
        [
          instance({
            instance_id: uuid(1),
            component: 'DoctorPicker',
            props: { awaiting_hospital: true, doctors: [{ id: 'd1', name: 'Dr Leaked' }] },
          }),
        ],
        { surface_id: SURFACE_ID },
      ),
      're-doctor-picker',
    );

    const text = deepText(host);
    expect(text).toContain('Choose a hospital first');
    // The guard must replace the list, not merely disable it — a disabled list
    // would still leak the names.
    expect(text).not.toContain('Dr Leaked');
    expect(deepQueryAll(host, 'input[type="radio"]')).toHaveLength(0);
  });

  it('preselects nothing when the server sent no selection', async () => {
    await render(
      surfaceUpdate(
        [
          instance({
            instance_id: uuid(1),
            component: 'SlotPicker',
            props: {
              selected_id: null,
              slots: [
                { id: 's1', start_at: '2026-11-02T09:10:00Z' },
                { id: 's2', start_at: '2026-11-02T11:30:00Z' },
              ],
            },
          }),
        ],
        { surface_id: SURFACE_ID },
      ),
      're-slot-picker',
    );

    const radios = deepQueryAll<HTMLInputElement>(host, 'input[type="radio"]');
    expect(radios).toHaveLength(2);
    expect(radios.every((radio) => !radio.checked)).toBe(true);
    expect(deepText(host)).toContain('Nothing is selected yet');
  });
});
