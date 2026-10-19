import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { loadConfig } from '../src/config.js';
import { call, makeFixtureHarness, type Harness } from './helpers.js';

/**
 * Coverage against the authored fixture rows.
 *
 * `fixtures/coverage/coverage_results.json` is the demo's expectation of what a
 * coverage lookup answers, and `scenarios.json` builds a whole journey
 * (`network_conflict`) on top of three of these rows. This file asks the API
 * every authored question and compares the answer field by field, so the two
 * can never drift apart silently.
 *
 * The comparison is deliberately exhaustive rather than selective: the fields
 * the fixtures pin are exactly the fields an assistant is tempted to guess at
 * (`accepts_insurance`, `in_network_for_plan`, `conflict`, freshness), and a
 * change in how any of them is derived has to fail here first.
 */

const config = loadConfig();

let harness: Harness | null = null;

afterEach(async () => {
  await harness?.cleanup();
  harness = null;
});

interface AuthoredCoverage {
  coverage_id: string;
  plan_id: string;
  provider_id: string | null;
  hospital_id: string | null;
  accepts_insurance: boolean | null;
  in_network_for_plan: boolean | null;
  status: 'in_network' | 'out_of_network' | 'unknown';
  referral_required: boolean | null;
  prior_auth_required: boolean | null;
  conflict: boolean;
  conflicting_evidence_ids: string[];
  winning_evidence_id: string | null;
  freshness: { state: string; age_seconds: number };
  evidence_ids: string[];
  disclaimer_key: string;
  synthetic: boolean;
}

interface Served {
  coverage_id: string;
  plan_id: string;
  provider_id: string | null;
  hospital_id: string | null;
  accepts_insurance: boolean | null;
  in_network_for_plan: boolean | null;
  status: string;
  referral_required: boolean | null;
  prior_auth_required: boolean | null;
  conflict?: boolean;
  conflicting_evidence_ids?: string[];
  winning_evidence_id: string | null;
  freshness: { state: string; age_seconds: number };
  evidence_ids: string[];
  disclaimer_key?: string;
  synthetic: boolean;
}

function listFixture<T>(...parts: string[]): T[] {
  const parsed = JSON.parse(readFileSync(resolve(config.fixturesRoot, ...parts), 'utf8')) as unknown;
  const rows = Array.isArray(parsed) ? parsed : Object.values(parsed as Record<string, unknown>).find(Array.isArray);
  if (!Array.isArray(rows)) throw new Error(`fixture ${parts.join('/')} is not a list`);
  return rows as T[];
}

const AUTHORED = listFixture<AuthoredCoverage>('coverage', 'coverage_results.json');

function bySuffix(suffix: string): AuthoredCoverage {
  const row = AUTHORED.find((entry) => entry.coverage_id.endsWith(suffix));
  if (!row) throw new Error(`no authored coverage result ending ${suffix}`);
  return row;
}

async function fixtureHarness(): Promise<Harness> {
  harness = await makeFixtureHarness();
  return harness;
}

async function lookup(h: Harness, row: Pick<AuthoredCoverage, 'coverage_id' | 'plan_id' | 'provider_id' | 'hospital_id'>): Promise<Served> {
  const response = await call(h.app, {
    method: 'POST',
    url: '/coverage/lookup',
    payload: { plan_id: row.plan_id, provider_id: row.provider_id, hospital_id: row.hospital_id },
  });
  expect(response.status, `lookup for ${row.coverage_id} must be answered, not rejected: ${JSON.stringify(response.body)}`).toBe(200);
  return response.body as Served;
}

/* Every field of the frozen CoverageResult that the fixture rows pin, read out
   of the served body and out of the authored row. `evidence_ids` and the
   conflict pair are compared in order: the order is the citation trail the
   answer is argued with, leading with the dispute. */
