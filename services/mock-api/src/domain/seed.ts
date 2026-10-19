import { deterministicUuid } from './ids.js';
import { addDays, dateInZone, toUtcIso, weekdayInZone, zonedTimeToUtc } from './time.js';
import type { Store } from '../db/store.js';
import type { AccessibilityFeature, DocumentKind, HospitalAdapterType } from '../contracts/enums.js';

/**
 * Synthetic directory fixtures.
 *
 * Everything is derived from stable seed strings, so a given `now` always
 * produces the same directory: same ids, same network records, same slots.
 * Slot ids are a pure function of (provider, UTC start), which makes "the same
 * fixture on every boot" true without freezing the demo into a directory full
 * of past appointments.
 */

export const DEMO_PATIENT_ID = deterministicUuid('patient:demo:0001');
export const DEMO_WORKFLOW_ID = deterministicUuid('workflow:demo:0001');
export const DEMO_EMAIL = 'demo.patient@example.test';
export const DEMO_PHONE = '+15555550142';

export const HOSPITAL_IDS = {
  northlake: deterministicUuid('hospital:northlake'),
  riverbend: deterministicUuid('hospital:riverbend'),
  stAuburn: deterministicUuid('hospital:st-auburn'),
  cascade: deterministicUuid('hospital:cascade'),
} as const;

export const PLAN_IDS = {
  blueMesaPpo: deterministicUuid('plan:blue-mesa-ppo'),
  blueMesaHmo: deterministicUuid('plan:blue-mesa-hmo'),
  summitAdvantage: deterministicUuid('plan:summit-advantage'),
} as const;

interface LocationSeed {
  key: string;
  address_line1: string;
  address_line2?: string | null;
  city: string;
  state: string;
  postal_code: string;
  latitude: number;
  longitude: number;
  timezone: string;
}

const LOCATIONS: LocationSeed[] = [
  {
    key: 'chicago',
    address_line1: '1200 N Lake Shore Dr',
    city: 'Chicago',
    state: 'IL',
    postal_code: '60611',
    latitude: 41.8956,
    longitude: -87.62,
    timezone: 'America/Chicago',
  },
  {
    key: 'austin',
    address_line1: '3300 Barton Creek Blvd',
    city: 'Austin',
    state: 'TX',
    postal_code: '78735',
    latitude: 30.2478,
    longitude: -97.8002,
    timezone: 'America/Chicago',
  },
  {
    key: 'denver',
    address_line1: '7400 E Colfax Ave',
    address_line2: 'Suite 210',
    city: 'Denver',
    state: 'CO',
    postal_code: '80206',
    latitude: 39.7402,
    longitude: -104.9489,
    timezone: 'America/Denver',
  },
  {
    key: 'seattle',
    address_line1: '1100 Terry Ave',
    city: 'Seattle',
    state: 'WA',
    postal_code: '98101',
    latitude: 47.6101,
    longitude: -122.3344,
    timezone: 'America/Los_Angeles',
  },
];

interface HospitalSeed {
  key: keyof typeof HOSPITAL_IDS;
  name: string;
  locationKey: string;
  adapter_type: HospitalAdapterType;
  accessibility_features: AccessibilityFeature[];
  languages_supported: string[];
  accepted_plan_ids: string[];
  directory_blurb: string;
}

