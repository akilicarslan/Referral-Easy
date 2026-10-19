import type { sse$SurfaceUpdate } from '@referraleasy/contracts';

/**
 * Static `SurfaceUpdate` fixtures for the demo page.
 *
 * These exist so every component — and, more importantly, every *honest state*
 * — can be seen without a backend running. The correctness rules that are
 * otherwise only visible in the negative are represented here as first-class
 * scenes, so a reviewer can look at them rather than take them on trust:
 *
 *   - a coverage `unknown` with its freshness
 *   - a `REQUESTED` booking, which must not look confirmed
 *   - a `BOOKING_UNVERIFIED` booking, which must not look like either
 *   - a degraded run, visibly flagged
 *   - a directory failure that says "we could not check" rather than "nothing"
 *   - a payload missing most of its fields
 *
 * Every fixture is marked `synthetic: true`, and the UI labels it. Canned data
 * that can pass as a measurement is the thing §18 forbids.
 */

/** Deterministic ids so the demo page is reproducible and diffable. */
const ID = {
  surface: '11111111-1111-4111-8111-111111111111',
  hospitals: '21111111-1111-4111-8111-111111111111',
  doctors: '21111111-1111-4111-8111-222222222222',
  doctorsAwaiting: '21111111-1111-4111-8111-223333333333',
  slots: '21111111-1111-4111-8111-3344444444aa',
  form: '31111111-1111-4111-8111-111111111111',
  review: '41111111-1111-4111-8111-111111111111',
  receiptRequested: '51111111-1111-4111-8111-111111111111',
  receiptConfirmed: '51111111-1111-4111-8111-222222222222',
  receiptUnverified: '51111111-1111-4111-8111-333333333333',
  fallback: '61111111-1111-4111-8111-111111111111',
  handoff: '71111111-1111-4111-8111-111111111111',
  empty: '81111111-1111-4111-8111-111111111111',
  malformed: '91111111-1111-4111-8111-111111111111',
} as const;

function hoursAgo(hours: number): string {
  return new Date(Date.now() - hours * 3_600_000).toISOString();
}

function daysAhead(days: number, hour = 9, minute = 10): string {
  const date = new Date();
  date.setDate(date.getDate() + days);
  date.setHours(hour, minute, 0, 0);
  return date.toISOString();
}

