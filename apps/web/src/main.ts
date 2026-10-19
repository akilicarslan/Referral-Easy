import './styles/tokens.css';
import './styles/base.css';
import './app/referral-app.js';

import { SurfaceRuntime } from './a2ui/adapter.js';
import { SurfaceStore } from './stream/surface-store.js';
import { SseClient, type StreamTransport } from './stream/sse-client.js';
import { PatientClient, demoPatientSession } from './stream/patient-client.js';
import { ReferralApp } from './app/referral-app.js';

const GATEWAY_BASE = (import.meta.env['VITE_GATEWAY_URL'] as string | undefined) ?? window.location.origin;
let disposePrevious: (() => void) | undefined;

export interface BootOptions {
  readonly token?: string;
  readonly fetchImpl?: typeof fetch;
  readonly transport?: StreamTransport;
}

/** One lifecycle owns the session, snapshot, stream, store and renderer. */
export function boot(options: BootOptions = {}): () => void {
  disposePrevious?.();
  const fetchImpl = options.fetchImpl ?? fetch;
  let active = true;
  let connectionGeneration = 0;
  let client: SseClient | undefined;
  let patient: PatientClient | undefined;
  const app = new ReferralApp();
  const runtime = new SurfaceRuntime({
    onDiagnostic: (diagnostic) => store.addDiagnostic(diagnostic),
    onAction: (action) => store.submit(action),
  });
  const store = new SurfaceStore({
    runtime,
    onChange: (snapshot) => { app.snapshot = snapshot; },
    sendAction: async (action) => {
      const workflowId = store.snapshot.workflowId;
      if (patient === undefined || workflowId === undefined) throw new Error('No patient session is active.');
      await patient.sendAction(workflowId, action);
    },
  });
  app.runtime = runtime;
  app.store = store;
  app.snapshot = store.snapshot;
  const root = document.getElementById('app');
  if (root === null) throw new Error('Missing #app root element.');
  // Replaces the pre-bundle skeleton as well as any previous app instance.
  root.replaceChildren(app);

  async function connect(workflowId: string, token: string): Promise<void> {
    const generation = ++connectionGeneration;
    app.hidden = false;
    client?.disconnect();
    patient = new PatientClient(GATEWAY_BASE, token, fetchImpl);
    store.beginWorkflow(workflowId);
    try {
      const snapshot = await patient.snapshot(workflowId);
      if (!active || generation !== connectionGeneration) return;
      store.hydrate(snapshot);
      if (snapshot.terminal !== null) { app.streamStatus = 'closed'; return; }
      client = new SseClient({
        baseUrl: GATEWAY_BASE, token,
        ...(options.transport === undefined ? {} : { transport: options.transport }),
        onEvent: (event) => {
          store.apply(event);
          if (event.type === 'terminal') client?.disconnect();
        },
        onStatus: (status) => {
          app.streamStatus = status;
          if (status === 'open') store.setLive();
          else if (status === 'closed') store.setInterrupted();
          else if (status === 'connecting' || status === 'reconnecting') store.setConnecting();
        },
        onDiagnostic: (diagnostic) => store.addDiagnostic(diagnostic),
      });
      client.connect(workflowId, { seq: snapshot.last_seq ?? -1, eventId: snapshot.last_event_id ?? undefined });
    } catch (error) {
      if (active && generation === connectionGeneration) store.setError(`Could not load this referral: ${describe(error)}`);
    }
  }

  const workflowId = readWorkflowId();
  const token = options.token ?? storedToken(workflowId);
  if (workflowId === undefined) {
    app.hidden = true;
  } else if (token === undefined) {
    app.hidden = true;
  } else {
    void connect(workflowId, token);
  }

  // The token issuer exists only in demo/test/CI. Never infer a patient's identity from a workflow.
  const form = document.createElement('form');
  form.style.cssText = 'max-width:560px;margin:16px auto;padding:16px;display:grid;gap:12px';
  form.innerHTML = `<h2>Open a demo referral</h2>
    <p>Synthetic data only. Use the workflow ID and patient reference from a seeded demo. This session does not verify a real identity.</p>
    <label>Workflow ID <input name="workflow_id" required pattern="[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}" autocomplete="off"></label>
    <label>Patient reference <input name="patient_ref" required autocomplete="off"></label>
    <button type="submit">Open demo session</button>
    <p role="status"></p>
    <p><a href="/demo.html">Component gallery</a> · <a href="/admin.html">Admin dashboard</a></p>`;
  const workflowInput = form.elements.namedItem('workflow_id') as HTMLInputElement;
  const patientInput = form.elements.namedItem('patient_ref') as HTMLInputElement;
  workflowInput.value = workflowId ?? '';
  patientInput.value = new URLSearchParams(window.location.search).get('patient_ref') ?? '';
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const button = form.querySelector('button')!;
    button.disabled = true;
    const status = form.querySelector('[role="status"]')!;
    status.textContent = 'Opening demo session…';
    void (async () => {
      try {
        const id = workflowInput.value.trim();
        const session = await demoPatientSession(GATEWAY_BASE, patientInput.value.trim(), fetchImpl);
        if (!active) return;
        try { sessionStorage.setItem(`referraleasy.patient.${id}`, session); } catch { /* Storage can be disabled. */ }
        const url = new URL(window.location.href);
        url.searchParams.set('workflow_id', id);
        url.hash = '';
        window.history.replaceState({}, '', url);
        await connect(id, session);
        status.textContent = '';
      } catch (error) {
        if (active) status.textContent = describe(error);
      } finally { button.disabled = false; }
    })();
  });
  root.append(form);

  app.addEventListener('re-retry', () => {
    const id = store.snapshot.workflowId ?? workflowId;
    if (id !== undefined && patient !== undefined) void connect(id, patient.token);
  });
  const visibilityChanged = () => {
    if (document.visibilityState === 'visible' && client?.status === 'closed' && store.snapshot.terminal === undefined) client.replay();
  };
  document.addEventListener('visibilitychange', visibilityChanged);
  const dispose = () => {
    active = false;
    client?.disconnect();
    document.removeEventListener('visibilitychange', visibilityChanged);
    runtime.dispose();
  };
  disposePrevious = dispose;
  return dispose;
}

function readWorkflowId(): string | undefined {
  const fromQuery = new URLSearchParams(window.location.search).get('workflow_id');
  if (fromQuery) return fromQuery;
  const fromHash = window.location.hash.replace(/^#\/?/, '');
  return fromHash.length > 0 ? fromHash : undefined;
}

function storedToken(workflowId: string | undefined): string | undefined {
  if (workflowId === undefined) return undefined;
  try { return sessionStorage.getItem(`referraleasy.patient.${workflowId}`) ?? undefined; }
  catch { return undefined; }
}

function describe(error: unknown): string { return error instanceof Error ? error.message : String(error); }

function run(): void {
  try { boot(); }
  catch (error) {
    const host = document.getElementById('app') ?? document.body;
    const box = document.createElement('section');
    box.setAttribute('role', 'alert');
    const heading = document.createElement('h1');
    heading.textContent = 'This page could not start';
    const detail = document.createElement('p');
    detail.textContent = `The client could not start. Nothing was submitted from this screen. (${describe(error)})`;
    box.append(heading, detail);
    host.replaceChildren(box);
  }
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, { once: true });
else run();