const FIELDS: Array<{
  field: string;
  served: (served: Served) => unknown;
  authored: (row: AuthoredCoverage) => unknown;
  equal?: (served: unknown, authored: unknown) => boolean;
}> = [
  { field: 'plan_id', served: (s) => s.plan_id, authored: (a) => a.plan_id },
  { field: 'provider_id', served: (s) => s.provider_id, authored: (a) => a.provider_id },
  { field: 'hospital_id', served: (s) => s.hospital_id, authored: (a) => a.hospital_id },
  { field: 'accepts_insurance', served: (s) => s.accepts_insurance, authored: (a) => a.accepts_insurance },
  { field: 'in_network_for_plan', served: (s) => s.in_network_for_plan, authored: (a) => a.in_network_for_plan },
  { field: 'status', served: (s) => s.status, authored: (a) => a.status },
  { field: 'referral_required', served: (s) => s.referral_required, authored: (a) => a.referral_required },
  { field: 'prior_auth_required', served: (s) => s.prior_auth_required, authored: (a) => a.prior_auth_required },
  { field: 'conflict', served: (s) => s.conflict, authored: (a) => a.conflict },
  {
    field: 'conflicting_evidence_ids',
    served: (s) => s.conflicting_evidence_ids ?? [],
    authored: (a) => a.conflicting_evidence_ids,
  },
  { field: 'winning_evidence_id', served: (s) => s.winning_evidence_id, authored: (a) => a.winning_evidence_id },
  { field: 'freshness.state', served: (s) => s.freshness.state, authored: (a) => a.freshness.state },
  {
    field: 'freshness.age_seconds',
    served: (s) => s.freshness.age_seconds,
    authored: (a) => a.freshness.age_seconds,
    /* The authored coverage row was written moments after the evidence row it
       summarises, so the two ages agree to within seconds rather than exactly
       (…0013 differs by 10 s). The service reports the evidence's own recorded
       age — the only number in the dataset that is actually recorded — and the
       `state` above is compared exactly. */
    equal: (served, authored) => typeof served === 'number' && typeof authored === 'number' && Math.abs(served - authored) <= 60,
  },
  { field: 'evidence_ids', served: (s) => s.evidence_ids, authored: (a) => a.evidence_ids },
  { field: 'disclaimer_key', served: (s) => s.disclaimer_key, authored: (a) => a.disclaimer_key },
  { field: 'synthetic', served: (s) => s.synthetic, authored: (a) => a.synthetic },
];

/**
 * Fields the service derives differently from the authored row, each with the
 * evidence for the service's reading. Anything outside this table is a
 * regression; anything inside it is a fixture-side finding reported to the
 * fixtures owner rather than copied into the service.
 *
 *   80000000-…-0012 (`accepts_insurance`, `prior_auth_required`)
 *     The row reports both as null while the provider's own directory listing
 *     names the plan's payer ("Great Lakes Mutual") and the plan sets
 *     `requires_prior_auth_for_specialist` explicitly — and while coverage
 *     result …0005, the SAME plan …0005 with a different provider, reports
 *     `false` for the same plan-level field. Nulling a knowable fact is not the
 *     fixture set's house rule either: the network_conflict journey asserts
 *     coverage result …0002 has `accepts_insurance: true` even though its
 *     plan-level status is unknown. The service reports the facts it holds and
 *     leaves null for what nobody can know (no provider named → nothing and
 *     nobody to accept it).
 *
 *   80000000-…-0012 (`evidence_ids`)
 *     The row cites 50000000-…-0020 as its second source, but that row is
 *     provider …0005's directory record (case …0016's citation, correctly made
 *     there). No evidence row in the set names provider …0009 at all, so the
 *     trail the service can honestly serve is the network record's own evidence
 *     …0033 and nothing else. Citing …0005's record for a question about …0009
 *     would be a wrong attribution, not a fuller answer.
 *
 *   80000000-…-0009 (`prior_auth_required`)
 *     The hospital-level row reports null for the same plan-level field, for
 *     the same reason. Its `accepts_insurance: null` is correct and matched:
 *     with no provider named there is no directory listing to read.
 */
const DOCUMENTED_DIVERGENCES: Record<string, string[]> = {
  '80000000-0000-4000-8000-000000000012': ['accepts_insurance', 'prior_auth_required', 'evidence_ids'],
  '80000000-0000-4000-8000-000000000009': ['prior_auth_required'],
};

