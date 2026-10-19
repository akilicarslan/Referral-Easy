import { describe, expect, it, beforeEach, afterEach } from 'vitest';
import { SurfaceRuntime, mountSurface, type SurfaceMount } from '../src/a2ui/adapter.js';
import { deepQuery, deepText, eventually, instance, surfaceUpdate, uuid } from './helpers.js';

/**
 * The rule the whole project exists to demonstrate:
 *
 *   **`REQUESTED` and `PENDING_REVIEW` are never shown as confirmed.**
 *   HTTP 200 is not confirmation.
 *
 * The assertions are written as *forbidden language*, not as expected strings,
 * because the failure mode is a component that quietly gains a green tick. A
 * whitelist of allowed words would let a new synonym through; a blacklist of
 * confirmation words catches "your appointment is confirmed", "Booking
 * confirmed", and the next phrasing someone invents.
 */

let runtime: SurfaceRuntime;
let host: HTMLElement;
let mount: SurfaceMount | undefined;

const SURFACE_ID = uuid(99);

/** Words that assert the hospital has accepted the booking. */
const CONFIRMATION_WORDS = [
  'confirmed',
  'confirmation of your appointment',
  'is booked',
  'has been booked',
  'successfully booked',
  'you are booked',
  'appointment is set',
];

const UNCONFIRMED_STATES = ['REQUESTED', 'PENDING_REVIEW', 'PENDING', 'AWAITING_CONFIRMATION'];
const CONFIRMED_STATES = ['BOOKED', 'CONFIRMED'];

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

async function renderReceipt(props: Record<string, unknown>): Promise<string> {
  const update = surfaceUpdate(
    [instance({ instance_id: uuid(1), component: 'AppointmentReceipt', props })],
    { surface_id: SURFACE_ID },
  );
  runtime.applyUpdate(update);
  mount = mountSurface(host, runtime, update.surface_id);
  await eventually(
    () => deepQuery(host, 're-appointment-receipt') !== null,
    'the receipt to render',
  );
  return deepText(host).toLowerCase();
}

describe('an unconfirmed booking never renders as confirmed', () => {
  for (const state of UNCONFIRMED_STATES) {
    it(`renders ${state} as requested, with no confirmation language`, async () => {
      const text = await renderReceipt({
        state,
        confirmation_reference: 'REF-8891-QK',
        hospital: { name: "St. Mary's General" },
        provider: { name: 'Dr Amelia Okafor' },
        slot: { start_at: '2026-11-02T09:10:00Z' },
      });

      // The positive claim: it says it is requested and that the hospital will confirm.
      expect(text).toContain('requested');
      expect(text).toContain('the hospital will confirm');

      // And it never says the thing that would be a lie.
      for (const word of CONFIRMATION_WORDS) {
        expect(text, `${state} must not say "${word}"`).not.toContain(word);
      }
    });
  }

  it('does not present the request reference as a confirmation', async () => {
    const text = await renderReceipt({
      state: 'PENDING_REVIEW',
      confirmation_reference: 'REF-8891-QK',
    });

    expect(text).toContain('ref-8891-qk');
    expect(text).toContain('a request reference, not a confirmation');
  });

  it('does not use the confirmed colour for a requested booking', async () => {
    await renderReceipt({ state: 'REQUESTED' });
    const block = deepQuery(host, '.status-block');
    expect(block).not.toBeNull();
    expect(block?.classList.contains('requested')).toBe(true);
    expect(block?.classList.contains('ok')).toBe(false);
  });

  it('does not show a tick glyph for a requested booking', async () => {
    await renderReceipt({ state: 'REQUESTED' });
    const glyph = deepQuery(host, '.status-block .glyph');
    expect(glyph?.textContent?.trim()).not.toBe('✓');
  });
});

describe('only a genuinely confirmed booking reads as confirmed', () => {
  for (const state of CONFIRMED_STATES) {
    it(`renders ${state} as confirmed`, async () => {
      const text = await renderReceipt({
        state,
        confirmation_reference: 'REF-8891-QK',
        slot: { start_at: '2026-11-02T09:10:00Z' },
        display_timezone: 'Europe/London',
      });

      expect(text).toContain('confirmed');
      expect(deepQuery(host, '.status-block.ok')).not.toBeNull();
    });
  }

  it('shows the confirmation reference only once confirmed', async () => {
    const text = await renderReceipt({
      state: 'BOOKED',
      confirmation_reference: 'REF-8891-QK',
    });
    expect(text).toContain('confirmation reference');
  });
});

describe('an outcome we could not establish is shown as unknown', () => {
  it('renders BOOKING_UNVERIFIED as not verified, and neither as success nor failure', async () => {
    const text = await renderReceipt({
      state: 'BOOKING_UNVERIFIED',
      booking_request_id: 'BR-2026-000419',
      derived_from: 'adapter_timeout',
    });

    expect(text).toContain('not verified');
    expect(text).toContain('could not verify');
    for (const word of CONFIRMATION_WORDS) {
      expect(text).not.toContain(word);
    }
    expect(text).not.toContain('did not go through');
    expect(deepQuery(host, '.status-block.unknown')).not.toBeNull();
  });

  it('falls back to unknown for a state it does not recognise', async () => {
    const text = await renderReceipt({ state: 'SOME_NEW_STATE_FROM_THE_FUTURE' });

    expect(text).toContain('not verified');
    for (const word of CONFIRMATION_WORDS) {
      expect(text).not.toContain(word);
    }
  });

  it('falls back to unknown when the state is missing entirely', async () => {
    const text = await renderReceipt({ hospital: { name: "St. Mary's General" } });

    expect(text).toContain('not verified');
    for (const word of CONFIRMATION_WORDS) {
      expect(text).not.toContain(word);
    }
  });
});

describe('degradation is surfaced on the receipt', () => {
  it('shows a reduced-pipeline notice when degraded is set', async () => {
    const text = await renderReceipt({ state: 'BOOKED', degraded: 'opensearch_down' });
    expect(text).toContain('reduced lookup');
    expect(text).toContain('directory index was unavailable');
  });

  it('shows nothing extra when the pipeline was full', async () => {
    const text = await renderReceipt({ state: 'BOOKED', degraded: 'full' });
    expect(text).not.toContain('reduced lookup');
  });
});
