/**
 * `@referraleasy/contracts` — generated Zod bindings plus the hand-written adapters.
 *
 * Everything under `generated/` is produced by `packages/contracts/generate.sh`
 * (`pnpm gen:contracts`) from `packages/contracts/schemas/*.json`, which is the single source of
 * truth (§1.1). Do not hand-edit it.
 *
 * Generated exports are namespaced `origin$Name` (e.g. `booking$BookingRequest`) because
 * definition names are only unique within a schema file — `AdapterType` exists in both
 * `booking.json` and `directory.json` meaning different things.
 */

export * from './generated/index.js';
export * from './a2a.js';
