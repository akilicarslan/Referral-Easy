/**
 * Seeded randomness for the fault engine.
 *
 * Every draw is a PURE function of (seed, sequence number) rather than a value
 * pulled from a stateful generator. That is a deliberate choice: a stateful
 * PRNG only reproduces a sequence when every preceding draw happened in the
 * same order, so a single retried request, a health check in between, or a
 * restarted process would silently shift the whole run. Here, request 7 with
 * seed 42 always yields the same fault, no matter what request 6 was or whether
 * the process restarted in between.
 */

/** FNV-1a, 32-bit. Small, fast and stable across platforms. */
export function fnv1a(input: string): number {
  let hash = 0x811c9dc5;
  for (let index = 0; index < input.length; index += 1) {
    hash ^= input.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

export function mulberry32(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Deterministic float in [0, 1) for a (seed, sequence) pair. */
export function draw(seed: number, sequence: number, salt = ''): number {
  const next = mulberry32(fnv1a(`${seed}|${sequence}|${salt}`));
  // Discard the first value: mulberry32's first output is correlated with the seed.
  next();
  return next();
}

/** Deterministic integer in [0, modulo). */
export function drawIndex(seed: number, sequence: number, modulo: number, salt = ''): number {
  if (modulo <= 0) return 0;
  return Math.floor(draw(seed, sequence, salt) * modulo) % modulo;
}

/** Deterministic boolean with probability `numerator / denominator`. */
export function drawChance(seed: number, sequence: number, numerator: number, denominator: number, salt = ''): boolean {
  return draw(seed, sequence, salt) < numerator / denominator;
}
