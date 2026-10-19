import { z } from 'zod-v3';
import { sse$ComponentInstance, sse$StreamEvent } from '@referraleasy/contracts';
import type { ClientActionShape } from './surface-store.js';

const snapshotSchema = z.object({
  workflow_id: z.string().uuid(),
  surface_id: z.string().uuid(),
  state: z.string().min(1),
  state_version: z.number().int().nonnegative(),
  components: z.array(z.unknown()),
  last_event_id: z.string().nullable(),
  last_seq: z.number().int().nonnegative().nullable(),
  terminal: z.unknown(),
});

export type WorkflowSnapshot = Omit<z.infer<typeof snapshotSchema>, 'components' | 'terminal'> & {
  components: ReturnType<typeof sse$ComponentInstance.parse>[];
  terminal: ReturnType<typeof sse$StreamEvent.parse> | null;
};

export class PatientRequestError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

/** Patient HTTP operations share one credential and one error handling path. */
export class PatientClient {
  constructor(
    private readonly baseUrl: string,
    readonly token: string,
    private readonly fetchImpl: typeof fetch = fetch,
  ) { this.fetchImpl = fetchImpl.bind(globalThis); }

  async snapshot(workflowId: string): Promise<WorkflowSnapshot> {
    const snapshot = snapshotSchema.parse(await this.#request(`/v1/workflows/${encodeURIComponent(workflowId)}`));
    if (snapshot.workflow_id !== workflowId) throw new Error('The gateway returned another workflow.');
    // Contracts use Zod 4; this app also ships Zod 3 for the A2UI adapter. Parse at each seam
    // instead of composing validators from incompatible Zod versions.
    return { ...snapshot, components: snapshot.components.map((item) => sse$ComponentInstance.parse(item)),
      terminal: snapshot.terminal === null ? null : sse$StreamEvent.parse(snapshot.terminal) };
  }

  async sendAction(workflowId: string, action: ClientActionShape): Promise<void> {
    await this.#request(`/v1/workflows/${encodeURIComponent(workflowId)}/actions`, {
      method: 'POST', body: JSON.stringify(action),
    });
  }

  async #request(path: string, init: RequestInit = {}): Promise<unknown> {
    const response = await this.fetchImpl(`${this.baseUrl.replace(/\/+$/, '')}${path}`, {
      ...init,
      headers: { 'content-type': 'application/json', authorization: `Bearer ${this.token}` },
    });
    const body: unknown = await response.json();
    if (!response.ok) throw new PatientRequestError(refusal(body, response.status), response.status);
    return body;
  }
}

/** Available only on the gateway's explicitly synthetic demo profile. */
export async function demoPatientSession(baseUrl: string, patientRef: string, fetchImpl: typeof fetch = fetch): Promise<string> {
  const response = await fetchImpl(`${baseUrl.replace(/\/+$/, '')}/v1/auth/patient-session`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ subject: patientRef, patient_ref: patientRef }),
  });
  const body: unknown = await response.json();
  if (!response.ok) throw new Error(refusal(body, response.status));
  const session = z.object({ access_token: z.string().min(1), fixture: z.literal(true) }).parse(body);
  return session.access_token;
}

function refusal(body: unknown, status: number): string {
  const parsed = z.object({ error: z.object({ message: z.string() }) }).safeParse(body);
  return parsed.success ? parsed.data.error.message : `Gateway returned ${status}.`;
}
