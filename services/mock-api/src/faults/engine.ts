import { FAULT_TYPES, type FaultType } from '../contracts/enums.js';
import type { MockApiConfig } from '../config.js';
import type { Store } from '../db/store.js';
import { drawChance, drawIndex } from './rng.js';
import { canonicalRoute, normalizePath, routeMatches } from './routes.js';
import { loadScenarios, type ScenarioLoadResult } from './scenarios.js';
import { DEFAULT_DELAY_MS, type FaultDecision, type FaultMode, type FaultScenario, type FaultSpec, type FaultStep } from './types.js';

export interface FaultRequestInput {
  method: string;
  path: string;
  headers: Record<string, string | string[] | undefined>;
  query: Record<string, unknown>;
}

/** Paths that are never faulted: the control plane must stay usable. */
/**
 * Paths a fault never touches, whatever is armed.
 *
 * The list is an operational requirement, not a courtesy: if the scenario you
 * need in order to DISARM a blanket fault is itself faulted, or the probe the
 * container health check runs (`curl -fsS /healthz`, the compose spelling in
 * ADR-0023) is rate limited, the demo loses the surface it needs to recover.
 */
const NEVER_FAULTED = [/^\/$/, /^\/health$/, /^\/healthz$/, /^\/openapi\.json$/, /^\/_faults/, /^\/_diagnostics/];

function headerValue(headers: FaultRequestInput['headers'], name: string): string | undefined {
  const raw = headers[name] ?? headers[name.toLowerCase()];
  if (Array.isArray(raw)) return raw[0];
  return raw;
}

function isFaultType(value: unknown): value is FaultType {
  return typeof value === 'string' && (FAULT_TYPES as readonly string[]).includes(value);
}

function stepRouteKey(step: FaultStep): string {
  return canonicalRoute(step.match.method ?? '*', step.match.path ?? '*');
}

/**
 * A step's effective occurrence.
 *
 * Normally the declared value. When an earlier step in the same scenario names
 * the same route at the same occurrence, the two are consecutive events rather
 * than one: `duplicate_event` declares the lost response and the duplicate
 * delivery both at occurrence 1, and reads them as "this request, then the
 * next one" — which is what the fixture's description says happens.
 */
function effectiveOccurrence(steps: FaultStep[], index: number, declared: number | undefined): number | undefined {
  if (declared === undefined) return undefined;
  const step = steps[index]!;
  const route = stepRouteKey(step);
  let effective = declared;
  for (let earlier = 0; earlier < index; earlier += 1) {
    const candidate = steps[earlier]!;
    if (stepRouteKey(candidate) !== route) continue;
    if (candidate.occurrence !== effective) continue;
    effective += 1;
  }
  return effective;
}

function toSpec(step: FaultStep | undefined, fallback: FaultType | undefined): FaultSpec | null {
  const raw = step?.fault;
  if (typeof raw === 'string') return { type: raw, delay_ms: DEFAULT_DELAY_MS[raw] };
  if (raw && typeof raw === 'object' && isFaultType(raw.type)) {
    const retryAfterSeconds =
      typeof raw.retry_after_seconds === 'number'
        ? raw.retry_after_seconds
        : typeof raw.retry_after_s === 'number'
          ? raw.retry_after_s
          : undefined;
    return {
      ...raw,
      type: raw.type,
      delay_ms: raw.delay_ms ?? DEFAULT_DELAY_MS[raw.type],
      ...(retryAfterSeconds === undefined ? {} : { retry_after_seconds: retryAfterSeconds }),
    };
  }
  if (isFaultType(fallback)) {
    return { type: fallback, delay_ms: DEFAULT_DELAY_MS[fallback] };
  }
  return null;
}

/**
 * Decides whether a request (or an internal operation) is faulted, why, and
 * with which parameters — reproducibly.
 *
 * Reproducibility contract: for a given `MOCK_FAULT_SEED` + `scenario_id` +
 * request sequence the decision is identical on every run, including across a
 * process restart, because the per-(scenario, method, path, step) occurrence
 * counter lives in SQLite and the random mode draws from a pure function of
 * (seed, sequence) rather than from a stateful generator.
 */
