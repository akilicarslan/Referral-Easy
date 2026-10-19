import { demoPatientSession } from '../stream/patient-client.js';

const PATIENT = 'b0000000-0000-4000-8000-000000000001';

/** The demo issuer and seed route remain server-gated; no credentials are embedded here. */
export function demoLauncher(baseUrl: string, fetchImpl: typeof fetch, open: (id: string, token: string, patient: string) => Promise<void>): HTMLElement {
  const section = document.createElement('section');
  section.className = 'demo-launcher';
  section.innerHTML = `
    <div class="intro"><span class="eyebrow">YOUR NEXT STEP IN CARE</span><h1>A referral.<br>A clear way forward.</h1><p>Start with a sample patient. We’ll help you find a provider, check coverage, and review an appointment request.</p><div class="intro-note"><span aria-hidden="true">↗</span><div><strong>You stay in control</strong><p>Choose your provider and approve the exact request before anything is booked.</p></div></div></div>
    <div class="launcher-card"><div class="card-heading"><div><span class="eyebrow">LET’S GET STARTED</span><h2>Create a demo referral</h2></div><span class="re-chip re-chip--synthetic">Synthetic data</span></div>
    <form class="create-form"><label>Sample patient<select name="sample"><option value="${PATIENT}">Demo patient 01 · Cardiology</option><option value="b0000000-0000-4000-8000-000000000003">Demo patient 03 · Cardiology, Spanish preference</option><option value="b0000000-0000-4000-8000-000000000004">Demo patient 04 · Neurology, Turkish preference</option></select></label>
    <div class="sample-summary"><span class="sample-avatar" aria-hidden="true">DE</span><div><strong>Referral records included</strong><p>Sample referral, coverage details, and care preferences.</p></div></div>
    <label>What would you like help with?<textarea name="request" rows="3" maxlength="2000" required>Schedule a cardiology consultation.</textarea></label><p class="form-hint">Demo records only. Please don’t enter real patient information.</p>
    <button class="re-btn re-btn--primary re-btn--block" type="submit">Create referral <span aria-hidden="true">→</span></button><p class="form-status" role="status" aria-live="polite"></p></form>
    <details class="existing-referral"><summary>Open a demo referral with an existing ID</summary><form class="open-form"><p>Use the workflow ID and matching patient reference from your demo.</p><label>Workflow ID<input name="workflow_id" required pattern="[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}" autocomplete="off"></label><label>Patient reference<input name="patient_ref" required autocomplete="off"></label><button class="re-btn re-btn--secondary" type="submit">Open demo session</button><p class="form-status" role="status" aria-live="polite"></p></form></details>
    <p class="launcher-foot">Local demo · No real appointments or patient records</p></div>`;
  const create = section.querySelector<HTMLFormElement>('.create-form')!;
  const sample = create.elements.namedItem('sample') as HTMLSelectElement;
  const request = create.elements.namedItem('request') as HTMLTextAreaElement;
  request.addEventListener('input', () => request.setCustomValidity(''));
  sample.addEventListener('change', () => { request.value = sample.value.endsWith('004') ? 'Schedule a neurology consultation.' : 'Schedule a cardiology consultation.'; });
  // Retain a created workflow and command across a failed initial dispatch: retry the same intent.
  let draft: { id: string; patient: string; version: number; command: string; text: string; token?: string } | undefined;
  create.addEventListener('submit', (event) => {
    event.preventDefault();
    if (!request.value.trim()) {
      request.setCustomValidity('Enter a short referral request.');
      request.reportValidity();
      return;
    }
    const button = create.querySelector('button')!;
    if (button.disabled) return;
    button.disabled = true;
    const status = create.querySelector<HTMLElement>('[role="status"]')!;
    status.textContent = draft ? 'Resuming your referral…' : 'Creating your referral…';
    void (async () => {
      try {
        if (!draft) {
          const admin = await post('/v1/auth/admin-session', { subject: 'demo-referral-create' });
          if (admin.fixture !== true || typeof admin.access_token !== 'string') throw new Error('The gateway did not return a demo session.');
          const workflow = await post('/v1/demo/workflows', { patient_ref: sample.value, ticket_ref: `WEB-DEMO-${crypto.randomUUID()}` }, admin.access_token);
          if (typeof workflow.workflow_id !== 'string' || typeof workflow.state_version !== 'number' || workflow.fixture !== true) throw new Error('The gateway did not return a demo referral.');
          draft = { id: workflow.workflow_id, patient: sample.value, version: workflow.state_version, command: crypto.randomUUID(), text: request.value.trim() };
          sample.disabled = true; request.disabled = true;
        }
        draft.token ??= await demoPatientSession(baseUrl, draft.patient, fetchImpl);
        const receipt = await post(`/v1/workflows/${encodeURIComponent(draft.id)}/messages`, { command_id: draft.command, workflow_state_version: draft.version, text: draft.text }, draft.token);
        if (receipt.committed !== true || receipt.dispatched !== true) throw new Error('The referral was created, but coordination has not started. Retry to check the same request.');
        await open(draft.id, draft.token, draft.patient);
        status.textContent = '';
      } catch (error) {
        status.textContent = `${error instanceof Error ? error.message : String(error)}${draft ? ' Your referral is saved. Retry uses the same referral and request.' : ' Check that the demo gateway is running, then try again.'}`;
        button.textContent = draft ? 'Retry starting referral' : 'Try creating referral again';
      } finally { button.disabled = false; }
    })();
  });
  const form = section.querySelector<HTMLFormElement>('.open-form')!;
  const workflow = form.elements.namedItem('workflow_id') as HTMLInputElement;
  const patient = form.elements.namedItem('patient_ref') as HTMLInputElement;
  const params = new URLSearchParams(window.location.search);
  workflow.value = params.get('workflow_id') ?? window.location.hash.replace(/^#\/?/, '');
  patient.value = params.get('patient_ref') ?? '';
  if (workflow.value) section.querySelector('details')!.open = true;
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const button = form.querySelector('button')!;
    if (button.disabled) return;
    button.disabled = true;
    const status = form.querySelector<HTMLElement>('[role="status"]')!;
    status.textContent = 'Opening demo session…';
    void (async () => {
      try {
        const token = await demoPatientSession(baseUrl, patient.value.trim(), fetchImpl);
        await open(workflow.value.trim(), token, patient.value.trim());
        status.textContent = '';
      } catch (error) { status.textContent = error instanceof Error ? error.message : String(error); }
      finally { button.disabled = false; }
    })();
  });
  return section;

  async function post(path: string, body: unknown, token?: string): Promise<Record<string, unknown>> {
    const response = await fetchImpl(`${baseUrl.replace(/\/+$/, '')}${path}`, { method: 'POST', headers: { 'content-type': 'application/json', ...(token ? { authorization: `Bearer ${token}` } : {}) }, body: JSON.stringify(body) });
    const result = await response.json() as Record<string, unknown>;
    if (!response.ok) {
      const error = result['error'] as { message?: string } | undefined;
      throw new Error(error?.message ?? `The demo gateway returned ${response.status}.`);
    }
    return result;
  }
}
