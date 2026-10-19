import { describe, expect, it, beforeEach, afterEach } from 'vitest';
import type { sse$StreamEvent } from '@referraleasy/contracts';
import { SurfaceRuntime } from '../src/a2ui/adapter.js';
import { SurfaceStore } from '../src/stream/surface-store.js';
import { ReferralApp } from '../src/app/referral-app.js';
import { deepQuery, deepText, eventually, instance, surfaceUpdate, uuid } from './helpers.js';

/**
 * The shell, driven exactly the way `src/main.ts` drives it.
 *
 * The point of these tests is the rule the whole app is built around: **a blank
 * screen is a failure.** Each phase has to produce something a patient can read
 * — including the phase where the backend has said nothing at all, and including
 * the one where the stream died without reaching an outcome.
 *
 * The last three tests are the interesting ones. They pin that an unverified
 * outcome does not get the confirmed treatment, that a missing `ok` reason code
 * does not turn into an invented explanation, and that a failed stream is
 * reported as "we do not know" rather than as a result.
 */

let runtime: SurfaceRuntime;
let store: SurfaceStore;
let app: ReferralApp;

const WORKFLOW_ID = uuid(50);
const SURFACE_ID = uuid(99);

function event(
  patch: Partial<sse$StreamEvent> & Pick<sse$StreamEvent, 'seq' | 'type'>,
): sse$StreamEvent {
  return {
    event_id: (patch.seq ?? 0).toString(16).padStart(32, '0'),
    workflow_id: WORKFLOW_ID,
    created_at: new Date(1_700_000_000_000 + (patch.seq ?? 0) * 1000).toISOString(),
    synthetic: true,
    ...patch,
  } as sse$StreamEvent;
}

beforeEach(() => {
  runtime = new SurfaceRuntime();
  app = new ReferralApp();
  app.runtime = runtime;
  app.streamStatus = 'idle';
  store = new SurfaceStore({
    runtime,
    onChange: (snapshot) => {
      app.snapshot = snapshot;
    },
  });
  document.body.append(app);
});

afterEach(() => {
  app.remove();
  runtime.dispose();
});

async function textWhen(predicate: (text: string) => boolean, what: string): Promise<string> {
  await eventually(() => predicate(deepText(app)), what);
  return deepText(app);
}

describe('the shell always renders something readable', () => {
  it('renders an honest empty state before the store has said anything', async () => {
    const text = await textWhen((value) => value.includes('Connecting'), 'the empty state');

    expect(text).toContain('Nothing has been received yet.');
    expect(text).toContain('Synthetic data');
    expect(text.length).toBeGreaterThan(40);
  });

  it('says it is still reaching the service during the connecting phase', async () => {
    store.beginWorkflow(WORKFLOW_ID);

    const text = await textWhen(
      (value) => value.includes('Reaching the referral service'),
      'the connecting state',
    );
    expect(text).toContain('Connecting');
    expect(text).toContain('last seq');
  });

  it('mounts the surface through the app, not just in isolation', async () => {
    store.beginWorkflow(WORKFLOW_ID);
    store.setLive();
    store.apply(
      event({
        seq: 0,
        type: 'surface_update',
        payload: surfaceUpdate(
          [
            instance({
              instance_id: uuid(1),
              component: 'HospitalPicker',
              props: { title: 'Pick a hospital' },
            }),
          ],
          { surface_id: SURFACE_ID },
        ),
      }),
    );

    await eventually(
      () => deepQuery(app, 're-hospital-picker') !== null,
      'the surface to be mounted inside the app shell',
    );

    expect(deepText(app)).toContain('Pick a hospital');
    // Live and healthy: no banner, just the surface.
    expect(deepText(app)).not.toContain('Reduced lookup');
  });

  it('shows a reduced pipeline when the workflow is degraded', async () => {
    store.beginWorkflow(WORKFLOW_ID);
    store.apply(
      event({
        seq: 0,
        type: 'workflow_state',
        payload: {
          from_state: 'INTAKE',
          to_state: 'PROVIDER_MATCHING',
          state_version: 2,
          reason_code: 'ok',
          degraded: 'opensearch_down',
        },
      }),
    );

    const text = await textWhen((value) => value.includes('Reduced lookup'), 'the degraded state');

    expect(text).toContain('The directory index was unavailable');
    expect(text).toContain('opensearch down');
  });

  it('reports a dropped stream as unknown, not as an outcome', async () => {
    store.beginWorkflow(WORKFLOW_ID);
    store.setLive();
    store.setInterrupted();

    const text = await textWhen((value) => value.includes('Connection lost'), 'the interrupted state');

    expect(text).toContain('not a confirmation and not a failure');
    expect(text).toContain('Reconnect and replay');
    expect(text).not.toContain('Workflow finished');
  });
});

