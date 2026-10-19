import { afterEach, describe, expect, it } from 'vitest';

import { AdminClient, type AdminFilters, type ReadResult } from '../src/admin/api.js';
import { AdminShell } from '../src/admin/shell.js';

/**
 * The shell, driven through the DOM.
 *
 * These assert the two things a person sees first — that the nav reaches all nine pages, and that a
 * hash naming nothing is answered rather than ignored — plus the wiring that is easy to get wrong:
 * the filters the shell owns have to reach the page element it creates, or every page reads the
 * default window while the controls say otherwise.
 */

/** A client whose read is scripted, so no fetch and no gateway are involved. */
class FakeClient extends AdminClient {
  readonly seen: { page: string; filters: AdminFilters }[] = [];

  constructor(
    private readonly answer: ReadResult<Record<string, unknown>> = {
      kind: 'unavailable',
      reason: 'no cluster',
    },
  ) {
    super('http://gateway', 'coordinator@demo', (() => Promise.reject(new Error('unused'))) as never);
  }

  override read(page: string, filters: AdminFilters): Promise<ReadResult<Record<string, unknown>>> {
    this.seen.push({ page, filters });
    return Promise.resolve(this.answer);
  }
}

const shells: AdminShell[] = [];

function mount(hash: string, client: FakeClient = new FakeClient()): AdminShell {
  window.location.hash = hash;
  const shell = new AdminShell();
  shell.client = client;
  document.body.append(shell);
  shells.push(shell);
  return shell;
}

afterEach(() => {
  for (const shell of shells.splice(0)) {
    shell.remove();
  }
  window.location.hash = '';
});

describe('the shell', () => {
  it('lists all nine pages in the nav', async () => {
    const shell = mount('#/overview');
    await shell.updateComplete;

    const links = [...shell.shadowRoot!.querySelectorAll('.nav a')].map((node) =>
      node.textContent?.trim(),
    );
    expect(links).toEqual([
      'Overview',
      'Agents & Versions',
      'Trace Explorer',
      'Evaluations',
      'Guardrails',
      'Feedback',
      'Fault Scenarios',
      'Human Queue',
      'Alerts',
    ]);
  });

  it('marks the current page, so the nav is not just a list of links', async () => {
    const shell = mount('#/feedback');
    await shell.updateComplete;

    const current = shell.shadowRoot!.querySelector('.nav a[aria-current="page"]');
    expect(current?.textContent?.trim()).toBe('Feedback');
  });

  it('answers an unknown hash with the pages it does have', async () => {
    // Not the overview. A wrong address that renders a nearby page reports success for a page
    // nobody built, which is exactly what a stale bookmark would do.
    const shell = mount('#/not-a-page');
    await shell.updateComplete;

    const text = shell.shadowRoot!.textContent ?? '';
    expect(text).toContain('No such page');
    expect(text).toContain('#/not-a-page');
    expect(shell.shadowRoot!.querySelectorAll('.nav a').length).toBe(9);
  });

  it('lands on the overview when no hash names a page', async () => {
    const shell = mount('');
    await shell.updateComplete;

    expect(shell.shadowRoot!.textContent).toContain('Overview');
    expect(shell.shadowRoot!.querySelector('.nav a[aria-current="page"]')?.textContent?.trim()).toBe(
      'Overview',
    );
  });

  it('hands the page element the filters it owns', async () => {
    // The wiring that silently breaks: the shell renders the controls, the page reads the window,
    // and if the property never crosses the boundary the controls change nothing.
    const client = new FakeClient();
    const shell = mount('#/overview', client);
    await shell.updateComplete;

    const page = shell.shadowRoot!.querySelector('re-admin-overview') as
      | (HTMLElement & { filters?: AdminFilters })
      | null;
    expect(page).not.toBeNull();
    expect(page?.filters?.range).toBe('day');
    expect(page?.filters?.timezone).toBe('America/Chicago');
  });

  it('replaces the filters object when a control changes, so the page reloads', async () => {
    const client = new FakeClient();
    const shell = mount('#/overview', client);
    await shell.updateComplete;

    const select = shell.shadowRoot!.querySelectorAll('select')[0] as HTMLSelectElement;
    select.value = 'month';
    select.dispatchEvent(new Event('change'));
    await shell.updateComplete;

    const page = shell.shadowRoot!.querySelector('re-admin-overview') as
      | (HTMLElement & { filters?: AdminFilters })
      | null;
    // A new object, not a mutation: `AdminPage.updated` compares by identity and an in-place change
    // would leave every page believing nothing had moved.
    expect(page?.filters?.range).toBe('month');
  });

  it('offers the zones the telemetry query accepts', async () => {
    const shell = mount('#/overview');
    await shell.updateComplete;

    const zone = shell.shadowRoot!.querySelectorAll('select')[1] as HTMLSelectElement;
    const values = [...zone.options].map((option) => option.value);
    expect(values).toContain('America/Chicago');
    expect(values).toContain('UTC');
    // The default is first, so the common case needs no interaction.
    expect(values[0]).toBe('America/Chicago');
  });

  it('has a custom range with no bounds by default, and shows the bound inputs only then', async () => {
    const shell = mount('#/overview');
    await shell.updateComplete;
    expect(shell.shadowRoot!.querySelectorAll('input[type="datetime-local"]')).toHaveLength(0);

    const range = shell.shadowRoot!.querySelectorAll('select')[0] as HTMLSelectElement;
    range.value = 'custom';
    range.dispatchEvent(new Event('change'));
    await shell.updateComplete;

    expect(shell.shadowRoot!.querySelectorAll('input[type="datetime-local"]')).toHaveLength(2);
  });
});
