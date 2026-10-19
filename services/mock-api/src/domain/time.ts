/**
 * Time rules (§17): every slot is STORED in UTC and DISPLAYED in the hospital's
 * IANA timezone. `display_timezone` is a rendering hint only — it is never used
 * for arithmetic, date filtering or comparisons.
 */

/** RFC 3339 UTC instant, second precision, always ending in `Z`. */
export function toUtcIso(date: Date): string {
  return date.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

export function fromUtcIso(value: string): Date {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    throw new RangeError(`Not a valid ISO timestamp: ${value}`);
  }
  return parsed;
}

export function addMinutes(date: Date, minutes: number): Date {
  return new Date(date.getTime() + minutes * 60_000);
}

export function nowUtcIso(): string {
  return toUtcIso(new Date());
}

/**
 * Renders a stored UTC instant in the hospital's zone. Callers must never derive
 * the wall clock from the raw UTC string, which is what silently shifts a
 * 09:00 America/Chicago appointment to 14:00 on a naive client.
 */
export function formatInZone(utcInstant: string, timezone: string, locale = 'en-US'): string {
  return new Intl.DateTimeFormat(locale, {
    timeZone: timezone,
    dateStyle: 'full',
    timeStyle: 'short',
  }).format(fromUtcIso(utcInstant));
}

/** Machine-readable rendering of the same instant, with the zone offset spelled out. */
export function formatInZoneIso(utcInstant: string, timezone: string, locale = 'en-US'): string {
  return new Intl.DateTimeFormat(locale, {
    timeZone: timezone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZoneName: 'longOffset',
  }).format(fromUtcIso(utcInstant));
}

/** Hour of day (0-23) of a UTC instant, in the given zone. Used for time-of-day filtering. */
export function hourInZone(utcInstant: string, timezone: string): number {
  const rendered = new Intl.DateTimeFormat('en-US', {
    timeZone: timezone,
    hour: '2-digit',
    hour12: false,
  }).format(fromUtcIso(utcInstant));
  return Number.parseInt(rendered, 10) % 24;
}

/** Weekday name in the given zone, lower-case (`monday`). */
export function weekdayInZone(utcInstant: string, timezone: string): string {
  return new Intl.DateTimeFormat('en-US', { timeZone: timezone, weekday: 'long' })
    .format(fromUtcIso(utcInstant))
    .toLowerCase();
}

/** Offset of `zone` from UTC, in minutes, at the given instant (handles DST). */
export function zoneOffsetMinutes(zone: string, instant: Date): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: zone,
    timeZoneName: 'longOffset',
  }).formatToParts(instant);
  const name = parts.find((part) => part.type === 'timeZoneName')?.value ?? 'GMT+00:00';
  const match = /GMT([+-])(\d{2}):?(\d{2})?/.exec(name);
  if (!match) return 0;
  const sign = match[1] === '-' ? -1 : 1;
  return sign * (Number.parseInt(match[2] ?? '0', 10) * 60 + Number.parseInt(match[3] ?? '0', 10));
}

/**
 * Converts hospital WALL-CLOCK time to the UTC instant that is stored.
 * Fixtures are written in the hospital's local terms ("09:00 in Chicago"),
 * never in UTC, which is exactly the failure mode §17 warns about.
 */
export function zonedTimeToUtc(zone: string, localIso: string): Date {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(localIso);
  if (!match) throw new RangeError(`Not a local ISO timestamp: ${localIso}`);
  const [, year, month, day, hour, minute] = match;
  const naive = Date.UTC(
    Number.parseInt(year!, 10),
    Number.parseInt(month!, 10) - 1,
    Number.parseInt(day!, 10),
    Number.parseInt(hour!, 10),
    Number.parseInt(minute!, 10),
  );
  // Two passes settle the instant across a DST boundary.
  let guess = naive - zoneOffsetMinutes(zone, new Date(naive)) * 60_000;
  guess = naive - zoneOffsetMinutes(zone, new Date(guess)) * 60_000;
  return new Date(guess);
}

/** `YYYY-MM-DD` for an instant, rendered in the given zone. */
export function dateInZone(instant: Date, zone: string): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: zone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(instant);
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? '01';
  return `${get('year')}-${get('month')}-${get('day')}`;
}

export function addDays(date: Date, days: number): Date {
  return new Date(date.getTime() + days * 86_400_000);
}

export function timeOfDayOfHour(hour: number): 'morning' | 'afternoon' | 'evening' {
  if (hour < 12) return 'morning';
  if (hour < 17) return 'afternoon';
  return 'evening';
}
