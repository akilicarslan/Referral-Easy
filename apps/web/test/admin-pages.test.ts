import { afterEach, describe, expect, it } from 'vitest';

import { AdminClient, type AdminFilters, type ReadResult } from '../src/admin/api.js';
import { AdminTraces } from '../src/admin/pages/traces.js';
import { AdminAgents } from '../src/admin/pages/agents.js';
import { AdminAlerts } from '../src/admin/pages/alerts.js';
import { AdminHumanQueue } from '../src/admin/pages/human-queue.js';
import { AdminScenarios } from '../src/admin/pages/scenarios.js';
import '../src/admin/pages/registry.js';

/**
 * The two pages that read a real endpoint, driven through the DOM.
 *
 * The assertions are about the things a reader would be misled by: a measurement rendered without its
 * sample, a value that could not be measured rendered as zero, and — the one with teeth — a trace id
 * that is not an id being put into a request path.
 */

const SAMPLE = { count: 4, documents: 5, duplicated: 1, truncated: false, other: 0 };
const FILTERS: AdminFilters = { range: 'day', timezone: 'UTC' };

class StubClient extends AdminClient {
  readonly asked: string[] = [];

  constructor(private readonly answer: ReadResult<Record<string, unknown>>) {
    super('http://gateway', 'coordinator@demo', (() => Promise.reject(new Error('no fetch'))) as never);
  }

  override read(page: string, _filters: AdminFilters): Promise<ReadResult<Record<string, unknown>>> {
    this.asked.push(page);
    return Promise.resolve(this.answer);
  }
}

const mounted: HTMLElement[] = [];

async function show<T extends HTMLElement>(
  element: T,
  client: StubClient,
  tail?: string,
): Promise<T> {
  (element as T & { client: AdminClient }).client = client;
  (element as T & { filters: AdminFilters }).filters = FILTERS;
  if (tail !== undefined) {
    (element as T & { tail: string }).tail = tail;
  }
  document.body.append(element);
  mounted.push(element);
  await (element as T & { updateComplete: Promise<unknown> }).updateComplete;
  // The read resolves in a microtask after the first render.
  await Promise.resolve();
  await (element as T & { updateComplete: Promise<unknown> }).updateComplete;
  return element;
}

afterEach(() => {
  for (const element of mounted.splice(0)) element.remove();
});

describe('Agents & Versions', () => {
  const payload: ReadResult<Record<string, unknown>> = {
    kind: 'data',
    data: {
      available: true,
      dimensions: {
        'agent.role': [{ key: 'scheduling', value: 4, sample: SAMPLE }],
        'agent.version': [{ key: 'v12', value: 4, sample: SAMPLE }],
        'agent.release': [],
        'model.name': [{ key: 'gemini-flash', value: 4, sample: SAMPLE }],
      },
    },
  };

  it('renders every dimension the read returned, including the empty one', async () => {
    const client = new StubClient(payload);
    const page = await show(new AdminAgents(), client);

    const text = page.shadowRoot!.textContent ?? '';
    expect(text).toContain('Agent role');
    expect(text).toContain('scheduling');
    expect(text).toContain('Model');
    expect(text).toContain('gemini-flash');
    // An empty dimension says so, rather than being dropped from the page: a filter list that
    // silently omits a dimension is one an operator cannot tell from a complete one.
    expect(text).toContain('No events in this window carried a Release');
    expect(client.asked).toEqual(['agents']);
  });

  it('carries the sample beside each value', async () => {
    const client = new StubClient(payload);
    const page = await show(new AdminAgents(), client);

    const text = page.shadowRoot!.textContent ?? '';
    // The dedup signal survives to the screen, which is what makes the count checkable.
    expect(text).toContain('duplicate collapsed');
  });

  it('names the half it does not show', async () => {
    const client = new StubClient(payload);
    const page = await show(new AdminAgents(), client);

    expect(page.shadowRoot!.textContent).toContain('Agent Card');
  });
});