describe('the outcome page cannot overstate what happened', () => {
  function terminal(state: string, reasonCode: string): sse$StreamEvent {
    return event({ seq: 0, type: 'terminal', payload: { to_state: state, reason_code: reasonCode } });
  }

  it('does not give a verified-free outcome the confirmed treatment', async () => {
    store.beginWorkflow(WORKFLOW_ID);
    store.apply(terminal('BOOKING_UNVERIFIED', 'booking_outcome_unknown'));

    const text = await textWhen((value) => value.includes('Workflow finished'), 'the outcome page');

    expect(text).toContain('booking unverified');
    expect(text).toContain('The booking was sent but the outcome could not be confirmed.');

    // The glyph is not a tick, and the tone is not `ok`.
    const glyph = deepQuery(app, '.page-glyph');
    expect(glyph?.textContent?.trim()).toBe('?');
    expect(glyph?.classList.contains('ok')).toBe(false);
  });

  it('softens a cancelled referral rather than calling it a failed booking', async () => {
    store.beginWorkflow(WORKFLOW_ID);
    store.apply(terminal('CANCELLED', 'ok'));

    const text = await textWhen((value) => value.includes('Workflow finished'), 'the outcome page');

    expect(text).toContain('stopped before a booking was made');
    expect(text).not.toContain('could not be completed automatically');
    expect(deepQuery(app, '.page-glyph')?.textContent?.trim()).toBe('○');
  });

  it('does not invent an explanation from the success reason code', async () => {
    store.beginWorkflow(WORKFLOW_ID);
    store.apply(terminal('BOOKED', 'ok'));

    const text = await textWhen((value) => value.includes('Workflow finished'), 'the outcome page');

    // `ok` is a real ReasonCode and means "nothing to explain". Falling through
    // to the generic sentence would have told the patient this step could not be
    // completed — on a booking that succeeded.
    expect(text).toContain('The hospital has confirmed this appointment.');
    expect(text).not.toContain('could not be completed automatically');
    expect(text).not.toContain('no explanation recorded');
  });
});

describe('problems in the stream are visible rather than swallowed', () => {
  it('lists adapter diagnostics under a disclosure', async () => {
    store.beginWorkflow(WORKFLOW_ID);
    store.addDiagnostic({
      code: 'SURFACE_UPDATE_INVALID',
      message: 'A surface_update did not match the contract and was skipped.',
    });

    const text = await textWhen((value) => value.includes('problem in the stream'), 'the diagnostics');

    expect(text).toContain('SURFACE_UPDATE_INVALID');
    expect(text).toContain('did not match the contract');
  });

  it('labels progressive output as not final', async () => {
    store.beginWorkflow(WORKFLOW_ID);
    store.apply(event({ seq: 0, type: 'partial_text', payload: { text: 'Considering three hospitals' } }));

    const text = await textWhen((value) => value.includes('Considering three hospitals'), 'progress');

    expect(text).toContain('not a final answer');
  });
});