export class FaultEngine {
  private readonly config: MockApiConfig;
  private readonly store: Store;
  readonly load: ScenarioLoadResult;
  private armedScenarioId: string | null;
  private armedMode: FaultMode;

  constructor(options: { config: MockApiConfig; store: Store }) {
    this.config = options.config;
    this.store = options.store;
    this.load = loadScenarios(options.config.faultScenarioDir);
    this.armedScenarioId = process.env.MOCK_API_FAULT_SCENARIO?.trim() || null;
    const envMode = process.env.MOCK_API_FAULT_MODE?.trim();
    this.armedMode = envMode === 'random' || envMode === 'off' || envMode === 'scenario' ? envMode : 'scenario';
  }

  get seed(): number {
    return this.config.faultSeed;
  }

  get scenarios(): FaultScenario[] {
    return this.load.scenarios;
  }

  arm(scenarioId: string | null, mode: FaultMode = 'scenario'): void {
    this.armedScenarioId = scenarioId;
    this.armedMode = mode;
  }

  get armed(): { scenario_id: string | null; mode: FaultMode } {
    return { scenario_id: this.armedScenarioId, mode: this.armedMode };
  }

  /** Decides the fault for an inbound HTTP request, if any. */
  beginRequest(input: FaultRequestInput): FaultDecision | null {
    if (!this.config.faultsEnabled) return null;
    const method = input.method.toUpperCase();
    const path = normalizePath(input.path);
    if (NEVER_FAULTED.some((pattern) => pattern.test(path))) return null;

    const requestSeq = this.store.nextRequestSequence();
    const headerMode = headerValue(input.headers, 'x-fault-mode');
    const mode: FaultMode =
      headerMode === 'random' || headerMode === 'off' || headerMode === 'scenario' ? headerMode : this.armedMode;
    if (mode === 'off') return null;

    // One-off override: `x-fault: service_unavailable_503`.
    const explicitFault = headerValue(input.headers, 'x-fault');
    if (isFaultType(explicitFault)) {
      const spec: FaultSpec = {
        type: explicitFault,
        delay_ms: DEFAULT_DELAY_MS[explicitFault],
        retry_after_seconds: explicitFault === 'rate_limit_429' ? 5 : undefined,
      };
      this.record(null, spec.type, method, path, null, requestSeq, 'scenario', this.config.faultSeed);
      return {
        mode: 'scenario',
        scenario_id: null,
        fault: spec,
        step_index: null,
        occurrence: null,
        request_seq: requestSeq,
        seed: this.config.faultSeed,
        match_key: canonicalRoute(method, path),
      };
    }

    if (mode === 'random') {
      const decision = this.randomDecision(method, path, requestSeq);
      if (decision) this.record(null, decision.fault.type, method, path, null, requestSeq, 'random', decision.seed);
      return decision;
    }

    const scenarioId = headerValue(input.headers, 'x-fault-scenario') ?? (input.query.fault_scenario as string | undefined) ?? this.armedScenarioId;
    if (!scenarioId) return null;
    const scenario = this.scenarios.find((entry) => entry.scenario_id === scenarioId);
    if (!scenario) return null;

    const decision = this.matchScenario(scenario, method, path, requestSeq, true);
    if (decision) {
      this.record(scenario.scenario_id, decision.fault.type, method, path, decision.occurrence, requestSeq, 'scenario', decision.seed);
    }
    return decision;
  }