describe('Trace Explorer', () => {
  const list: ReadResult<Record<string, unknown>> = {
    kind: 'data',
    data: {
      available: true,
      traces: [
        {
          trace_id: '4bf92f3577b34da6a3ce929d0e0e4736',
          service_name: 'gateway',
          entry_span: 'POST /v1/workflows',
          started_at: '2026-10-15T06:00:00Z',
          duration_ms: 250,
          span_count: 6,
          sample: { count: 6, documents: 6, duplicated: 0, truncated: false, other: 0 },
        },
      ],
    },
  };

  const detail: ReadResult<Record<string, unknown>> = {
    kind: 'data',
    data: {
      available: true,
      trace_id: '4bf92f3577b34da6a3ce929d0e0e4736',
      span_count: 2,
      spans: [
        {
          span_id: 'a',
          parent_span_id: null,
          name: 'POST /v1/workflows',
          service_name: 'gateway',
          kind: 'SERVER',
          started_at: '2026-10-15T06:00:00.000Z',
          duration_ms: 100,
          attributes: { 'http.status_code': 200 },
        },
        {
          span_id: 'b',
          parent_span_id: 'a',
          name: 'schedule.hold',
          service_name: 'scheduling',
          kind: 'CLIENT',
          // Half way through the parent, for a quarter of its duration.
          started_at: '2026-10-15T06:00:00.050Z',
          duration_ms: 25,
          attributes: {},
        },
      ],
    },
  };

  it('lists traces and links each row to its waterfall', async () => {
    const client = new StubClient(list);
    const page = await show(new AdminTraces(), client);

    const link = page.shadowRoot!.querySelector('tbody a') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe('#/traces/4bf92f3577b34da6a3ce929d0e0e4736');
    expect(page.shadowRoot!.textContent).toContain('gateway');
    expect(page.shadowRoot!.textContent).toContain('250.0 ms');
    expect(client.asked).toEqual(['traces']);
  });

  it('refuses a tail that is not a trace id, and asks for nothing', async () => {
    // The id goes into a request path. Validating the shape here is what stops a hash segment from
    // becoming a path the page did not mean — `../` is not a trace.
    for (const bad of ['../events', 'not-hex', 'deadbeef', '%2e%2e']) {
      const client = new StubClient(detail);
      const page = await show(new AdminTraces(), client, bad);

      expect(page.shadowRoot!.textContent, bad).toContain('is not a trace id');
      expect(client.asked, bad).toEqual([]);
      mounted.splice(mounted.indexOf(page), 1);
      page.remove();
    }
  });

  it('accepts an id of the right shape and shows the waterfall', async () => {
    const client = new StubClient(detail);
    const page = await show(new AdminTraces(), client, '4bf92f3577b34da6a3ce929d0e0e4736');

    expect(client.asked).toEqual(['traces/4bf92f3577b34da6a3ce929d0e0e4736']);
    expect(page.shadowRoot!.textContent).toContain('schedule.hold');
    // The attributes each span carried, which §15 keeps as the span's own record.
    expect(page.shadowRoot!.textContent).toContain('http.status_code=200');
  });

  it('draws the bar from the start offset, not from the order the spans arrived in', async () => {
    const client = new StubClient(detail);
    const page = await show(new AdminTraces(), client, '4bf92f3577b34da6a3ce929d0e0e4736');

    // `getAttribute('style')` rather than `style.cssText`: Lit binds `style` as an attribute, and
    // jsdom parses it into `.style` lazily enough that the raw attribute is the reliable read.
    const bars = [...page.shadowRoot!.querySelectorAll('.row-bar')].map(
      (bar) => bar.getAttribute('style') ?? '',
    );
    // The trace runs 100ms from origin: the child starts at 50ms (50%) and lasts 25ms (25%).
    expect(bars[0]).toContain('left:0.00%');
    expect(bars[0]).toContain('width:100.00%');
    expect(bars[1]).toContain('left:50.00%');
    expect(bars[1]).toContain('width:25.00%');
  });

  it('renders a missing duration as no data rather than an instantaneous span', async () => {
    const missing = structuredClone(detail) as unknown as ReadResult<Record<string, unknown>>;
    const spans = (missing as unknown as { data: { spans: Record<string, unknown>[] } }).data.spans;
    delete spans[0]!.duration_ms;
    const client = new StubClient(missing);
    const page = await show(new AdminTraces(), client, '4bf92f3577b34da6a3ce929d0e0e4736');

    expect(page.shadowRoot!.textContent).toContain('no data');
  });

  it('says which two things an empty waterfall could mean rather than guessing', async () => {
    const client = new StubClient({ kind: 'data', data: { available: true, trace_id: 'abc', spans: [] } });
    const page = await show(new AdminTraces(), client, '4bf92f3577b34da6a3ce929d0e0e4736');

    const text = page.shadowRoot!.textContent ?? '';
    expect(text).toContain('seven-day retention');
    expect(text).toContain('does not guess');
  });
});