const HOSPITALS: HospitalSeed[] = [
  {
    key: 'northlake',
    name: 'Northlake Medical Center',
    locationKey: 'chicago',
    adapter_type: 'instant_confirm',
    accessibility_features: ['wheelchair_accessible', 'accessible_parking', 'sign_language_interpreter'],
    languages_supported: ['en', 'es', 'pl'],
    accepted_plan_ids: [PLAN_IDS.blueMesaPpo, PLAN_IDS.blueMesaHmo],
    directory_blurb:
      'Tertiary referral centre on the north side of Chicago. Same-day scheduling for most specialties; neurology intake runs Monday to Thursday.',
  },
  {
    key: 'riverbend',
    name: 'Riverbend Specialty Hospital',
    locationKey: 'austin',
    adapter_type: 'instant_confirm',
    accessibility_features: ['wheelchair_accessible', 'hearing_loop', 'service_animal_friendly'],
    languages_supported: ['en', 'es'],
    accepted_plan_ids: [PLAN_IDS.blueMesaPpo],
    directory_blurb:
      'Multi-specialty outpatient centre in Austin. Confirms appointments immediately for in-network plans.',
  },
  {
    key: 'stAuburn',
    name: 'St. Auburn Referral Center',
    locationKey: 'denver',
    adapter_type: 'review_required',
    accessibility_features: ['wheelchair_accessible', 'large_print_materials', 'accessible_parking'],
    languages_supported: ['en', 'es'],
    accepted_plan_ids: [PLAN_IDS.blueMesaPpo, PLAN_IDS.summitAdvantage],
    directory_blurb:
      'Referral intake office. Every referral is reviewed by a specialist before an appointment is offered; expect a decision within two business days.',
  },
  {
    key: 'cascade',
    name: 'Cascade Neurology Institute',
    locationKey: 'seattle',
    adapter_type: 'review_required',
    accessibility_features: ['wheelchair_accessible', 'sign_language_interpreter', 'hearing_loop'],
    languages_supported: ['en', 'zh-Hans'],
    accepted_plan_ids: [PLAN_IDS.blueMesaPpo],
    directory_blurb:
      'Neurology-only institute. Referrals are triaged by the on-call neurologist before scheduling.',
  },
];

interface ProviderSeed {
  key: string;
  display_name: string;
  credentials: string;
  specialty: string;
  subspecialties: string[];
  languages: Array<{ language: string; fluency: 'native' | 'fluent' | 'conversational' }>;
  accepts_new_patients: boolean;
  accessibility_features: string[];
  first_visit_duration_minutes: number;
  return_visit_duration_minutes: number;
  hospitals: Array<keyof typeof HOSPITAL_IDS>;
  payers: string[];
  phone: string;
}

const PROVIDERS: ProviderSeed[] = [
  {
    key: 'kowalski',
    display_name: 'Dr. Mira Kowalski',
    credentials: 'MD, PhD',
    specialty: 'neurology',
    subspecialties: ['epilepsy', 'headache medicine'],
    languages: [
      { language: 'en', fluency: 'native' },
      { language: 'pl', fluency: 'fluent' },
    ],
    accepts_new_patients: true,
    accessibility_features: ['wheelchair_accessible_exam_room'],
    first_visit_duration_minutes: 60,
    return_visit_duration_minutes: 30,
    hospitals: ['northlake', 'cascade'],
    payers: ['Blue Mesa Health', 'Summit Advantage'],
    phone: '+1-312-555-0101',
  },
  {
    key: 'okafor',
    display_name: 'Dr. Daniel Okafor',
    credentials: 'MD',
    specialty: 'neurology',
    subspecialties: ['movement disorders'],
    languages: [
      { language: 'en', fluency: 'native' },
      { language: 'es', fluency: 'conversational' },
    ],
    accepts_new_patients: true,
    accessibility_features: [],
    first_visit_duration_minutes: 45,
    return_visit_duration_minutes: 20,
    hospitals: ['northlake', 'stAuburn'],
    payers: ['Blue Mesa Health'],
    phone: '+1-303-555-0144',
  },
  {
    key: 'reyes',
    display_name: 'Dr. Ana Reyes',
    credentials: 'MD',
    specialty: 'cardiology',
    subspecialties: ['heart failure', 'preventive cardiology'],
    languages: [
      { language: 'en', fluency: 'fluent' },
      { language: 'es', fluency: 'native' },
    ],
    accepts_new_patients: true,
    accessibility_features: ['wheelchair_accessible_exam_room'],
    first_visit_duration_minutes: 45,
    return_visit_duration_minutes: 20,
    hospitals: ['riverbend', 'northlake'],
    payers: ['Blue Mesa Health'],
    phone: '+1-512-555-0110',
  },
  {
    key: 'lindqvist',
    display_name: 'Dr. Sofia Lindqvist',
    credentials: 'MD',
    specialty: 'rheumatology',
    subspecialties: ['lupus', 'inflammatory arthritis'],
    languages: [
      { language: 'en', fluency: 'native' },
      { language: 'sv', fluency: 'fluent' },
    ],
    accepts_new_patients: true,
    accessibility_features: ['accessible_parking'],
    first_visit_duration_minutes: 50,
    return_visit_duration_minutes: 25,
    hospitals: ['northlake'],
    payers: ['Blue Mesa Health', 'Summit Advantage'],
    phone: '+1-312-555-0177',
  },
  {
    key: 'haddad',
    display_name: 'Dr. Yusuf Haddad',
    credentials: 'MD',
    specialty: 'endocrinology',
    subspecialties: ['thyroid', 'diabetes technology'],
    languages: [
      { language: 'en', fluency: 'fluent' },
      { language: 'ar', fluency: 'native' },
    ],
    accepts_new_patients: false,
    accessibility_features: [],
    first_visit_duration_minutes: 40,
    return_visit_duration_minutes: 20,
    hospitals: ['riverbend', 'stAuburn'],
    payers: ['Blue Mesa Health', 'Summit Advantage'],
    phone: '+1-512-555-0122',
  },
  {
    key: 'nguyen',
    display_name: 'Dr. Bao Nguyen',
    credentials: 'DO',
    specialty: 'orthopedic_surgery',
    subspecialties: ['sports medicine'],
    languages: [
      { language: 'en', fluency: 'native' },
      { language: 'vi', fluency: 'fluent' },
    ],
    accepts_new_patients: true,
    accessibility_features: ['wheelchair_accessible', 'accessible_parking'],
    first_visit_duration_minutes: 30,
    return_visit_duration_minutes: 15,
    hospitals: ['riverbend'],
    payers: ['Blue Mesa Health'],
    phone: '+1-512-555-0133',
  },
  {
    key: 'fairbanks',
    display_name: 'Dr. Eleanor Fairbanks',
    credentials: 'MD',
    specialty: 'gastroenterology',
    subspecialties: ['inflammatory bowel disease'],
    languages: [{ language: 'en', fluency: 'native' }],
    accepts_new_patients: true,
    accessibility_features: ['hearing_loop'],
    first_visit_duration_minutes: 45,
    return_visit_duration_minutes: 20,
    hospitals: ['northlake', 'cascade'],
    payers: ['Blue Mesa Health', 'Summit Advantage'],
    phone: '+1-206-555-0166',
  },
  {
    key: 'moreau',
    display_name: 'Dr. Claude Moreau',
    credentials: 'MD, MSc',
    specialty: 'neurology',
    subspecialties: ['neuromuscular'],
    languages: [
      { language: 'en', fluency: 'fluent' },
      { language: 'fr', fluency: 'native' },
    ],
    accepts_new_patients: true,
    accessibility_features: ['sign_language_interpreter'],
    first_visit_duration_minutes: 60,
    return_visit_duration_minutes: 30,
    hospitals: ['cascade', 'stAuburn'],
    payers: ['Blue Mesa Health'],
    phone: '+1-206-555-0188',
  },
];