/** The eight components, each rendered in the state that best shows its rules. */
export const DEMO_SCENES: readonly {
  readonly key: string;
  readonly label: string;
  readonly note: string;
  readonly update: sse$SurfaceUpdate;
}[] = [
  {
    key: 'hospitals',
    label: 'HospitalPicker',
    note: 'Coverage is a section variant. One hospital is verified, one is unknown, one has no coverage data at all. Nothing is preselected.',
    update: update([
      {
        instance_id: ID.hospitals,
        component: 'HospitalPicker',
        workflow_state_version: 3,
        action: 'system:await_patient_choice',
        submitted: false,
        disabled: false,
        notice: null,
        props: {
          title: 'Choose a hospital',
          subtitle: 'We checked these against your plan where we could.',
          coverage: 'unknown',
          coverage_freshness: { checked_at: hoursAgo(18), age_hours: 18 },
          hospitals: [
            {
              id: 'hosp-1',
              name: "St. Mary's General",
              city: 'Leeds',
              region: 'West Yorkshire',
              distance_km: 2.4,
              coverage: 'yes',
              coverage_checked_at: hoursAgo(3),
              accepting_referrals: true,
            },
            {
              id: 'hosp-2',
              name: 'Northgate Royal Infirmary',
              city: 'Leeds',
              distance_km: 6.1,
              coverage: 'unknown',
              coverage_checked_at: hoursAgo(18),
              accepting_referrals: true,
              last_seen: hoursAgo(30),
            },
            {
              id: 'hosp-3',
              name: 'Riverside Community Hospital',
              city: 'Bradford',
              distance_km: 14.8,
              accepting_referrals: false,
            },
          ],
          selected_id: null,
          total: 11,
          synthetic: true,
        },
      },
    ]),
  },

  {
    key: 'doctors',
    label: 'DoctorPicker (in order)',
    note: 'The hospital is named, so a stale payload cannot present doctors from somewhere else.',
    update: update([
      {
        instance_id: ID.doctors,
        component: 'DoctorPicker',
        workflow_state_version: 5,
        action: 'system:await_patient_choice',
        submitted: false,
        disabled: false,
        notice: null,
        props: {
          title: 'Choose a doctor',
          hospital_id: 'hosp-1',
          hospital_name: "St. Mary's General",
          awaiting_hospital: false,
          doctors: [
            {
              id: 'doc-1',
              name: 'Dr Amelia Okafor',
              specialty: 'Cardiology',
              languages: ['English', 'Igbo'],
              coverage: 'yes',
              coverage_checked_at: hoursAgo(3),
              wait_days: 4,
            },
            {
              id: 'doc-2',
              name: 'Dr Ravi Menon',
              specialty: 'Cardiology',
              languages: ['English', 'Malayalam'],
              coverage: 'no',
              coverage_checked_at: hoursAgo(52),
            },
          ],
          selected_id: null,
          total: 6,
          synthetic: true,
        },
      },
    ]),
  },

  {
    key: 'doctors-out-of-order',
    label: 'DoctorPicker (before a hospital)',
    note: 'Doctors are never shown before a hospital is chosen. This is what the guard renders instead.',
    update: update([
      {
        instance_id: ID.doctorsAwaiting,
        component: 'DoctorPicker',
        workflow_state_version: 1,
        action: null,
        props: {
          title: 'Choose a doctor',
          awaiting_hospital: true,
          doctors: [],
        },
      },
    ]),
  },

  {
    key: 'slots',
    label: 'SlotPicker',
    note: 'No slot is preselected, and every time carries its zone. The third slot has no zone on purpose.',
    update: update([
      {
        instance_id: ID.slots,
        component: 'SlotPicker',
        workflow_state_version: 7,
        action: 'scheduling:create_booking',
        submitted: false,
        disabled: false,
        notice: null,
        props: {
          title: 'Choose a time',
          provider_name: 'Dr Amelia Okafor',
          provider_id: 'doc-1',
          display_timezone: 'Europe/London',
          selected_id: null,
          slots: [
            { id: 'slot-1', start_at: daysAhead(4, 9, 10), end_at: daysAhead(4, 9, 40), visit_type: 'In person', location: 'Cardiology, Level 3' },
            { id: 'slot-2', start_at: daysAhead(4, 11, 30), end_at: daysAhead(4, 12, 0), visit_type: 'In person' },
            { id: 'slot-3', start_at: daysAhead(5, 14, 0), end_at: daysAhead(5, 14, 30), timezone: undefined },
            { id: 'slot-4', start_at: daysAhead(5, 16, 15), end_at: daysAhead(5, 16, 45), available: false },
          ],
          total: 9,
          synthetic: true,
        },
      },
    ]),
  },

  {
    key: 'form',
    label: 'PatientDetailsForm',
    note: 'A real form. Fixture documents are labelled, and the injection fixture is shown rather than hidden.',
    update: update([
      {
        instance_id: ID.form,
        component: 'PatientDetailsForm',
        workflow_state_version: 8,
        action: 'intake:validate_referral',
        submitted: false,
        disabled: false,
        notice: null,
        props: {
          title: 'Your details',
          subtitle: 'The hospital needs these before it can accept the referral.',
          fields: [
            { name: 'given_name', label: 'First name', type: 'text', required: true, autocomplete: 'given-name' },
            { name: 'family_name', label: 'Last name', type: 'text', required: true, autocomplete: 'family-name' },
            { name: 'date_of_birth', label: 'Date of birth', type: 'date', required: true, autocomplete: 'bday' },
            { name: 'phone', label: 'Phone', type: 'tel', required: true, autocomplete: 'tel', hint: 'Used only to reach you about this referral.' },
            { name: 'referral_reason', label: 'Reason for referral', type: 'textarea', required: false },
            { name: 'blood_group', label: 'Blood group', type: 'lookup_widget', required: false },
          ],
          documents: [
            { document_id: 'doc-fixture-referral-letter', kind: 'Referral letter', filename: 'referral-letter-2026-09.pdf', synthetic: true, contains_injection_attempt: false },
            { document_id: 'doc-fixture-injection', kind: 'Patient note', filename: 'handwritten-note.pdf', synthetic: true, contains_injection_attempt: true },
          ],
          values: {},
          errors: {},
          submit_label: 'Save details',
          synthetic: true,
        },
      },
    ]),
  },

  {
    key: 'review',
    label: 'BookingReview',
    note: 'Consent gates the button, and the button explains why it is unavailable rather than just greying out.',
    update: update([
      {
        instance_id: ID.review,
        component: 'BookingReview',
        workflow_state_version: 9,
        action: 'scheduling:create_booking',
        submitted: false,
        disabled: false,
        notice: null,
        props: {
          title: 'Review your referral',
          hospital: { name: "St. Mary's General", city: 'Leeds' },
          provider: { name: 'Dr Amelia Okafor', specialty: 'Cardiology' },
          slot: { start_at: daysAhead(4, 9, 10), label: 'Fri 4 days from now, 09:10', timezone: 'Europe/London' },
          patient_summary: {
            given_name: 'Jordan',
            family_name: 'Ellery',
            date_of_birth: '1984-03-11',
            phone: '+44 7700 900123',
          },
          attachments: [
            { kind: 'Referral letter', filename: 'referral-letter-2026-09.pdf', synthetic: true },
          ],
          consent_text:
            'I agree to these details being shared with St. Mary’s General for the purpose of this referral.',
          requires_consent: true,
          submit_label: 'Send referral request',
          degraded: null,
          synthetic: true,
        },
      },
    ]),
  },

  {
    key: 'review-degraded',
    label: 'BookingReview (degraded)',
    note: 'A reduced pipeline is always visible. An answer produced without the directory is a different answer.',
    update: update([
      {
        instance_id: ID.review,
        component: 'BookingReview',
        workflow_state_version: 9,
        action: 'scheduling:create_booking',
        submitted: false,
        disabled: false,
        notice: 'Ranking was reduced for this step.',
        props: {
          title: 'Review your referral',
          hospital: { name: "St. Mary's General" },
          provider: { name: 'Dr Amelia Okafor' },
          slot: { start_at: daysAhead(4, 9, 10), label: 'Fri 4 days from now, 09:10', timezone: 'Europe/London' },
          patient_summary: { given_name: 'Jordan', family_name: 'Ellery' },
          consent_text: 'I agree to these details being shared with the hospital.',
          requires_consent: true,
          degraded: 'embeddings_down',
          synthetic: true,
        },
      },
    ]),
  },

  {
    key: 'receipt-requested',
    label: 'AppointmentReceipt — REQUESTED',
    note: 'The single most important scene. A request awaiting review must not read as confirmation: no tick, no green, no "confirmed", no confirmation reference.',
    update: update([
      {
        instance_id: ID.receiptRequested,
        component: 'AppointmentReceipt',
        workflow_state_version: 12,
        action: null,
        props: {
          title: 'Your appointment',
          state: 'REQUESTED',
          booking_request_id: 'BR-2026-000418',
          confirmation_reference: 'REF-8891-QK',
          hospital: { name: "St. Mary's General", city: 'Leeds' },
          provider: { name: 'Dr Amelia Okafor', specialty: 'Cardiology' },
          slot: { start_at: daysAhead(4, 9, 10), visit_type: 'In person' },
          display_timezone: 'Europe/London',
          next_step:
            'The hospital reviews requests within one working day. You will be contacted on the number you gave.',
          reconciles_at: new Date(Date.now() + 3_600_000).toISOString(),
          degraded: null,
          synthetic: true,
        },
      },
    ]),
  },

  {
    key: 'receipt-confirmed',
    label: 'AppointmentReceipt — BOOKED',
    note: 'The only state allowed to look confirmed.',
    update: update([
      {
        instance_id: ID.receiptConfirmed,
        component: 'AppointmentReceipt',
        workflow_state_version: 12,
        action: null,
        props: {
          title: 'Your appointment',
          state: 'BOOKED',
          confirmation_reference: 'REF-8891-QK',
          hospital: { name: "St. Mary's General", city: 'Leeds' },
          provider: { name: 'Dr Amelia Okafor', specialty: 'Cardiology' },
          slot: { start_at: daysAhead(4, 9, 10), visit_type: 'In person' },
          display_timezone: 'Europe/London',
          next_step: 'Bring photo ID. Arrive ten minutes early.',
          synthetic: true,
        },
      },
    ]),
  },

  {
    key: 'receipt-unverified',
    label: 'AppointmentReceipt — BOOKING_UNVERIFIED',
    note: 'The outcome could not be established. It must not read as success, and it must not read as failure.',
    update: update([
      {
        instance_id: ID.receiptUnverified,
        component: 'AppointmentReceipt',
        workflow_state_version: 12,
        action: null,
        props: {
          title: 'Your appointment',
          state: 'BOOKING_UNVERIFIED',
          booking_request_id: 'BR-2026-000419',
          derived_from: 'adapter_timeout',
          hospital: { name: "St. Mary's General" },
          provider: { name: 'Dr Amelia Okafor' },
          slot: { start_at: daysAhead(4, 9, 10) },
          display_timezone: 'Europe/London',
          next_step:
            'Do not assume this is booked. We are checking with the hospital and will tell you either way.',
          reconciles_at: new Date(Date.now() + 900_000).toISOString(),
          synthetic: true,
        },
      },
    ]),
  },

  {
    key: 'fallback',
    label: 'DirectoryFallbackCard',
    note: '"We could not check" never renders as "there is nothing". Each unchecked item is named, and last-known data is fenced off with its age.',
    update: update([
      {
        instance_id: ID.fallback,
        component: 'DirectoryFallbackCard',
        workflow_state_version: 4,
        action: 'directory:search',
        submitted: false,
        disabled: false,
        notice: null,
        props: {
          reason_code: 'upstream_unavailable',
          reason_detail: 'The directory service did not respond within 3000 ms.',
          degraded: 'opensearch_down',
          unchecked: [
            'Whether these hospitals accept your plan',
            'Whether appointments are available this week',
            'Whether the listed waiting times are current',
          ],
          can_retry: true,
          retry_label: 'Check the directory again',
          last_known: {
            label: 'Nearest hospital on record',
            value: "St. Mary's General",
            checked_at: hoursAgo(18),
          },
          synthetic: true,
        },
      },
    ]),
  },

  {
    key: 'handoff',
    label: 'HumanHandoffCard',
    note: 'Says what was already passed on, and admits when no response window was given rather than inventing one.',
    update: update([
      {
        instance_id: ID.handoff,
        component: 'HumanHandoffCard',
        workflow_state_version: 15,
        action: null,
        props: {
          reason_code: 'no_candidate_matched',
          reason_detail: 'Nothing in the directory met the stated constraints.',
          handed_over: [
            'Your details and contact number',
            'The reason for referral',
            'The attached referral letter',
            'The hospitals you had already been shown',
          ],
          response_window: '2 working days',
          reference: 'HITL-2026-0117',
          contact_channels: [
            { label: 'Referral team', value: '+44 113 496 0000' },
            { label: 'Opening hours', value: 'Mon–Fri, 08:30–17:00' },
          ],
          synthetic: true,
        },
      },
    ]),
  },

  {
    key: 'empty',
    label: 'Empty state',
    note: 'An empty list is a real state and is said out loud, with the reason the server gave.',
    update: update([
      {
        instance_id: ID.empty,
        component: 'SlotPicker',
        workflow_state_version: 6,
        action: 'system:await_patient_choice',
        submitted: false,
        disabled: false,
        notice: null,
        props: {
          title: 'Choose a time',
          provider_name: 'Dr Amelia Okafor',
          slots: [],
          selected_id: null,
          empty_reason:
            'This doctor has no open slots in the next six weeks. Choose another doctor to see their availability.',
          total: 0,
        },
      },
    ]),
  },

  {
    key: 'sparse',
    label: 'Missing fields',
    note: 'A payload with almost nothing in it. It must render, not blank the screen.',
    update: update([
      {
        instance_id: ID.malformed,
        component: 'BookingReview',
        workflow_state_version: 2,
        action: 'scheduling:create_booking',
        submitted: false,
        disabled: false,
        notice: null,
        props: {},
      },
    ]),
  },
];

/** Wrap component instances in a `SurfaceUpdate` with the same surface id. */
function update(components: sse$SurfaceUpdate['components']): sse$SurfaceUpdate {
  return {
    surface_id: ID.surface,
    components,
  };
}

/** One scene's update, by key. */
export function demoScene(key: string): sse$SurfaceUpdate {
  const scene = DEMO_SCENES.find((entry) => entry.key === key);
  if (scene === undefined) {
    throw new Error(`No demo scene named "${key}". Known: ${DEMO_SCENES.map((s) => s.key).join(', ')}`);
  }
  return scene.update;
}

/** Every scene as a single ordered surface — the "everything at once" view. */
export function allScenesUpdate(): sse$SurfaceUpdate {
  const components = DEMO_SCENES.flatMap((scene) => scene.update.components);
  return { surface_id: ID.surface, components };
}

export const DEMO_SURFACE_ID = ID.surface;
