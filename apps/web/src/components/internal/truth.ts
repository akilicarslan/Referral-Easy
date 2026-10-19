/**
 * The honesty vocabulary.
 *
 * Every rule in here exists because getting it wrong would tell the patient
 * something untrue. They are functions rather than component-local conventions
 * so there is exactly one place to review, and so a test can pin each one.
 *
 * The three rules being encoded:
 *
 *   1. `REQUESTED` and `PENDING_REVIEW` are never shown as confirmed. A booking
 *      awaiting review renders as "requested — the hospital will confirm".
 *      HTTP 200 is not confirmation, and neither is the presence of a number.
 *
 *   2. `unknown` is shown as unknown, with its freshness, and never smoothed
 *      into "no". "We could not verify your plan's network status (checked 18
 *      hours ago)" is correct; "not covered" is a lie.
 *
 *   3. Every count carries its sample size, and fixture output says so.
 */

// ── Certainty ────────────────────────────────────────────────────────────────

/**
 * How much we actually know. This is the type every status surface is built on.
 * It is intentionally coarse: the UI only needs to distinguish these four, and a
 * finer-grained enum would tempt a component into inventing a fifth look.
 */
export type Certainty = 'confirmed' | 'requested' | 'unknown' | 'failed';

/**
 * Booking states that mean the hospital has actually confirmed.
 *
 * Deliberately an allowlist. An unrecognised state — a new one the backend adds,
 * a typo, a missing field — falls through to `unknown`, never to `confirmed`.
 * Failing towards "we are not sure yet" is the only safe direction.
 */
const CONFIRMED_BOOKING_STATES = new Set(['BOOKED', 'CONFIRMED', 'SCHEDULED', 'COMPLETED']);

/** States that mean "sent, awaiting a human or a downstream system". */
const REQUESTED_BOOKING_STATES = new Set([
  'REQUESTED',
  'PENDING_REVIEW',
  'PENDING',
  'AWAITING_CONFIRMATION',
  'BOOKING_IN_FLIGHT',
  'SUBMITTED',
]);

const FAILED_BOOKING_STATES = new Set(['FAILED', 'CANCELLED', 'ABANDONED', 'REJECTED']);

export interface StatusReadout {
  readonly certainty: Certainty;
  /** Chip class suffix. Matches the `.chip.*` classes in shared styles. */
  readonly tone: 'ok' | 'requested' | 'unknown' | 'error';
  /** A glyph so the state is not conveyed by colour alone (WCAG 1.4.1). */
  readonly glyph: string;
  /** Short chip word. */
  readonly label: string;
  /** The full sentence the patient reads. */
  readonly sentence: string;
}

const READOUTS: Record<Certainty, Omit<StatusReadout, 'certainty'>> = {
  confirmed: {
    tone: 'ok',
    glyph: '✓',
    label: 'Confirmed',
    sentence: 'The hospital has confirmed this appointment.',
  },
  requested: {
    tone: 'requested',
    glyph: '○',
    label: 'Requested',
    sentence: 'Requested — the hospital will confirm this appointment.',
  },
  unknown: {
    tone: 'unknown',
    glyph: '?',
    label: 'Not verified',
    sentence: 'We could not verify the outcome of this booking.',
  },
  failed: {
    tone: 'error',
    glyph: '✕',
    label: 'Not booked',
    sentence: 'This booking did not go through.',
  },
};

/**
 * Classify a booking lifecycle state.
 *
 * The default arm is `unknown`, and that is the whole point of the function.
 */
export function classifyBookingState(state: string | undefined | null): StatusReadout {
  const normalised = typeof state === 'string' ? state.trim().toUpperCase() : '';

  if (normalised === '') {
    return { certainty: 'unknown', ...READOUTS.unknown };
  }
  if (CONFIRMED_BOOKING_STATES.has(normalised)) {
    return { certainty: 'confirmed', ...READOUTS.confirmed };
  }
  if (REQUESTED_BOOKING_STATES.has(normalised)) {
    return { certainty: 'requested', ...READOUTS.requested };
  }
  if (FAILED_BOOKING_STATES.has(normalised)) {
    return { certainty: 'failed', ...READOUTS.failed };
  }
  // `BOOKING_UNVERIFIED`, `RECOVERY`, and anything we do not recognise.
  return { certainty: 'unknown', ...READOUTS.unknown };
}

/** True only when the hospital has confirmed. Use this, never a raw string compare. */
export function isConfirmed(state: string | undefined | null): boolean {
  return classifyBookingState(state).certainty === 'confirmed';
}

// ── Verification / coverage ──────────────────────────────────────────────────

export interface CoverageReadout {
  readonly tone: 'ok' | 'unknown' | 'error';
  readonly glyph: string;
  readonly label: string;
  readonly sentence: string;
}

/**
 * Describe network-status verification.
 *
 * A `no` here means the directory positively said "not in network". Anything we
 * could not check — including the `unknown` the contract defines — is reported as
 * unverified, with the age of the last check attached by the caller.
 */