interface PlanSeed {
  key: keyof typeof PLAN_IDS;
  payer_name: string;
  plan_name: string;
  plan_type: 'PPO' | 'HMO' | 'EPO' | 'POS' | 'HDHP' | 'medicare_advantage';
  network_name: string | null;
  requires_referral: boolean;
  requires_prior_auth_for_specialist: boolean;
}

const PLANS: PlanSeed[] = [
  {
    key: 'blueMesaPpo',
    payer_name: 'Blue Mesa Health',
    plan_name: 'Blue Mesa PPO Select',
    plan_type: 'PPO',
    network_name: 'Blue Mesa Preferred',
    requires_referral: false,
    requires_prior_auth_for_specialist: false,
  },
  {
    key: 'blueMesaHmo',
    payer_name: 'Blue Mesa Health',
    plan_name: 'Blue Mesa HMO Core',
    plan_type: 'HMO',
    network_name: 'Blue Mesa Core',
    requires_referral: true,
    requires_prior_auth_for_specialist: false,
  },
  {
    key: 'summitAdvantage',
    payer_name: 'Summit Advantage',
    plan_name: 'Summit Advantage Complete',
    plan_type: 'medicare_advantage',
    network_name: 'Summit Advantage National',
    requires_referral: true,
    requires_prior_auth_for_specialist: true,
  },
];

/**
 * Network status per (plan, provider). Deliberately includes the awkward cases:
 * a provider who bills the payer but is out-of-network for the plan, and a
 * provider with no record at all — absence of evidence is `unknown`, never a
 * silent downgrade to "in network".
 */