  /**
   * Fault decision for an internal hop (the adapter's upstream call, the
   * outbox sender). Inside a request the request's own decision wins, so a
   * scenario armed through a header also drives the hops it triggers.
   */
  internalDecision(
    operation: string,
    requestDecision: FaultDecision | null,
    applies?: (decision: FaultDecision) => boolean,
  ): FaultDecision | null {
    if (!this.config.faultsEnabled) return null;
    const [method, path] = operation.split(' ');
    if (!method || !path) return null;

    // When the request was already answered for this very route, that answer IS
    // this hop's answer. `POST /notifications` and the sender's internal hop are
    // one folded route, so counting again would make the second look like the
    // next attempt of a fault that already fired — the fixture says the
    // confirmation cannot be delivered, and it means this delivery.
    if (requestDecision && requestDecision.match_key === canonicalRoute(method, path)) {
      return applies && !applies(requestDecision) ? null : requestDecision;
    }

    const scenarioId = requestDecision?.scenario_id ?? this.armedScenarioId;
    if (!scenarioId) return null;
    const scenario = this.scenarios.find((entry) => entry.scenario_id === scenarioId);
    if (!scenario) return null;
    const requestSeq = requestDecision?.request_seq ?? this.store.nextRequestSequence();

    // Peek before consuming. An occurrence belongs to an attempt the fault is
    // actually about: a scenario that names `channel: "sms"` must not spend its
    // single occurrence on the email leg it does not touch, and must not fail
    // that leg either. Nothing is recorded for an attempt the fault skips — the
    // delivery that succeeded is its own evidence.
    const preview = this.matchScenario(scenario, method, path, requestSeq, false);
    if (!preview) return null;
    if (applies && !applies(preview)) return null;

    const occurrence = this.store.nextOccurrence(scenario.scenario_id, preview.match_key);
    const decision: FaultDecision = { ...preview, occurrence };
    this.record(scenario.scenario_id, decision.fault.type, method, path, decision.occurrence, requestSeq, 'scenario', decision.seed);
    return decision;
  }

  /**
   * Gives a decision back unused.
   *
   * The caller is a route that discovered the fault is not about this request
   * after all — the fixture named a subject, and the request named another. The
   * occurrence returns to the counter and the offered decision leaves no trace
   * in the fault log, because nothing was observed: the request is answered
   * normally, and the next request about the subject the fixture names still
   * gets the fault it authored.
   */
  release(decision: FaultDecision | null): void {
    if (!decision) return;
    if (decision.mode !== 'scenario' || !decision.scenario_id || decision.occurrence === null) return;
    this.store.releaseOccurrence(decision.scenario_id, decision.match_key, {
      requestSeq: decision.request_seq,
      occurrence: decision.occurrence,
      faultType: decision.fault.type,
    });
  }

  reset(): void {
    this.store.resetFaultState();
  }

  describeState(): Record<string, unknown> {
    return {
      seed: this.config.faultSeed,
      enabled: this.config.faultsEnabled,
      armed: this.armed,
      scenario_source: this.load.source,
      scenario_directory: this.load.directory,
      fixture_scenarios: this.load.fixtureIds,
      load_errors: this.load.errors,
      manifest: this.load.manifest,
      journeys: this.load.journeys,
      variants: this.load.variants,
      scenarios: this.scenarios.map((scenario) => ({
        scenario_id: scenario.scenario_id,
        description: scenario.description,
        fault_seed: scenario.fault_seed ?? this.config.faultSeed,
        default_fault: scenario.default_fault ?? null,
        journey: scenario.journey ?? null,
        injection: scenario.injection ?? null,
        source_file: scenario.source_file ?? null,
        steps: scenario.steps?.length ?? 0,
      })),
      occurrences: this.store.listFaultEvents().map((row) => ({
        scenario_id: row.scenario_id,
        fault_type: row.fault_type,
        method: row.method,
        path: row.path,
        occurrence: row.occurrence,
        // The seed travels with the fault: it is what makes the event
        // reproducible rather than merely logged.
        seed: row.seed,
        request_seq: row.request_seq,
        mode: row.mode,
        created_at: row.created_at,
      })),
    };
  }

  /* --------------------------------------------------------------- internals */

