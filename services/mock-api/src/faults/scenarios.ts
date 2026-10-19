import { readdirSync, readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { FAULT_TYPES, type FaultType } from '../contracts/enums.js';
import { canonicalRoute } from './routes.js';
import type { FaultJourneyMeta, FaultManifestMeta, FaultScenario, FaultStep, FaultVariantMeta } from './types.js';

/**
 * A complete built-in scenario per fault type.
 *
 * The fixtures agent owns `fixtures/fault-scenarios/*.json` at the repository
 * root and files found there always win. These built-ins exist so the service
 * (and its test suite) behaves identically when that directory is empty or
 * partially populated — a missing fixture must never turn into "the fault
 * silently cannot be triggered".
 */
function step(path: string, method: string, occurrence: number, fault: FaultType, delay_ms?: number): FaultStep {
  return {
    match: { method, path },
    occurrence,
    fault: delay_ms === undefined ? fault : { type: fault, delay_ms },
  };
}

export function builtInScenarios(): FaultScenario[] {
  return [
    {
      scenario_id: 'latency',
      description: 'Upstream answers correctly, but slowly enough to be perceived as a stall.',
      fault_seed: 42,
      default_fault: 'latency',
      steps: [
        step('/appointments', 'POST', 1, 'latency', 1200),
        step('/availability', 'POST', 1, 'latency', 1200),
      ],
    },
    {
      scenario_id: 'rate_limit_429',
      description: 'The hospital edge rate-limits the caller. Nothing was booked.',
      fault_seed: 42,
      default_fault: 'rate_limit_429',
      steps: [step('/appointments', 'POST', 1, 'rate_limit_429')],
    },
    {
      scenario_id: 'service_unavailable_503',
      description: 'The hospital edge is down. Never an empty result — always a failure.',
      fault_seed: 42,
      default_fault: 'service_unavailable_503',
      steps: [
        step('/appointments', 'POST', 1, 'service_unavailable_503'),
        step('/availability', 'POST', 1, 'service_unavailable_503'),
      ],
    },
    {
      scenario_id: 'malformed_schema',
      description: 'The upstream answered 200 with a payload that does not match its own schema.',
      fault_seed: 42,
      default_fault: 'malformed_schema',
      steps: [step('/availability', 'POST', 1, 'malformed_schema')],
    },
    {
      scenario_id: 'empty_valid_result',
      description: 'A genuine, valid, empty answer: the constraints really do match nothing.',
      fault_seed: 42,
      default_fault: 'empty_valid_result',
      steps: [step('/availability', 'POST', 1, 'empty_valid_result')],
    },
    {
      scenario_id: 'stale_slot_409',
      description: 'The caller books against a slot version that has moved on.',
      fault_seed: 42,
      default_fault: 'stale_slot_409',
      steps: [step('/appointments', 'POST', 1, 'stale_slot_409')],
    },
    {
      scenario_id: 'timeout_before_commit',
      description: 'The request times out BEFORE anything was written upstream. No booking exists afterwards.',
      fault_seed: 42,
      default_fault: 'timeout_before_commit',
      steps: [step('/appointments', 'POST', 1, 'timeout_before_commit', 30_000)],
    },
    {
      scenario_id: 'timeout_after_commit',
      description: 'The request times out AFTER the upstream committed. A booking EXISTS afterwards and must be reconciled with the same key.',
      fault_seed: 42,
      default_fault: 'timeout_after_commit',
      steps: [step('/appointments', 'POST', 1, 'timeout_after_commit', 30_000)],
    },
    {
      scenario_id: 'duplicate_event',
      description: 'The hospital delivers the same booking event twice for one idempotency key.',
      fault_seed: 42,
      default_fault: 'duplicate_event',
      steps: [step('/appointments', 'POST', 1, 'duplicate_event')],
    },
    {
      scenario_id: 'stale_coverage_evidence',
      description: 'The coverage record is stale and conflicts with the plan data. Shown as stale, never smoothed over.',
      fault_seed: 42,
      default_fault: 'stale_coverage_evidence',
      steps: [step('/coverage/lookup', 'POST', 1, 'stale_coverage_evidence')],
    },
    {
      scenario_id: 'notification_failure',
      description: 'The confirmation notification cannot be delivered. The booking is untouched; the entry goes terminal instead of looping.',
      fault_seed: 42,
      default_fault: 'notification_failure',
      steps: [step('/internal/notifications/send', 'POST', 1, 'notification_failure')],
    },
  ];
}

export interface ScenarioLoadResult {
  scenarios: FaultScenario[];
  source: 'fixtures' | 'builtin' | 'mixed';
  directory: string;
  /** Journey id declared by each fixture file, in file order. */
  fixtureIds: string[];
  errors: Array<{ file: string; message: string }>;
  variants: FaultVariantMeta[];
  journeys: FaultJourneyMeta[];
  manifest: FaultManifestMeta;
  /** Built-in ids a fixture alias took over. */
  shadowed: string[];
}

interface LoadedFile {
  file: string;
  scenario: FaultScenario;
  journey: string;
  injection: string;
}

function stemOf(file: string): string {
  return file.replace(/\.json$/i, '');
}

/** The fault a fixture file injects: its `default_fault`, else its file name. */
function injectionOf(parsed: FaultScenario, file: string): string {
  return typeof parsed.default_fault === 'string' ? parsed.default_fault : stemOf(file);
}

/**
 * Reads `fixtures/scenarios.json`, the machine-readable index of the demo
 * scenarios. It is the authority on which fault files belong to which journey —
 * four files declare `three_transient_errors`, three declare
 * `timeout_after_commit`, two declare `standard_booking`, and the ids are the
 * journey names rather than the fault names.
 */
function loadManifest(faultDirectory: string): { path: string; byFile: Map<string, string>; files: string[] } | null {
  const path = join(faultDirectory, '..', 'scenarios.json');
  if (!existsSync(path)) return null;
  try {
    const parsed = JSON.parse(readFileSync(path, 'utf8')) as {
      scenarios?: Array<{ scenario_id?: unknown; fault_scenarios?: unknown }>;
    };
    const byFile = new Map<string, string>();
    const files: string[] = [];
    for (const scenario of parsed.scenarios ?? []) {
      if (typeof scenario?.scenario_id !== 'string' || !Array.isArray(scenario.fault_scenarios)) continue;
      for (const file of scenario.fault_scenarios) {
        if (typeof file !== 'string') continue;
        byFile.set(file, scenario.scenario_id);
        files.push(file);
      }
    }
    return byFile.size > 0 ? { path, byFile, files } : null;
  } catch {
    return null;
  }
}

/**
 * The route a step targets, canonically.
 *
 * Canonical because both the grouping and the "is this route already covered?"
 * question below have to see `GET /availability` and `POST /availability` as one
 * route, or the union would emit the same fault twice and the occurrence
 * numbers would stop meaning anything.
 */
function matchKeyOf(step: FaultStep): string {
  return canonicalRoute(step.match.method ?? '*', step.match.path ?? '*');
}

/** Tags a step with the seed of the file it was authored in. */
function seedOf(step: FaultStep, scenario: FaultScenario): FaultStep {
  return scenario.fault_seed === undefined ? step : { ...step, seed: scenario.fault_seed };
}

/**
 * Unions the variants of one journey into the single scenario the bare journey
 * id resolves to.
 *
 * The preference order matters. A variant whose file name *is* the journey name
 * wins (that is the canonical injection, e.g. `timeout_after_commit.json` for
 * `timeout_after_commit`); otherwise the widest variant wins — for
 * `three_transient_errors` that is `service_unavailable_503.json`, whose three
 * steps are the cumulative retry ladder the other files repeat as a prefix.
 * Steps that only some variant targets (the malformed directory search) are
 * appended rather than dropped, because dropping one would make that fault
 * unreachable through the journey id.
 */
function unionJourney(journey: string, variants: LoadedFile[], declaredByManifest: boolean): FaultScenario {
  const preferred =
    variants.find((variant) => stemOf(variant.file) === journey) ??
    [...variants].sort((left, right) => (right.scenario.steps?.length ?? 0) - (left.scenario.steps?.length ?? 0) || left.file.localeCompare(right.file))[0]!;

  const covered = new Set((preferred.scenario.steps ?? []).map(matchKeyOf));
  const extra: FaultStep[] = [];
  for (const variant of [...variants].sort((left, right) => left.file.localeCompare(right.file))) {
    for (const step of variant.scenario.steps ?? []) {
      const key = matchKeyOf(step);
      if (covered.has(key)) continue;
      covered.add(key);
      extra.push(seedOf(step, variant.scenario));
    }
  }

  return {
    scenario_id: journey,
    description: preferred.scenario.description,
    fault_seed: preferred.scenario.fault_seed,
    default_fault: preferred.scenario.default_fault,
    steps: [...(preferred.scenario.steps ?? []).map((step) => seedOf(step, preferred.scenario)), ...extra],
    journey,
    injection: preferred.injection,
    source_file: preferred.file,
    variant_ids: variants.map((variant) => variant.scenario.scenario_id),
  };
}

/**
 * The fault NAME resolves to the fixture that injects it, plus whatever the
 * built-in scenario of the same name says that the fixture does not.
 *
 * Arming a fault by name is a promise about the fault rather than about one
 * route. The fixture file is the author's account of where that fault lands in
 * its journey; the built-in is this service's account of the same fault. Where
 * the fixture is silent the built-in is not a guess — it is the only other
 * description of the fault, and dropping it would make the fault unreachable on
 * a route the contract still exposes and the fixtures still assert: the
 * availability answer to a malformed upstream page, for one, whose fault file
 * was authored against the directory search instead.
 *
 * The fixture wins wherever both name the same route — including the seed that
 * travels with the step, so a reproducer still gets the authored numbers — and
 * a step-less file keeps its "this fault, everywhere" reading rather than
 * inheriting a narrower set of routes from the built-in.
 */
function withBuiltInCoverage(variant: FaultScenario, builtIn: FaultScenario | undefined, alias: string): FaultScenario {
  const named: FaultScenario = { ...variant, scenario_id: alias, aliases: [alias] };
  const steps = variant.steps ?? [];
  if (!builtIn || steps.length === 0) return named;

  const covered = new Set(steps.map(matchKeyOf));
  const extra: FaultStep[] = [];
  for (const step of builtIn.steps ?? []) {
    const key = matchKeyOf(step);
    if (covered.has(key)) continue;
    covered.add(key);
    extra.push(seedOf(step, builtIn));
  }
  return extra.length === 0 ? named : { ...named, steps: [...steps, ...extra] };
}

/**
 * Loads `fixtures/fault-scenarios/*.json`.
 *
 * A fixture file is a *fault injection into a demo journey*, not a scenario in
 * its own right: the id names the journey and several files may carry it. So
 * every file lands under its own id (`journey/injection`), the journey id
 * resolves to the union of its files, and the fault name (`latency`, …) is an
 * alias onto the fixture that injects it — which means arming a fault by name
 * reproduces the authored numbers rather than a built-in approximation.
 *
 * A malformed file is reported but never fatal, and a missing fixture
 * directory falls back to the built-ins, so a broken fixture cannot disable
 * fault coverage.
 */
export function loadScenarios(directory: string): ScenarioLoadResult {
  const byId = new Map<string, FaultScenario>(builtInScenarios().map((scenario) => [scenario.scenario_id, scenario]));
  const fixtureIds: string[] = [];
  const errors: Array<{ file: string; message: string }> = [];
  const loaded: LoadedFile[] = [];

  if (existsSync(directory)) {
    let entries: string[] = [];
    try {
      entries = readdirSync(directory).filter((name) => name.endsWith('.json'));
    } catch (error) {
      errors.push({ file: directory, message: error instanceof Error ? error.message : String(error) });
    }
    for (const entry of entries.sort()) {
      try {
        const parsed = JSON.parse(readFileSync(join(directory, entry), 'utf8')) as FaultScenario;
        if (!parsed || typeof parsed.scenario_id !== 'string') {
          throw new Error('missing scenario_id');
        }
        if (!FAULT_TYPES.includes(parsed.default_fault as FaultType) && !parsed.steps?.length) {
          throw new Error('scenario declares neither a valid default_fault nor any steps');
        }
        loaded.push({ file: entry, scenario: parsed, journey: parsed.scenario_id, injection: injectionOf(parsed, entry) });
        fixtureIds.push(parsed.scenario_id);
      } catch (error) {
        errors.push({ file: entry, message: error instanceof Error ? error.message : String(error) });
      }
    }
  }

  const manifest = loadManifest(directory);
  if (manifest) {
    for (const entry of loaded) {
      const declared = manifest.byFile.get(entry.file);
      if (declared) entry.journey = declared;
    }
  }

  // Group by journey, then register: variants, then journey unions, then
  // aliases. An alias may take over a built-in id (fixtures win) but never a
  // fixture id, so a journey name can never be shadowed by a fault name.
  const groups = new Map<string, LoadedFile[]>();
  for (const entry of loaded) {
    const list = groups.get(entry.journey);
    if (list) list.push(entry);
    else groups.set(entry.journey, [entry]);
  }

  const variants: FaultVariantMeta[] = [];
  const journeys: FaultJourneyMeta[] = [];
  for (const [journey, files] of [...groups.entries()].sort(([left], [right]) => left.localeCompare(right))) {
    const variantScenarios: FaultScenario[] = [];
    for (const entry of files) {
      const id = entry.injection === journey ? journey : `${journey}/${entry.injection}`;
      const variant: FaultScenario = {
        ...entry.scenario,
        scenario_id: id,
        journey,
        injection: entry.injection,
        source_file: entry.file,
      };
      byId.set(id, variant);
      variantScenarios.push(variant);
      variants.push({
        id,
        journey,
        injection: entry.injection,
        source_file: entry.file,
        fault_seed: entry.scenario.fault_seed ?? null,
        steps: entry.scenario.steps?.length ?? 0,
      });
    }
    const union = unionJourney(journey, files, Boolean(manifest));
    byId.set(journey, union);
    journeys.push({
      journey,
      files: files.map((entry) => entry.file),
      variant_ids: variantScenarios.map((variant) => variant.scenario_id),
      preferred_file: union.source_file ?? files[0]!.file,
      declared_by_manifest: Boolean(manifest?.byFile.has(files[0]!.file)),
    });
  }

  const fixtureIdsInPlay = new Set([...variants.map((variant) => variant.id), ...journeys.map((entry) => entry.journey)]);
  const shadowed: string[] = [];
  for (const entry of loaded) {
    const alias = entry.injection;
    if (fixtureIdsInPlay.has(alias)) continue;
    let shadowedBuiltIn = false;
    if (byId.has(alias) && !variants.some((variant) => variant.id === alias) && !journeys.some((journey) => journey.journey === alias)) {
      shadowed.push(alias);
      shadowedBuiltIn = true;
    }
    const target = byId.get(`${entry.journey}/${alias}`) ?? byId.get(entry.journey);
    if (!target) continue;
    byId.set(alias, withBuiltInCoverage(target, shadowedBuiltIn ? byId.get(alias) : undefined, alias));
  }

  const scenarios = [...byId.values()].sort((left, right) => left.scenario_id.localeCompare(right.scenario_id));
  const source: ScenarioLoadResult['source'] =
    fixtureIds.length === 0 ? 'builtin' : scenarios.length > fixtureIds.length ? 'mixed' : 'fixtures';

  const manifestFiles = manifest?.files ?? [];
  const present = new Set(loaded.map((entry) => entry.file));
  const referenced = new Set(manifestFiles);

  return {
    scenarios,
    source,
    directory,
    fixtureIds,
    errors,
    variants,
    journeys,
    manifest: {
      path: manifest?.path ?? join(directory, '..', 'scenarios.json'),
      found: Boolean(manifest),
      complete: manifestFiles.every((file) => present.has(file)),
      missing_files: manifestFiles.filter((file) => !present.has(file)),
      unreferenced_files: [...present].filter((file) => !referenced.has(file)).sort(),
    },
    shadowed,
  };
}