export function describeCoverage(
  state: string | undefined | null,
  ageHours?: number | undefined,
): CoverageReadout {
  const freshness = describeFreshness(ageHours);

  switch (state) {
    case 'yes':
      return {
        tone: 'ok',
        glyph: '✓',
        label: 'In network',
        sentence: `Your plan's network status is verified${freshness.suffix}.`,
      };
    case 'no':
      return {
        tone: 'error',
        glyph: '✕',
        label: 'Not in network',
        sentence: `The directory reports this provider is out of network${freshness.suffix}.`,
      };
    case 'unknown':
      return {
        tone: 'unknown',
        glyph: '?',
        label: 'Not verified',
        sentence: `We could not verify your plan's network status${freshness.suffix}.`,
      };
    default:
      return {
        tone: 'unknown',
        glyph: '?',
        label: 'Not checked',
        sentence: 'Network status has not been checked for this provider.',
      };
  }
}

// ── Freshness ────────────────────────────────────────────────────────────────

export interface Freshness {
  /** A phrase that reads as a suffix, e.g. " (checked 18 hours ago)". */
  readonly suffix: string;
  /** The bare phrase, e.g. "18 hours ago". Empty when there is nothing to say. */
  readonly phrase: string;
  readonly isStale: boolean;
}

/** Longer than this and a "checked" timestamp stops being reassuring. */
const STALE_AFTER_HOURS = 24;

/**
 * Turn an age into words.
 *
 * `undefined` produces an empty phrase rather than a guess. Saying "recently"
 * when we do not know would be a small lie of exactly the kind this app is
 * built to avoid.
 */
export function describeFreshness(ageHours?: number | null): Freshness {
  if (typeof ageHours !== 'number' || !Number.isFinite(ageHours) || ageHours < 0) {
    return { suffix: '', phrase: '', isStale: true };
  }

  const phrase = formatAge(ageHours);
  return {
    suffix: ` (checked ${phrase})`,
    phrase,
    isStale: ageHours > STALE_AFTER_HOURS,
  };
}

function formatAge(ageHours: number): string {
  if (ageHours < 1) {
    const minutes = Math.max(1, Math.round(ageHours * 60));
    return minutes === 1 ? '1 minute ago' : `${minutes} minutes ago`;
  }
  if (ageHours < 24) {
    const hours = Math.round(ageHours);
    return hours === 1 ? '1 hour ago' : `${hours} hours ago`;
  }
  const days = Math.round(ageHours / 24);
  return days === 1 ? '1 day ago' : `${days} days ago`;
}

/** Age in hours from an ISO timestamp, or `undefined` if it cannot be read. */
export function hoursSince(iso: string | undefined | null, now: Date = new Date()): number | undefined {
  if (typeof iso !== 'string' || iso.length === 0) {
    return undefined;
  }
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) {
    return undefined;
  }
  return Math.max(0, (now.getTime() - then.getTime()) / 3_600_000);
}

// ── Degradation (§10) ────────────────────────────────────────────────────────

export interface DegradationReadout {
  readonly tone: 'degraded';
  readonly glyph: string;
  readonly label: string;
  readonly sentence: string;
}

const DEGRADATION_READOUTS: Record<string, string> = {
  rerank_down: 'Ranking was reduced, so these results are less well ordered than usual.',
  embeddings_down: 'Semantic search was unavailable, so these results come from keyword matching only.',
  opensearch_down: 'The directory index was unavailable, so these results come from a smaller cached set.',
  static_seed: 'These results come from a fixed seed set — no live search was performed.',
  no_source: 'No source was reachable, so nothing here reflects the live directory.',
};

/**
 * Describe a reduced pipeline.
 *
 * The contract says a workflow state change carries `degraded`, and that an
 * answer produced without the directory is a different answer. So a degraded run
 * always produces a visible readout — never a silent one.
 */
export function describeDegradation(level: string | undefined | null): DegradationReadout | undefined {
  if (typeof level !== 'string' || level.length === 0 || level === 'full') {
    return undefined;
  }
  return {
    tone: 'degraded',
    glyph: '△',
    label: level.replace(/_/g, ' '),
    sentence:
      DEGRADATION_READOUTS[level] ??
      'Part of the lookup pipeline was unavailable, so this answer is less complete than usual.',
  };
}

// ── Counts, samples, and synthetic data ──────────────────────────────────────

export interface CountReadout {
  /** e.g. "12 providers". Never a bare numeral in the UI. */
  readonly text: string;
  /** True when the caller should render the sample-size caption too. */
  readonly hasSample: boolean;
}

/**
 * Describe a result count together with its sample size.
 *
 * A number shown without its denominator is how a demo starts lying. `total` is
 * optional because some payloads only carry the visible slice; when it is absent
 * the sample is reported as the visible count and described as such.
 */
export function describeCount(count: number, options: { noun: string; total?: number | undefined }): CountReadout {
  const noun = count === 1 ? options.noun : pluralise(options.noun);
  return {
    text: `${count} ${noun}`,
    hasSample: options.total !== undefined,
  };
}