describe('Fault Scenarios', () => {
  const payload: ReadResult<Record<string, unknown>> = {
    kind: 'data',
    data: {
      available: true,
      runs: { linked: false, reason: 'the Scenario Runner that produces one is Phase 7.4' },
      scenarios: [
        {
          scenario_id: 'multi_constraint_search',
          number: 2,
          title: 'Multi-constraint search',
          summary: 'Turkish-speaking + Friday + 15 miles.',
          fault_scenarios: ['empty_valid_result.json'],
          expected_result_kinds: ['no_slots_for_constraints'],
          assertions: ['Every constraint is hard, so no relaxation is permissible.'],
          reproducible_from_fixtures: true,
          non_fixture_dependencies: [],
        },
        {
          scenario_id: 'version_regression',
          number: 8,
          title: 'Version regression',
          summary: 'A description changes without a version bump.',
          fault_scenarios: [],
          expected_result_kinds: ['blocked'],
          assertions: ['CI fails without a version bump.'],
          reproducible_from_fixtures: false,
          non_fixture_dependencies: ['a live model provider'],
        },
      ],
    },
  };

  it('renders every assertion, quoted rather than summarised', async () => {
    const client = new StubClient(payload);
    const page = await show(new AdminScenarios(), client);

    const text = page.shadowRoot!.textContent ?? '';
    expect(text).toContain('Multi-constraint search');
    expect(text).toContain('Every constraint is hard, so no relaxation is permissible.');
    expect(text).toContain('1 assertion');
    expect(client.asked).toEqual(['scenarios']);
  });

  it('says no run is linked rather than leaving a blank column', async () => {
    // §12 asks for a trace and metric link per scenario. A blank reads as "ran, produced nothing".
    const client = new StubClient(payload);
    const page = await show(new AdminScenarios(), client);

    const text = page.shadowRoot!.textContent ?? '';
    expect(text).toContain('7.4');
    // Split across a line in the template, so the assertion matches the fragments rather than
    // depending on how the source happens to wrap.
    expect(text).toContain('A definition is not a');
    expect(text).toContain('canned success charts');
  });

  it('distinguishes "no fault injected" from "not declared", and names a scenario that needs more than fixtures', async () => {
    const client = new StubClient(payload);
    const page = await show(new AdminScenarios(), client);

    const text = page.shadowRoot!.textContent ?? '';
    expect(text).toContain('none injected');
    expect(text).toContain('a live model provider');
    expect(text).toContain('cannot be replayed offline');
  });
});