describe('coverage against the authored fixture rows', () => {
  it('answers every authored lookup with the authored facts', async () => {
    const h = await fixtureHarness();
    const unexpected: string[] = [];
    const healed: string[] = [];

    for (const row of AUTHORED) {
      const served = await lookup(h, row);
      const short = row.coverage_id.slice(-4);
      const documented = DOCUMENTED_DIVERGENCES[row.coverage_id] ?? [];
      const differing = FIELDS.filter((entry) => {
        const servedValue = entry.served(served);
        const authoredValue = entry.authored(row);
        return entry.equal ? !entry.equal(servedValue, authoredValue) : JSON.stringify(servedValue) !== JSON.stringify(authoredValue);
      });

      for (const entry of differing) {
        if (documented.includes(entry.field)) continue;
        unexpected.push(
          `${short}.${entry.field}: served ${JSON.stringify(entry.served(served))}, authored ${JSON.stringify(entry.authored(row))}`,
        );
      }
      for (const field of documented) {
        if (!differing.some((entry) => entry.field === field)) {
          healed.push(`${short}.${field}: the documented divergence is gone — the fixture may have been fixed`);
        }
      }
    }

    expect(unexpected, 'fields where the service disagrees with the authored row').toEqual([]);
    expect(healed, 'documented divergences that no longer diverge').toEqual([]);
  });

  it('answers the network_conflict journey the way the journey asserts', async () => {
    const h = await fixtureHarness();

    /* Assertion 1: the two facts are reported independently — a known payer
       relationship is not swallowed by an unknown network status. */
    const unknownCase = bySuffix('0002');
    const servedUnknown = await lookup(h, unknownCase);
    expect(servedUnknown.accepts_insurance).toBe(true);
    expect(servedUnknown.in_network_for_plan).toBeNull();
    expect(servedUnknown.status).toBe('unknown');

    /* Assertion 2: the conflict is named on both sides, and the arbitration
       points at the authoritative record — never at the superseded one. */
    const supersededEvidenceId = unknownCase.conflicting_evidence_ids[0]!;
    const authoritativeEvidenceId = unknownCase.conflicting_evidence_ids[1]!;
    expect(servedUnknown.conflict).toBe(true);
    expect(servedUnknown.conflicting_evidence_ids).toEqual([supersededEvidenceId, authoritativeEvidenceId]);
    expect(servedUnknown.winning_evidence_id).toBe(authoritativeEvidenceId);
    expect(servedUnknown.winning_evidence_id).not.toBe(supersededEvidenceId);
    expect(servedUnknown.evidence_ids).toContain(supersededEvidenceId);

    /* Assertion 5: the same plan yields a plain out-of-network answer elsewhere,
       so "unknown" is not the only shape the service has. */
    const outOfNetwork = await lookup(h, bySuffix('0003'));
    expect(outOfNetwork.status).toBe('out_of_network');
    expect(outOfNetwork.in_network_for_plan).toBe(false);
    expect(outOfNetwork.conflict).toBe(false);
    expect(outOfNetwork.winning_evidence_id).toBeNull();

    /* Assertion 4: the hospital-level conflict — a stale public description
       against the hospital's structured record. No provider is named, so no
       plan-level record exists and the answer says so instead of guessing. */
    const hospitalLevel = await lookup(h, bySuffix('0009'));
    expect(hospitalLevel.provider_id).toBeNull();
    expect(hospitalLevel.status).toBe('unknown');
    expect(hospitalLevel.accepts_insurance).toBeNull();
    expect(hospitalLevel.in_network_for_plan).toBeNull();
    expect(hospitalLevel.conflict).toBe(true);
    expect(hospitalLevel.freshness.state).toBe('stale');
    expect(hospitalLevel.winning_evidence_id).toBe(bySuffix('0009').winning_evidence_id);
  });

  it('serves the hospital-level answer through the query-parameter spelling too', async () => {
    const h = await fixtureHarness();
    const row = bySuffix('0009');
    const posted = await lookup(h, row);
    const params = new URLSearchParams({ plan_id: row.plan_id, hospital_id: String(row.hospital_id) });
    const fetched = await call(h.app, { method: 'GET', url: `/coverage?${params.toString()}` });
    expect(fetched.status).toBe(200);
    expect(fetched.body).toEqual(posted);
  });

  it('refuses a lookup that names neither a provider nor a hospital', async () => {
    const h = await fixtureHarness();
    const response = await call(h.app, {
      method: 'POST',
      url: '/coverage/lookup',
      payload: { plan_id: bySuffix('0001').plan_id },
    });
    expect(response.status).toBe(400);
    expect((response.body as { error: { code: string } }).error.code).toBe('missing_lookup_subject');
  });

  it('reports a conflict on a clean answer only while the fault is armed', async () => {
    const h = await fixtureHarness();
    const clean = bySuffix('0001');

    const before = await lookup(h, clean);
    expect(before.conflict).toBe(false);
    expect(before.freshness.state).toBe('fresh');

    const armed = await call(h.app, { method: 'POST', url: '/_faults/arm', payload: { scenario_id: 'stale_coverage_evidence' } });
    expect(armed.status).toBe(200);
    const during = await lookup(h, clean);
    expect(during.conflict).toBe(true);
    expect(during.freshness.state).toBe('stale');
    /* The facts themselves do not move: a stale source is a conflict about the
       same network answer, not a different answer. */
    expect(during.status).toBe(before.status);
    expect(during.in_network_for_plan).toBe(before.in_network_for_plan);

    const reset = await call(h.app, { method: 'POST', url: '/_faults/reset' });
    expect(reset.status).toBe(200);
    await call(h.app, { method: 'POST', url: '/_faults/arm', payload: { mode: 'off' } });
    const after = await lookup(h, clean);
    expect(after.conflict).toBe(false);
  });
});