function pluralise(noun: string): string {
  if (noun.endsWith('y')) {
    return `${noun.slice(0, -1)}ies`;
  }
  if (noun.endsWith('s')) {
    return noun;
  }
  return `${noun}s`;
}

/** The caption that accompanies every count. */
export function describeSample(count: number, total?: number | undefined): string {
  if (total === undefined) {
    return `Sample: all ${count} shown.`;
  }
  return `Sample: ${count} of ${total}.`;
}

/** The label for fixture output (§12, §18: canned data must not pass as measured). */
export const SYNTHETIC_LABEL = 'Synthetic data';
export const SYNTHETIC_TITLE = 'This is fixture data from the offline profile, not a live result.';

// ── Time ─────────────────────────────────────────────────────────────────────

/**
 * Render an instant in the display timezone.
 *
 * A slot time without a zone is not a time (§17), so when the zone is missing
 * the formatter says so instead of silently using the browser's.
 */
export function formatInstant(
  iso: string | undefined | null,
  timeZone: string | undefined | null,
): { readonly text: string; readonly zone: string; readonly timeZoneKnown: boolean } {
  if (typeof iso !== 'string' || iso.length === 0) {
    return { text: 'Time not provided', zone: '', timeZoneKnown: false };
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return { text: 'Time not provided', zone: '', timeZoneKnown: false };
  }

  const hasZone = typeof timeZone === 'string' && timeZone.length > 0;
  const options: Intl.DateTimeFormatOptions = {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  };
  if (hasZone) {
    options.timeZone = timeZone;
  }

  try {
    return {
      text: new Intl.DateTimeFormat('en-GB', options).format(date),
      zone: hasZone ? (timeZone as string) : browserTimeZone(),
      timeZoneKnown: hasZone,
    };
  } catch {
    return {
      text: new Intl.DateTimeFormat('en-GB', {
        weekday: 'short',
        day: 'numeric',
        month: 'short',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      }).format(date),
      zone: browserTimeZone(),
      timeZoneKnown: false,
    };
  }
}

function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch {
    return 'local time';
  }
}

// ── Reason codes ─────────────────────────────────────────────────────────────

/**
 * Turn a contract `ReasonCode` into a sentence a patient can act on.
 *
 * Unknown codes fall through to a generic-but-honest sentence rather than the
 * raw snake_case token, which means nothing to a patient.
 */
const REASON_SENTENCES: Record<string, string> = {
  missing_mandatory_field: 'A required detail is still missing.',
  ambiguous_input: 'An answer could not be interpreted with confidence.',
  conflicting_evidence: 'Two sources disagreed, so this needs a person to check.',
  stale_evidence: 'The information we had was too old to rely on.',
  no_candidate_matched: 'Nothing in the directory matched what you asked for.',
  no_slot_available: 'There are no open slots that fit your constraints.',
  hard_constraint_unsatisfiable: 'Your requirements cannot all be met at once.',
  document_missing: 'A required document was not attached.',
  document_unreadable: 'A document was attached but could not be read.',
  coverage_unknown: 'We could not verify your coverage.',
  provider_not_in_network: 'This provider is not in your plan’s network.',
  approval_expired: 'The approval expired before the booking completed.',
  approval_payload_changed: 'The details changed after approval, so it no longer applies.',
  upstream_unavailable: 'The hospital system is not responding right now.',
  upstream_rate_limited: 'The hospital system is rate-limiting us; this should clear shortly.',
  upstream_timeout: 'The hospital system took too long to answer.',
  upstream_malformed_response: 'The hospital system returned something we could not read.',
  booking_outcome_unknown: 'The booking was sent but the outcome could not be confirmed.',
  policy_blocked: 'A policy prevented this step.',
  guardrail_blocked: 'A safety check stopped this step.',
  budget_exhausted: 'The run hit its cost limit before finishing.',
  deadline_exceeded: 'The run hit its time limit before finishing.',
  human_owned: 'A person has taken this over.',
  other: 'Something went wrong that we could not categorise.',
};

/**
 * Reason codes that mean "nothing to explain".
 *
 * `ok` is a real member of the contract's `ReasonCode` enum, and without this it
 * fell through to the "we could not do this" fallback — so a successful booking
 * rendered with the sentence *"This step could not be completed automatically."*
 * That is the exact class of false statement this file exists to prevent.
 */
const NO_REASON_CODES = new Set(['ok']);

export function describeReason(code: string | undefined | null): string | undefined {
  if (typeof code !== 'string' || code.length === 0 || NO_REASON_CODES.has(code)) {
    return undefined;
  }
  // An unrecognised code is reported as unexplained rather than as a failure: the
  // next code the backend adds is not necessarily a problem, and guessing that it
  // is would be inventing a cause for something we were never told.
  return REASON_SENTENCES[code] ?? 'We have no explanation recorded for this yet.';
}
