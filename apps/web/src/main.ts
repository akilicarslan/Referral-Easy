import './styles/tokens.css';
import './styles/base.css';
import './app/referral-app.js';

import { SurfaceRuntime } from './a2ui/adapter.js';
import { SurfaceStore } from './stream/surface-store.js';
import { SseClient, type StreamTransport } from './stream/sse-client.js';
import { PatientClient } from './stream/patient-client.js';
import { ReferralApp } from './app/referral-app.js';
import { demoLauncher } from './app/demo-launcher.js';
import './styles/workspace.css';

const GATEWAY_BASE = (import.meta.env['VITE_GATEWAY_URL'] as string | undefined)?.trim() || window.location.origin;
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
  let refreshTimer: ReturnType<typeof setTimeout> | undefined;
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

  function refreshUnverified(workflowId: string, generation: number): void {
    if (!active || generation !== connectionGeneration || store.snapshot.workflow?.state !== 'BOOKING_UNVERIFIED') return;
    if (refreshTimer !== undefined) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(async () => {
      refreshTimer = undefined;
      try {
        const snapshot = await patient!.snapshot(workflowId);
        if (!active || generation !== connectionGeneration) return;
        if ((snapshot.last_seq ?? -1) > store.snapshot.seq
          || snapshot.state_version > (store.snapshot.workflow?.stateVersion ?? -1)) {
          store.hydrate(snapshot);
        }
      } catch (error) {
        if (active && generation === connectionGeneration) store.addDiagnostic({
          code: 'BOOKING_STATUS_REFRESH_FAILED',
          message: `Could not refresh the booking status: ${describe(error)}`,
        });
      }
      refreshUnverified(workflowId, generation);
    }, 1000);
  }

  async function connect(workflowId: string, token: string): Promise<void> {
    const generation = ++connectionGeneration;
    if (refreshTimer !== undefined) clearTimeout(refreshTimer);
    app.hidden = false;
    launcher.hidden = true;
    workspace.hidden = false;
    client?.disconnect();
    patient = new PatientClient(GATEWAY_BASE, token, fetchImpl);
    store.beginWorkflow(workflowId);
    try {
      const snapshot = await patient.snapshot(workflowId);
      if (!active || generation !== connectionGeneration) return;
      store.hydrate(snapshot);
      if (snapshot.terminal !== null) {
        app.streamStatus = 'closed';
        refreshUnverified(workflowId, generation);
        return;
      }
      client = new SseClient({
        baseUrl: GATEWAY_BASE, token,
        ...(options.transport === undefined ? {} : { transport: options.transport }),
        onEvent: (event) => {
          store.apply(event);
          if (event.type === 'terminal') {
            client?.disconnect();
            refreshUnverified(workflowId, generation);
          }
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
      if (active && generation === connectionGeneration) {
        store.setError(`Could not load this referral: ${describe(error)}`);
        launcher.hidden = false;
      }
    }
  }

  const header = document.createElement('header');
  header.className = 'site-header';
  header.innerHTML = `<a class="wordmark" href="/"><span class="brand-mark" aria-hidden="true">↗</span>ReferralEasy<span class="brand-context">Care, connected.</span></a><nav aria-label="Main navigation"><a href="/" aria-current="page">Referrals</a><a href="/demo.html">Component gallery</a><a href="/admin.html">Admin dashboard <span aria-hidden="true">↗</span></a></nav>`;
  const workspace = document.createElement('main');
  workspace.className = 'referral-workspace';
  workspace.hidden = true;
  workspace.innerHTML = `<aside class="journey-panel"><span class="eyebrow">YOUR REFERRAL</span><h1>One step closer<br>to your care.</h1><p>Your care team coordinates the details. You make the choices.</p><ol><li><strong>Find your care</strong><span>Coverage and provider options</span></li><li><strong>Prepare your visit</strong><span>Details and required documents</span></li><li><strong>Review and approve</strong><span>Your request, with your consent</span></li><li><strong>Track the outcome</strong><span>Confirmation or next steps</span></li></ol><div class="journey-note">Synthetic demo<br><span>No real appointment will be made.</span></div><a href="/">＋ Create another referral</a></aside><section class="referral-content" aria-label="Referral updates"><div class="workspace-heading"><span class="eyebrow">CARE COORDINATION</span><span class="re-chip re-chip--synthetic">Synthetic demo</span></div></section>`;
  workspace.querySelector('.referral-content')!.append(app);
  const launcher = demoLauncher(GATEWAY_BASE, fetchImpl, async (id, session, patientRef) => {
    if (!active) return;
    try { sessionStorage.setItem(`referraleasy.patient.${id}`, session); } catch { /* Storage can be disabled. */ }
    const url = new URL(window.location.href);
    url.searchParams.set('workflow_id', id);
    url.searchParams.set('patient_ref', patientRef);
    url.hash = '';
    window.history.replaceState({}, '', url);
    await connect(id, session);
  });
  root.replaceChildren(header, launcher, workspace);
  const workflowId = readWorkflowId();
  const token = options.token ?? storedToken(workflowId);
  app.hidden = true;
  if (workflowId !== undefined && token !== undefined) void connect(workflowId, token);

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
    if (refreshTimer !== undefined) clearTimeout(refreshTimer);
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
