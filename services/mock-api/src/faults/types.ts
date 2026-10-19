import type { FaultType } from '../contracts/enums.js';

/**
 * The `fault` object inside a scenario step.
 *
 * The fixtures carry more than a type and a delay: they name the channel that
 * failed, the slot version that went stale, the record that superseded the one
 * the caller saw. None of that is discarded — the extra keys are carried
 * through verbatim and surfaced as `detail.fault_params`, so an authored hint
 * stays visible to whoever is reproducing the run, even when this service does
 * not act on it.
 */
export interface FaultSpec {
  type: FaultType;
  delay_ms?: number;
  retry_after_seconds?: number;
  /** Free-form detail echoed into the error envelope's `detail` field. */
  detail?: string;
  /** The fixtures' spelling of `retry_after_seconds`. */
  retry_after_s?: number;
  /** Sent as the `Retry-After` header when the fixture specifies one. */
  retry_after_header?: string;
  /** Fixture hint: what the caller is expected to fall back to. Surfaced verbatim. */
  degrade_to?: string;
  /**
   * `timeout_before_commit` only. `true` means the hospital DID commit on this
   * occurrence, so the request is answered normally instead of timing out.
   */
  committed?: boolean;
  /** Every other fixture-authored hint. */
  [key: string]: unknown;
}

export interface FaultStepMatch {
  method?: string;
  path?: string;
}

export interface FaultStep {
  match: FaultStepMatch;
  /** 1-based. Absent means "every matching request". */
  occurrence?: number;
  /** Either a full spec or a bare fault name; both appear in fixture files. */
  fault?: FaultSpec | FaultType;
  /**
   * The seed of the fixture file this step came from. Set when the steps of
   * several files are unioned into one journey, so a step still reports the
   * seed that makes *it* reproducible rather than the journey's first seed.
   */
  seed?: number;
}

/** Exactly the shape produced by the fixtures agent in `fixtures/fault-scenarios/*.json`. */
export interface FaultScenario {
  scenario_id: string;
  description?: string;
  fault_seed?: number;
  default_fault?: FaultType;
  steps?: FaultStep[];
  /**
   * Fixture provenance. Several fixture files may declare the same
   * `scenario_id`: the id names the *journey* (a demo scenario such as
   * `three_transient_errors`), while each file injects one fault into it. Those
   * fields keep the distinction visible instead of collapsing it.
   */
  journey?: string;
  /** The fault this file injects (`default_fault`, else the file stem). */
  injection?: string;
  /** Basename of the fixture file this scenario was loaded from. */
  source_file?: string;
  /** Ids that also resolve to this scenario. */
  aliases?: string[];
  /** For a journey: the variant ids that were unioned into it. */
  variant_ids?: string[];
}

/** One loaded fixture file, as loaded. */
export interface FaultVariantMeta {
  id: string;
  journey: string;
  injection: string;
  source_file: string;
  fault_seed: number | null;
  steps: number;
}

/** One journey: the demo scenario and the fixture files that inject into it. */
export interface FaultJourneyMeta {
  journey: string;
  files: string[];
  variant_ids: string[];
  /** The variant the bare journey id resolves to. */
  preferred_file: string;
  /** True when the grouping came from `fixtures/scenarios.json`. */
  declared_by_manifest: boolean;
}

/**
 * `fixtures/scenarios.json` is the machine-readable index of the demo
 * scenarios and, crucially, the authority on which fault files belong to which
 * journey. It is used when present and reconstructed heuristically when not.
 */
export interface FaultManifestMeta {
  path: string;
  found: boolean;
  /** True when every file named by the manifest loaded. */
  complete: boolean;
  missing_files: string[];
  /** Files present in the directory but named by no scenario. */
  unreferenced_files: string[];
}

export type FaultMode = 'scenario' | 'random' | 'off';

export interface FaultDecision {
  mode: FaultMode;
  scenario_id: string | null;
  fault: FaultSpec;
  /** Which step produced the decision; null for the random mode. */
  step_index: number | null;
  occurrence: number | null;
  request_seq: number;
  seed: number;
  match_key: string;
}

/**
 * Faults applied at the mock API's own HTTP edge, before any state is written:
 * the caller sees a slow, rate-limited or unavailable network.
 */
export const EDGE_FAULTS: readonly FaultType[] = [
  'latency',
  'rate_limit_429',
  'service_unavailable_503',
];

/**
 * The two timeouts are NOT edge faults, because their entire point is the
 * difference in what survives them. The booking pipeline decides whether the
 * transaction commits and only then withholds the response, which is why
 * `timeout_before_commit` leaves no booking and `timeout_after_commit` leaves
 * one that reconciliation must resolve.
 */
export const PIPELINE_FAULTS: readonly FaultType[] = [
  'timeout_before_commit',
  'timeout_after_commit',
];

export const MODULE_FAULTS: readonly FaultType[] = [
  'malformed_schema',
  'empty_valid_result',
  'stale_slot_409',
  'duplicate_event',
  'stale_coverage_evidence',
  'notification_failure',
];

export const DEFAULT_DELAY_MS: Record<string, number> = {
  latency: 800,
  timeout_before_commit: 30_000,
  timeout_after_commit: 30_000,
};

/** Internal (non-HTTP) operation paths a scenario can target. */
export const INTERNAL_OPERATIONS = {
  notificationSend: 'POST /internal/notifications/send',
  upstreamBooking: 'POST /internal/upstream/booking',
} as const;