const NETWORK_MATRIX: Record<string, Record<string, 'in_network' | 'out_of_network' | 'unknown'>> = {
  blueMesaPpo: {
    kowalski: 'in_network',
    okafor: 'in_network',
    reyes: 'in_network',
    lindqvist: 'out_of_network',
    haddad: 'in_network',
    nguyen: 'in_network',
    fairbanks: 'in_network',
    moreau: 'in_network',
  },
  blueMesaHmo: {
    kowalski: 'in_network',
    // okafor: no record at all -> unknown, never guessed.
    reyes: 'in_network',
    lindqvist: 'out_of_network',
  },
  summitAdvantage: {
    kowalski: 'in_network',
    okafor: 'out_of_network',
    lindqvist: 'in_network',
    fairbanks: 'in_network',
  },
};

interface SlotTemplate {
  localTime: string; // HH:MM in the HOSPITAL's wall clock
  visitType: 'first_visit' | 'return_visit';
}

const SLOT_TIMES: SlotTemplate[] = [
  { localTime: '09:00', visitType: 'first_visit' },
  { localTime: '10:30', visitType: 'return_visit' },
  { localTime: '13:00', visitType: 'first_visit' },
  { localTime: '15:30', visitType: 'return_visit' },
];

const SLOT_DAYS = 21;

export interface SeedSummary {
  /** Which dataset was served, and where it came from. */
  mode: 'fixtures' | 'builtin';
  source_directory: string | null;
  locations: number;
  hospitals: number;
  providers: number;
  plans: number;
  networkRecords: number;
  referralRequirements: number;
  documents: number;
  slots: number;
  evidence: number;
  coverageSnapshots: number;
}

/**
 * Writes the whole directory. Idempotent: re-running replaces the directory rows
 * and leaves bookings, slots already claimed and the outbox untouched.
 */