describe('Alerts', () => {
  const payload: ReadResult<Record<string, unknown>> = {
    kind: 'data',
    data: {
      available: true,
      rules: [
        {
          rule: 'terminal_failure_rate',
          threshold: 'terminal failure > 20% with at least 20 invocations in 300s',
          severity: 'page',
        },
      ],
      evaluation: { ran: false, reason: 'the scheduler is the remaining work in Phase 7.3' },
      outbox: {
        messages: [],
        counts: { pending: 0, delivered: 0, failed: 0, dead: 0 },
        sends: false,
        note: 'The mock outbox composes and records; it never sends.',
      },
      kill_switch: { engaged: false, reason: 'released (was: testing)', since: null, by: 'oncall' },
    },
  };

  it('leads with what has not happened rather than an empty list', async () => {
    // An empty alert list reads as a healthy system. This page's hardest job is not saying that.
    const page = await show(new AdminAlerts(), new StubClient(payload));

    const text = page.shadowRoot!.textContent ?? '';
    expect(text).toContain('Nothing has been evaluated');
    expect(text).toContain('Phase 7.3');
    expect(text).toContain('not a clean bill of health');
  });

  it('shows the threshold the package publishes, not a second copy', async () => {
    const page = await show(new AdminAlerts(), new StubClient(payload));

    expect(page.shadowRoot!.textContent).toContain(
      'terminal failure > 20% with at least 20 invocations in 300s',
    );
  });

  it('lists every outbox state, including the empty ones', async () => {
    const page = await show(new AdminAlerts(), new StubClient(payload));

    const text = page.shadowRoot!.textContent ?? '';
    for (const state of ['pending', 'delivered', 'failed', 'dead']) {
      expect(text, state).toContain(state);
    }
  });

  it('shows the kill switch and what it does not stop', async () => {
    const page = await show(new AdminAlerts(), new StubClient(payload));

    const text = page.shadowRoot!.textContent ?? '';
    expect(text).toContain('released');
    expect(text).toContain('booking_halted');
    expect(text).toContain('help card');
  });
});

describe('Human Queue', () => {
  const payload: ReadResult<Record<string, unknown>> = {
    kind: 'data',
    data: {
      available: true,
      tickets: [
        {
          handoff_id: 'h1',
          workflow_id: 'w1',
          ticket_ref: 'T-1',
          reason: 'no_match',
          state: 'claimed',
          claimed_by: 'operator-7',
          claim_expires_at: '2026-10-15T12:15:00+00:00',
          claimed_expired: true,
          wait_ms: 900_000,
        },
      ],
      counts: { open: 0, claimed: 1, claimed_expired: 1 },
      truncated: false,
      note: 'Claiming, releasing and resolving happen in the workers service.',
    },
  };

  it('renders the queue with each ticket its own wait', async () => {
    const client = new StubClient(payload);
    const page = await show(new AdminHumanQueue(), client);

    const text = page.shadowRoot!.textContent ?? '';
    expect(text).toContain('T-1');
    expect(text).toContain('operator-7');
    // 900s is a quarter of an hour.
    expect(text).toContain('15.0 min');
    expect(client.asked).toEqual(['human-queue']);
  });

  it('shows an expired claim as expired rather than applying it', async () => {
    // The row still reads `claimed` until something reclaims it; a queue that hid the expiry would
    // disagree with the claim that is about to succeed.
    const page = await show(new AdminHumanQueue(), new StubClient(payload));

    const text = page.shadowRoot!.textContent ?? '';
    expect(text).toContain('expired');
    expect(text).toContain('1 of those expired');
  });

  it('says where the work is done rather than offering a button', async () => {
    const page = await show(new AdminHumanQueue(), new StubClient(payload));

    expect(page.shadowRoot!.querySelectorAll('button')).toHaveLength(0);
    expect(page.shadowRoot!.textContent).toContain('workers service');
  });

  it('distinguishes an empty queue from an unreadable one', async () => {
    const empty: ReadResult<Record<string, unknown>> = {
      kind: 'data',
      data: { available: true, tickets: [], counts: { open: 0, claimed: 0 }, truncated: false, note: '' },
    };
    const page = await show(new AdminHumanQueue(), new StubClient(empty));

    expect(page.shadowRoot!.textContent).toContain('measured empty queue');
  });
});
