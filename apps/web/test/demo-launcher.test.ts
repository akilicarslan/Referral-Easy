import { afterEach, describe, expect, it } from 'vitest';
import { demoLauncher } from '../src/app/demo-launcher.js';
import { eventually } from './helpers.js';

const id = 'c0000000-0000-4000-8000-000000000001';
const patient = 'b0000000-0000-4000-8000-000000000001';
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
afterEach(() => { document.body.replaceChildren(); window.history.replaceState({}, '', '/'); });

describe('demo referral creation', () => {
  it('seeds with an admin session, dispatches with a patient session, then opens the saved workflow', async () => {
    const calls: { path: string; body: Record<string, unknown>; token: string | undefined }[] = [];
    let opened: unknown;
    const fetchImpl: typeof fetch = async (url, init) => {
      const path = String(url);
      calls.push({ path, body: JSON.parse(String(init?.body)), token: (init?.headers as Record<string, string>)['authorization'] });
      if (path.endsWith('admin-session')) return json({ access_token: 'admin', fixture: true });
      if (path.endsWith('demo/workflows')) return json({ workflow_id: id, state_version: 1, fixture: true });
      if (path.endsWith('patient-session')) return json({ access_token: 'patient', fixture: true });
      return json({ committed: true, dispatched: true });
    };
    const launcher = demoLauncher('http://gateway', fetchImpl, async (...args) => { opened = args; });
    document.body.append(launcher);
    launcher.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }));
    await eventually(() => opened !== undefined, 'the referral to open');
    expect(opened).toEqual([id, 'patient', patient]);
    expect(calls[1]?.token).toBe('Bearer admin');
    expect(calls[1]?.body['patient_ref']).toBe(patient);
    expect(calls[3]?.token).toBe('Bearer patient');
    expect(calls[3]?.body['text']).toBe('Schedule a cardiology consultation.');
  });

  it('retries a failed initial dispatch using the saved workflow and original command', async () => {
    let seeds = 0;
    const messages: unknown[] = [];
    let opened = false;
    const fetchImpl: typeof fetch = async (url, init) => {
      const path = String(url);
      if (path.endsWith('admin-session')) return json({ access_token: 'admin', fixture: true });
      if (path.endsWith('demo/workflows')) { seeds++; return json({ workflow_id: id, state_version: 1, fixture: true }); }
      if (path.endsWith('patient-session')) return json({ access_token: 'patient', fixture: true });
      messages.push(JSON.parse(String(init?.body)));
      if (messages.length === 1) throw new Error('Connection interrupted');
      return json({ committed: true, dispatched: true });
    };
    const launcher = demoLauncher('', fetchImpl, async () => { opened = true; });
    document.body.append(launcher);
    const form = launcher.querySelector('form')!;
    form.dispatchEvent(new Event('submit', { cancelable: true }));
    await eventually(() => launcher.textContent!.includes('Your referral is saved'), 'the saved-referral recovery');
    expect(opened).toBe(false);
    form.dispatchEvent(new Event('submit', { cancelable: true }));
    await eventually(() => opened, 'the retry to open the referral');
    expect(seeds).toBe(1);
    expect(messages[0]).toEqual(messages[1]);
  });

  it('does not open a referral when the initial request is refused', async () => {
    let opened = false;
    const fetchImpl: typeof fetch = async (url) => {
      if (String(url).endsWith('admin-session')) return json({ error: { message: 'Demo profile is disabled' } }, 404);
      throw new Error('must not seed');
    };
    const launcher = demoLauncher('', fetchImpl, async () => { opened = true; });
    document.body.append(launcher);
    launcher.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }));
    await eventually(() => launcher.textContent!.includes('Demo profile is disabled'), 'the gateway refusal');
    expect(opened).toBe(false);
    expect(launcher.querySelector('button')!.disabled).toBe(false);
  });
});