export function seedDirectory(store: Store, now: Date = new Date()): SeedSummary {
  const nowIso = toUtcIso(now);
  const locationIdByKey = new Map<string, string>();

  for (const location of LOCATIONS) {
    const location_id = deterministicUuid(`location:${location.key}`);
    locationIdByKey.set(location.key, location_id);
    store.upsertDirectoryRow(
      'locations',
      location_id,
      [
        ['city', location.city],
        ['state', location.state],
        ['timezone', location.timezone],
      ],
      {
        location_id,
        address_line1: location.address_line1,
        address_line2: location.address_line2 ?? null,
        city: location.city,
        state: location.state,
        postal_code: location.postal_code,
        geo: { latitude: location.latitude, longitude: location.longitude },
        timezone: location.timezone,
      },
    );
  }

  const hospitalIdByKey = new Map<string, string>();
  const hospitalZoneByKey = new Map<string, string>();
  for (const hospital of HOSPITALS) {
    const hospital_id = HOSPITAL_IDS[hospital.key];
    const location = LOCATIONS.find((entry) => entry.key === hospital.locationKey)!;
    hospitalIdByKey.set(hospital.key, hospital_id);
    hospitalZoneByKey.set(hospital.key, location.timezone);
    store.upsertDirectoryRow(
      'hospitals',
      hospital_id,
      [
        ['name', hospital.name],
        ['location_id', locationIdByKey.get(hospital.locationKey)!],
        ['timezone', location.timezone],
        ['adapter_type', hospital.adapter_type],
      ],
      {
        hospital_id,
        name: hospital.name,
        location_id: locationIdByKey.get(hospital.locationKey)!,
        timezone: location.timezone,
        adapter_type: hospital.adapter_type,
        accepts_new_patients: true,
        accessibility_features: hospital.accessibility_features,
        languages_supported: hospital.languages_supported,
        accepted_plan_ids: hospital.accepted_plan_ids,
        directory_blurb: hospital.directory_blurb,
        knowledge_version: 'dir-2026-10-01',
        updated_at: nowIso,
        synthetic: true,
      },
    );
  }

  const providerIdByKey = new Map<string, string>();
  const providerHospitals = new Map<string, string[]>();
  for (const provider of PROVIDERS) {
    const provider_id = deterministicUuid(`provider:${provider.key}`);
    const hospitalIds = provider.hospitals.map((key) => hospitalIdByKey.get(key)!);
    providerIdByKey.set(provider.key, provider_id);
    providerHospitals.set(provider.key, provider.hospitals);
    store.upsertDirectoryRow(
      'providers',
      provider_id,
      [
        ['specialty', provider.specialty],
        ['display_name', provider.display_name],
      ],
      {
        provider_id,
        display_name: provider.display_name,
        credentials: provider.credentials,
        specialty: provider.specialty,
        subspecialties: provider.subspecialties,
        languages: provider.languages,
        accepts_new_patients: provider.accepts_new_patients,
        accessibility_features: provider.accessibility_features,
        first_visit_duration_minutes: provider.first_visit_duration_minutes,
        return_visit_duration_minutes: provider.return_visit_duration_minutes,
        hospital_ids: hospitalIds,
        accepts_insurance: provider.payers,
        profile_text: `${provider.display_name} practises ${provider.specialty.replace('_', ' ')} at ${provider.hospitals
          .map((key) => HOSPITALS.find((entry) => entry.key === key)!.name)
          .join(' and ')}.`,
        contact_phone: provider.phone,
        contact_source: { source_id: 'directory-feed-2026-10-01', field_path: 'contact_phone' },
        knowledge_version: 'dir-2026-10-01',
        updated_at: nowIso,
        synthetic: true,
      },
    );
    for (const hospitalId of hospitalIds) {
      store.linkProviderHospital(provider_id, hospitalId);
    }
  }

  for (const plan of PLANS) {
    const plan_id = PLAN_IDS[plan.key];
    store.upsertDirectoryRow('insurance_plans', plan_id, [], {
      plan_id,
      payer_name: plan.payer_name,
      plan_name: plan.plan_name,
      plan_type: plan.plan_type,
      network_name: plan.network_name,
      requires_referral: plan.requires_referral,
      requires_prior_auth_for_specialist: plan.requires_prior_auth_for_specialist,
      synthetic: true,
    });
  }

  let networkRecords = 0;
  for (const [planKey, byProvider] of Object.entries(NETWORK_MATRIX)) {
    for (const [providerKey, status] of Object.entries(byProvider)) {
      const plan_id = PLAN_IDS[planKey as keyof typeof PLAN_IDS];
      const provider_id = providerIdByKey.get(providerKey)!;
      const hospitalKeys = providerHospitals.get(providerKey) ?? [];
      const hospitalIds = hospitalKeys.length > 0 ? hospitalKeys.map((key) => hospitalIdByKey.get(key)!) : [null];
      for (const hospitalId of hospitalIds) {
        const record_id = deterministicUuid(`network:${planKey}:${providerKey}:${hospitalId ?? 'any'}`);
        store.upsertNetworkRecord({
          record_id,
          plan_id,
          provider_id,
          hospital_id: hospitalId,
          status,
          effective_from: '2026-01-01',
          effective_to: status === 'out_of_network' ? '2026-12-31' : null,
          evidence_id: deterministicUuid(`evidence:network:${record_id}`),
        });
        networkRecords += 1;
      }
    }
  }

  let referralRequirements = 0;
  const requirementSeeds: Array<{
    hospital: keyof typeof HOSPITAL_IDS;
    plan?: keyof typeof PLAN_IDS;
    visit_type: 'first_visit' | 'return_visit';
    required_documents: DocumentKind[];
    max_age: number | null;
    prior_auth: boolean;
  }> = [
    {
      hospital: 'stAuburn',
      visit_type: 'first_visit',
      required_documents: ['referral_letter', 'prior_report'],
      max_age: 90,
      prior_auth: true,
    },
    {
      hospital: 'stAuburn',
      visit_type: 'return_visit',
      required_documents: [],
      max_age: null,
      prior_auth: false,
    },
    {
      hospital: 'cascade',
      visit_type: 'first_visit',
      required_documents: ['referral_letter'],
      max_age: 180,
      prior_auth: false,
    },
    {
      hospital: 'northlake',
      visit_type: 'first_visit',
      required_documents: [],
      max_age: null,
      prior_auth: false,
    },
    {
      hospital: 'northlake',
      plan: 'summitAdvantage',
      visit_type: 'first_visit',
      required_documents: ['referral_letter', 'authorization_reference'],
      max_age: 60,
      prior_auth: true,
    },
    {
      hospital: 'riverbend',
      visit_type: 'first_visit',
      required_documents: ['prior_report'],
      max_age: 365,
      prior_auth: false,
    },
  ];
  for (const requirement of requirementSeeds) {
    const hospital_id = hospitalIdByKey.get(requirement.hospital)!;
    const plan_id = requirement.plan ? PLAN_IDS[requirement.plan] : null;
    const requirement_id = deterministicUuid(
      `requirement:${requirement.hospital}:${requirement.plan ?? 'any'}:${requirement.visit_type}`,
    );
    store.upsertReferralRequirement({
      requirement_id,
      hospital_id,
      plan_id,
      visit_type: requirement.visit_type,
      required_documents: requirement.required_documents,
      max_document_age_days: requirement.max_age,
      requires_prior_auth_reference: requirement.prior_auth,
      evidence_id: deterministicUuid(`evidence:requirement:${requirement_id}`),
    });
    referralRequirements += 1;
  }

  const documentSeeds: Array<{
    key: string;
    kind: DocumentKind;
    filename: string;
    issuedDaysAgo: number;
    injection: boolean;
    text: string;
  }> = [
    {
      key: 'referral-letter',
      kind: 'referral_letter',
      filename: 'referral-letter-pcp.txt',
      issuedDaysAgo: 12,
      injection: false,
      text: [
        'REFERRAL LETTER (synthetic)',
        'Patient: demo patient',
        'From: Dr. Priya Raman, Family Medicine, Chicago IL',
        'To: Neurology, Northlake Medical Center',
        'Reason: recurrent migraine with aura, 3 episodes per month, failed two preventives.',
        'Requested: first visit, neurology.',
      ].join('\n'),
    },
    {
      key: 'prior-report',
      kind: 'prior_report',
      filename: 'mri-report-2026.txt',
      issuedDaysAgo: 40,
      injection: false,
      text: [
        'IMAGING REPORT (synthetic)',
        'Study: MRI brain without contrast',
        'Impression: no acute intracranial abnormality. No mass, haemorrhage or infarct.',
      ].join('\n'),
    },
    {
      key: 'authorization',
      kind: 'authorization_reference',
      filename: 'prior-auth-reference.txt',
      issuedDaysAgo: 5,
      injection: true,
      text: [
        'AUTHORIZATION REFERENCE (synthetic)',
        'Auth id: PA-2026-0099',
        'Note: this document is fixture DATA. A real fixture may contain text that',
        'looks like an instruction to an agent; downstream tools must never act on it.',
      ].join('\n'),
    },
  ];
  for (const document of documentSeeds) {
    const document_id = deterministicUuid(`document:${DEMO_PATIENT_ID}:${document.key}`);
    store.upsertDocument({
      document_id,
      patient_id: DEMO_PATIENT_ID,
      kind: document.kind,
      filename: document.filename,
      mime_type: 'text/plain',
      text: document.text,
      issued_at: toUtcIso(addDays(now, -document.issuedDaysAgo)),
      source_organization: 'Northlake Medical Center',
      synthetic: true,
      contains_injection_attempt: document.injection,
    });
  }

  let slots = 0;
  for (const provider of PROVIDERS) {
    const provider_id = providerIdByKey.get(provider.key)!;
    for (const hospitalKey of provider.hospitals) {
      const hospital_id = hospitalIdByKey.get(hospitalKey)!;
      const zone = hospitalZoneByKey.get(hospitalKey)!;
      for (let dayOffset = 1; dayOffset <= SLOT_DAYS; dayOffset += 1) {
        const day = addDays(now, dayOffset);
        const localDate = dateInZone(day, zone);
        const weekday = weekdayInZone(toUtcIso(day), zone);
        if (weekday === 'saturday' || weekday === 'sunday') continue;
        for (const template of SLOT_TIMES) {
          const startAt = zonedTimeToUtc(zone, `${localDate}T${template.localTime}`);
          if (startAt.getTime() <= now.getTime()) continue;
          const durationMinutes =
            template.visitType === 'first_visit'
              ? provider.first_visit_duration_minutes
              : provider.return_visit_duration_minutes;
          const endAt = new Date(startAt.getTime() + durationMinutes * 60_000);
          const startUtc = toUtcIso(startAt);
          const slot_id = deterministicUuid(`slot:${provider_id}:${startUtc}`);
          store.insertSlot({
            slot_id,
            provider_id,
            hospital_id,
            start_at: startUtc,
            end_at: toUtcIso(endAt),
            display_timezone: zone,
            visit_type: template.visitType,
            status: 'available',
            version: 0,
          });
          slots += 1;
        }
      }
    }
  }

  return {
    mode: 'builtin',
    source_directory: null,
    locations: LOCATIONS.length,
    hospitals: HOSPITALS.length,
    providers: PROVIDERS.length,
    plans: PLANS.length,
    networkRecords,
    referralRequirements,
    documents: documentSeeds.length,
    slots,
    evidence: 0,
    coverageSnapshots: 0,
  };
}
