import { describe, expect, it } from 'vitest';
import { PatientClient, demoPatientSession } from '../src/stream/patient-client.js';
import { uuid } from './helpers.js';

const snapshot = { workflow_id: uuid(50), surface_id: uuid(60), state: 'INTAKE', state_version: 1,
  components: [], last_seq: null, last_event_id: null, terminal: null };

describe('patient session and protected HTTP operations', () => {
  it('preserves the Window receiver required by native browser fetch', async () => {
    const client = new PatientClient('http://gateway', 'token', async function (this: unknown) {
      expect(this).toBe(globalThis);
      return new Response(JSON.stringify(snapshot));
    });
    await client.snapshot(uuid(50));
  });
  it('uses the same bearer credential for the snapshot and card actions', async () => {
    const calls: { url: string; init: RequestInit | undefined }[] = [];
    const client = new PatientClient('http://gateway/', 'patient-token', async (url, init) => {
      calls.push({ url: String(url), init });
      return new Response(JSON.stringify(calls.length === 1 ? snapshot : { recorded: 'answer' }));
    });
    expect(await client.snapshot(uuid(50))).toEqual(snapshot);
    const action = { command_id: uuid(1), instance_id: uuid(2), action: 'system:await_patient_choice', workflow_state_version: 1 };
    await client.sendAction(uuid(50), action);
    for (const call of calls) expect(call.init?.headers).toMatchObject({ authorization: 'Bearer patient-token' });
    expect(calls[1]!.init?.body).toBe(JSON.stringify(action));
  });

  it('keeps server refusal details and rejects a snapshot for another patient workflow', async () => {
    const refused = new PatientClient('http://gateway', 'token', async () => new Response(JSON.stringify({ error: { message: 'Session expired' } }), { status: 401 }));
    await expect(refused.snapshot(uuid(50))).rejects.toThrow('Session expired');
    const wrong = new PatientClient('http://gateway', 'token', async () => new Response(JSON.stringify({ ...snapshot, workflow_id: uuid(51) })));
    await expect(wrong.snapshot(uuid(50))).rejects.toThrow('another workflow');
  });

  it('requires a labeled fixture session and sends the entered patient reference', async () => {
    let body: unknown;
    const token = await demoPatientSession('http://gateway/', 'patient-1', async (_url, init) => {
      body = JSON.parse(String(init?.body));
      return new Response(JSON.stringify({ access_token: 'fixture-token', fixture: true }));
    });
    expect(body).toEqual({ subject: 'patient-1', patient_ref: 'patient-1' });
    expect(token).toBe('fixture-token');
    await expect(demoPatientSession('http://gateway', 'patient-1', async () => new Response(JSON.stringify({ access_token: 'not-labeled' })))).rejects.toThrow();
  });
});