  private matchScenario(
    scenario: FaultScenario,
    method: string,
    path: string,
    requestSeq: number,
    countOccurrences: boolean,
  ): FaultDecision | null {
    const steps = scenario.steps ?? [];

    // Which of this scenario's steps concern this route at all. The counter is
    // per (scenario, route) — the Nth request to this route — and is
    // incremented ONCE per request, because that is what a fixture's
    // `occurrence` means: the `three_transient_errors` ladder is step 1, 2 and 3
    // of the same endpoint, and a per-step counter could never reach 2. The
    // route is canonical, so a fixture written against one spelling of an
    // endpoint still counts and fires on the other.
    const routeKey = canonicalRoute(method, path);
    const candidates = steps.map((step, index) => ({ step, index })).filter(({ step }) => routeMatches(step, method, path));

    if (candidates.length > 0) {
      const occurrence = countOccurrences
        ? this.store.nextOccurrence(scenario.scenario_id, routeKey)
        : this.store.peekOccurrence(scenario.scenario_id, routeKey) + 1;

      for (const { step, index } of candidates) {
        // Two steps naming the same route at the same occurrence cannot both be
        // the same event, so the later one is read as the next event: that is
        // how `duplicate_event` says "the confirmation arrives twice".
        const wantOccurrence = effectiveOccurrence(steps, index, step.occurrence);
        if (wantOccurrence !== undefined && occurrence !== wantOccurrence) continue;
        const spec = toSpec(step, scenario.default_fault);
        if (!spec) continue;
        return {
          mode: 'scenario',
          scenario_id: scenario.scenario_id,
          fault: spec,
          step_index: index,
          occurrence,
          request_seq: requestSeq,
          // The step's own seed wins when several files were unioned into one
          // journey: a reproducer needs the seed of the file that authored the
          // step, not the journey's first one.
          seed: step.seed ?? scenario.fault_seed ?? this.config.faultSeed,
          match_key: routeKey,
        };
      }
    }

    // A scenario with no steps at all is a whole-surface fault named by its
    // `default_fault`. When steps exist they are the complete description of
    // where the fault lands: a step-less guess must never widen the blast
    // radius to endpoints the author did not name.
    if (steps.length === 0 && scenario.default_fault && isFaultType(scenario.default_fault)) {
      return {
        mode: 'scenario',
        scenario_id: scenario.scenario_id,
        fault: { type: scenario.default_fault, delay_ms: DEFAULT_DELAY_MS[scenario.default_fault] },
        step_index: null,
        occurrence: countOccurrences ? this.store.nextOccurrence(scenario.scenario_id, `${routeKey} #default`) : null,
        request_seq: requestSeq,
        seed: scenario.fault_seed ?? this.config.faultSeed,
        match_key: `${routeKey} #default`,
      };
    }
    return null;
  }

  /**
   * Seeded random mode. Roughly one request in four is faulted, and the fault
   * is drawn from the canonical fault order using the same (seed, sequence)
   * function every time — so replaying the same request sequence with the same
   * seed reproduces the same failures exactly.
   */
  private randomDecision(method: string, path: string, requestSeq: number): FaultDecision | null {
    if (!drawChance(this.config.faultSeed, requestSeq, 1, 4, `${method} ${path}`)) return null;
    const index = drawIndex(this.config.faultSeed, requestSeq, FAULT_TYPES.length, `${method} ${path}`);
    const type = FAULT_TYPES[index]!;
    return {
      mode: 'random',
      scenario_id: null,
      fault: {
        type,
        delay_ms: DEFAULT_DELAY_MS[type],
        retry_after_seconds: type === 'rate_limit_429' ? 5 : undefined,
      },
      step_index: null,
      occurrence: null,
      request_seq: requestSeq,
      seed: this.config.faultSeed,
      match_key: canonicalRoute(method, path),
    };
  }

  private record(
    scenarioId: string | null,
    faultType: FaultType,
    method: string,
    path: string,
    occurrence: number | null,
    requestSeq: number,
    mode: string,
    seed: number,
  ): void {
    this.store.recordFaultEvent({
      scenarioId,
      faultType,
      method,
      path,
      occurrence,
      requestSeq,
      // The seed that produced THIS decision, which under the shared fixtures is
      // the seed the fixture file declares rather than the process-wide default:
      // a log that reports the wrong seed is a log that cannot be replayed.
      seed,
      mode,
      createdAt: new Date().toISOString(),
    });
  }
}
