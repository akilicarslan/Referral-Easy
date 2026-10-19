/**
 * Execute with Playwright MCP browser_run_code_unsafe(filename=absolute path).
 * Requires the updated gateway on 18080, Vite on 3001, and an isolated mock API on 4011.
 * The test scripts coordinator/scheduling decisions. The gateway uses its configured guardrail backend.
 */
async (page) => {
  const gateway = 'http://127.0.0.1:18080';
  const mock = 'http://127.0.0.1:4011';
  const web = 'http://127.0.0.1:3001';
  const api = page.request;
  const json = async (response, expected = 200) => {
    if (response.status() !== expected) throw new Error(`${response.status()}: ${await response.text()}`);
    return response.json();
  };
  const admin = await json(await api.post(`${gateway}/v1/auth/admin-session`, { data: { subject: 'browser-scenario-driver' } }));
  const headers = { authorization: `Bearer ${admin.access_token}` };
  const results = [];

  const discover = async (adapter) => {
    const hospitals = (await json(await api.get(`${mock}/hospitals?adapter_type=${adapter}`))).hospitals;
    for (const hospital of hospitals) {
      const providers = (await json(await api.get(`${mock}/providers?hospital_id=${hospital.hospital_id}`))).providers;
      for (const provider of providers) {
        const availability = await json(await api.get(`${mock}/availability?hospital_id=${hospital.hospital_id}&provider_id=${provider.provider_id}&visit_type=first_visit`));
        const slot = availability.slots?.find((item) => item.status === 'available');
        if (slot) return { hospital, provider, slot };
      }
    }
    throw new Error(`No fresh ${adapter} slot; restart the isolated mock API with a new temporary database.`);
  };

  for (const adapter of ['instant_confirm', 'review_required']) {
    const { hospital, provider, slot } = await discover(adapter);
    const patient = 'b0000000-0000-4000-8000-000000000001';
    const seeded = await json(await api.post(`${gateway}/v1/demo/workflows`, { headers, data: {
      patient_ref: patient, ticket_ref: `T-BROWSER-${adapter}-${crypto.randomUUID()}`, state: 'INTAKE',
    } }));
    const id = seeded.workflow_id;
    const path = `${gateway}/v1/workflows/${id}`;
    const move = async (version, state, props = {}) => json(await api.post(`${path}/transitions`, { headers, data: {
      command_id: crypto.randomUUID(), expected_state_version: version, to_state: state, props,
    } }));
    const act = async (action) => {
      const [response] = await Promise.all([
        page.waitForResponse((r) => r.url().endsWith(`/v1/workflows/${id}/actions`) && r.request().method() === 'POST', { timeout: 5000 }),
        action(),
      ]);
      return json(response);
    };
    await move(1, 'PROVIDER_MATCHING');
    await move(2, 'AWAITING_HOSPITAL_CHOICE', { options: [{ id: hospital.hospital_id, name: hospital.name }] });
    await page.goto(`${web}/?workflow_id=${id}`);
    await page.getByRole('textbox', { name: 'Patient reference' }).fill(patient);
    await page.getByRole('button', { name: 'Open demo session' }).click();
    await page.getByRole('radio').waitFor({ timeout: 5000 });
    if (await page.getByRole('radio').isChecked()) throw new Error('Hospital was preselected');
    // Reload must restore the protected snapshot and its current question.
    await page.reload();
    await act(() => page.getByRole('radio').check());
    await move(3, 'AWAITING_DOCTOR_CHOICE', { hospital_id: hospital.hospital_id, hospital_name: hospital.name,
      options: [{ id: provider.provider_id, name: provider.display_name, specialty: provider.specialty }] });
    await page.getByRole('heading', { name: 'Choose a doctor' }).waitFor({ timeout: 5000 });
    if (await page.getByRole('radio').count() !== 1) throw new Error('Superseded hospital was re-added');
    await act(() => page.getByRole('radio').check());
    await move(4, 'AWAITING_SLOT_CHOICE', { provider_name: provider.display_name, display_timezone: slot.display_timezone,
      options: [{ id: slot.slot_id, start_at: slot.start_at, end_at: slot.end_at, timezone: slot.display_timezone }] });
    await page.getByRole('heading', { name: 'Choose a time' }).waitFor({ timeout: 5000 });
    await act(() => page.getByRole('radio').check());
    const payload = { patient_id: patient, hospital_id: hospital.hospital_id, provider_id: provider.provider_id,
      slot_id: slot.slot_id, visit_type: 'first_visit' };
    const review = await move(5, 'AWAITING_FINAL_APPROVAL', { booking_payload: payload });
    await page.getByRole('checkbox').waitFor({ timeout: 5000 });
    if (await page.getByRole('button', { name: 'Send referral request' }).isEnabled()) throw new Error('Consent was bypassed');
    const patientSession = await json(await api.post(`${gateway}/v1/auth/patient-session`, { data: { subject: patient, patient_ref: patient } }));
    const patientHeaders = { authorization: `Bearer ${patientSession.access_token}` };
    const denied = await api.post(`${path}/actions`, { headers: patientHeaders, data: {
      command_id: crypto.randomUUID(), instance_id: review.components[0].instance_id,
      action: 'system:capture_consent', workflow_state_version: 6, value: { consented: false },
    } });
    if (denied.status() !== 422) throw new Error('False consent was accepted');
    await page.getByRole('checkbox').check();
    const consent = await act(() => page.getByRole('button', { name: 'Send referral request' }).click());
    if (!consent.approval_id) throw new Error('No bound approval was created');
    await move(6, 'BOOKING_IN_FLIGHT');
    const key = `browser-${id}`;
    const bookingCommand = crypto.randomUUID();
    const hospitalRequest = { ...payload, workflow_id: id, command_id: bookingCommand, approval_id: consent.approval_id, idempotency_key: key };
    const hospitalReply = await json(await api.post(`${mock}/appointments`, { data: hospitalRequest }));
    const confirmed = hospitalReply.booking.state === 'CONFIRMED';
    const report = { command_id: bookingCommand, approval_id: consent.approval_id,
      expected_state_version: 7, booking_payload: payload, screening_text: 'Schedule a cardiology consultation.',
      outcome: confirmed ? 'confirmed' : 'unverified', external_ref: confirmed ? hospitalReply.booking.external_reference : null };
    const completed = await json(await api.post(`${path}/bookings`, { headers, data: report }));
    const replayed = await json(await api.post(`${path}/bookings`, { headers, data: report }));
    if (replayed.replayed !== true) throw new Error('Booking command did not replay');
    await page.getByRole('heading', { name: /Workflow finished/ }).waitFor({ timeout: 5000 });
    if (confirmed) await page.getByText('Confirmed', { exact: true }).waitFor({ timeout: 5000 });
    else if (await page.getByText('Confirmed', { exact: true }).count()) throw new Error('Unconfirmed booking looked confirmed');
    // Both the terminal outcome and display-only receipt must survive a reload.
    await page.reload();
    await page.getByRole('heading', { name: /Workflow finished/ }).waitFor({ timeout: 5000 });
    if (confirmed) await page.getByText('Confirmed', { exact: true }).waitFor({ timeout: 5000 });
    const bookings = await json(await api.get(`${gateway}/v1/admin/workflows/${id}`, { headers }));
    if (bookings.bookings.length !== 1 || bookings.workflow.payload.answers.length !== 3) throw new Error('Unexpected booking or answer count');
    const stranger = await json(await api.post(`${gateway}/v1/auth/patient-session`, { data: { subject: 'stranger', patient_ref: 'stranger' } }));
    if ((await api.get(path, { headers: { authorization: `Bearer ${stranger.access_token}` } })).status() !== 404) throw new Error('Patient isolation failed');
    const mockReplay = await json(await api.post(`${mock}/appointments`, { data: hospitalRequest }));
    if (!hospitalReply.booking.booking_request_id || mockReplay.booking.booking_request_id !== hospitalReply.booking.booking_request_id) throw new Error('Mock booking replay changed its id');
    results.push({ adapter, workflowId: id, state: completed.state, hospitalState: hospitalReply.booking.state,
      answers: 3, bookings: 1, falseConsentRefused: true, patientIsolation: true, reload: true, replay: true });
  }
  return results;
}
