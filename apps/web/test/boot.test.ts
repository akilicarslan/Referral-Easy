import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { deepText, eventually, uuid } from './helpers.js';

/**
 * The entry point, booted for real.
 *
 * `src/main.ts` is exercised end to end rather than unit-tested with mocks,
 * because the thing worth checking is the wiring a mock would replace.
 *
 * The import has to happen after `#app` exists (the module boots on import), and
 * it can only happen once: jsdom — like a browser — refuses to construct a class
 * that is not in the custom element registry, so re-importing the module graph
 * is not an option. Hence the top-level await below and `boot()` per case.
 */

const root = document.createElement('div');
root.id = 'app';
document.body.append(root);

// The Docker build explicitly supplies an empty value for same-origin requests.
vi.stubEnv('VITE_GATEWAY_URL', '');
const { boot } = await import('../src/main.js');
vi.unstubAllEnvs();

beforeEach(() => {
  document.body.replaceChildren(root);
  root.replaceChildren();
});

afterEach(() => {
  window.history.replaceState({}, '', '/');
});

function screen(): string {
  return deepText(root);
}

describe('the client always puts something on screen', () => {
  it('refreshes an unverified booking after its stream ends and stops when confirmed', async () => {
    vi.useFakeTimers();
    window.history.replaceState({}, '', `/?workflow_id=${uuid(50)}`);
    let reads = 0;
    const dispose = boot({ token: 'demo-token', fetchImpl: async () => {
      reads += 1;
      const confirmed = reads > 1;
      return new Response(JSON.stringify({
        workflow_id: uuid(50), surface_id: uuid(60),
        state: confirmed ? 'BOOKED' : 'BOOKING_UNVERIFIED', state_version: confirmed ? 10 : 9,
        components: [], last_seq: confirmed ? 50 : 44, last_event_id: null,
        terminal: { event_id: (confirmed ? 'b' : 'a').repeat(32), seq: confirmed ? 50 : 44,
          workflow_id: uuid(50), type: 'terminal', created_at: '2026-10-19T20:00:00Z', synthetic: true,
          payload: { to_state: confirmed ? 'BOOKED' : 'BOOKING_UNVERIFIED', reason_code: 'ok' } },
      }));
    } });
    try {
      await vi.advanceTimersByTimeAsync(0);
      expect(screen()).toContain('Booking confirmation pending');
      await vi.advanceTimersByTimeAsync(2000);
      expect(screen()).toContain('The hospital has confirmed this appointment.');
      const finalReads = reads;
      await vi.advanceTimersByTimeAsync(5000);
      expect(reads).toBe(finalReads);
    } finally { dispose(); vi.useRealTimers(); }
  });

  it('opens the stream on the current origin when the configured gateway URL is empty', async () => {
    window.history.replaceState({}, '', `/?workflow_id=${uuid(50)}`);
    let streamUrl: string | undefined;
    boot({ token: 'demo-token', fetchImpl: async () => new Response(JSON.stringify({
      workflow_id: uuid(50), surface_id: uuid(60), state: 'INTAKE', state_version: 1,
      components: [], last_seq: null, last_event_id: null, terminal: null,
    })), transport: { open: (url, _cursor, handlers) => {
      streamUrl = url;
      handlers.onOpen();
      return () => {};
    } } });
    await eventually(() => streamUrl !== undefined, 'the same-origin stream to open');
    const url = new URL(streamUrl!);
    expect(url.origin).toBe(window.location.origin);
    expect(url.pathname).toBe(`/v1/workflows/${uuid(50)}/events`);
    expect(screen()).not.toContain('Invalid URL');
  });

  it('explains a URL with no workflow in it', async () => {
    window.history.replaceState({}, '', '/');
    boot();

    await eventually(() => screen().includes('Open a demo referral'), 'the demo session form');
    expect(root.querySelector('input[name="workflow_id"]')).not.toBeNull();
    expect(root.querySelector('re-referral-app')?.hasAttribute('hidden')).toBe(true);
  });

  it('reports an unreachable stream as unknown, not as an outcome', async () => {
    window.history.replaceState({}, '', `/?workflow_id=${uuid(50)}`);
    boot({ token: 'demo-token', fetchImpl: async () => new Response(JSON.stringify({
      workflow_id: uuid(50), surface_id: uuid(60), state: 'INTAKE', state_version: 1,
      components: [], last_seq: null, last_event_id: null, terminal: null,
    })) });

    await eventually(() => screen().includes('Connection lost'), 'the interrupted state');

    expect(screen()).toContain('not a confirmation and not a failure');
    expect(screen()).toContain('Reconnect and replay');
    // The stream never opened, so there is no outcome and no workflow state.
    expect(screen()).not.toContain('Workflow finished');
  });

  it('attaches a store and a runtime to the app element', () => {
    window.history.replaceState({}, '', `/?workflow_id=${uuid(50)}`);
    boot();

    const app = root.querySelector('re-referral-app') as (HTMLElement & {
      store?: unknown;
      runtime?: unknown;
      streamStatus?: unknown;
    }) | null;

    expect(app, 'the app element should be in the DOM').not.toBeNull();
    expect(app?.store, 'the store should be attached').toBeDefined();
    expect(app?.runtime, 'the runtime should be attached').toBeDefined();
    expect(app?.streamStatus).toBeDefined();
  });
});
