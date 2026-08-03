// Time/number helpers. All display is Europe/Oslo. The datetime-local <-> UTC
// conversion reads the zone's real offset out of Intl at the instant in question,
// so it stays correct across the CET/CEST switch without a tz library.

// Wall-clock fields for Europe/Oslo. hourCycle 'h23' so midnight is "00", not "24".
const osloParts = new Intl.DateTimeFormat('en-US', {
  timeZone: 'Europe/Oslo',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
});

function osloWallClock(instant: number): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of osloParts.formatToParts(new Date(instant))) {
    if (part.type !== 'literal') out[part.type] = part.value;
  }
  return out;
}

/** Europe/Oslo's offset from UTC, in ms, at a given instant (+1h CET, +2h CEST). */
function osloOffsetMs(instant: number): number {
  const p = osloWallClock(instant);
  const wallAsUtc = Date.UTC(
    Number(p.year),
    Number(p.month) - 1,
    Number(p.day),
    Number(p.hour),
    Number(p.minute),
    Number(p.second),
  );
  return wallAsUtc - Math.floor(instant / 1000) * 1000;
}

const timeFmt = new Intl.DateTimeFormat('nb-NO', {
  timeZone: 'Europe/Oslo',
  hour: '2-digit',
  minute: '2-digit',
});

const dateTimeFmt = new Intl.DateTimeFormat('nb-NO', {
  timeZone: 'Europe/Oslo',
  day: 'numeric',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
});

const dateFmt = new Intl.DateTimeFormat('nb-NO', {
  timeZone: 'Europe/Oslo',
  weekday: 'long',
  day: 'numeric',
  month: 'long',
  year: 'numeric',
});

/** "18:16" in Oslo time. */
export function fmtTime(iso: string): string {
  return timeFmt.format(new Date(iso));
}

/** "14. apr., 18:16" in Oslo time. */
export function fmtDateTime(iso: string): string {
  return dateTimeFmt.format(new Date(iso));
}

/** "tirsdag 14. april 2026" in Oslo time. */
export function fmtDate(iso: string): string {
  return dateFmt.format(new Date(iso));
}

/** UTC instant -> value for a datetime-local input, expressed in Oslo wall time. */
export function toLocalInput(iso: string): string {
  const p = osloWallClock(new Date(iso).getTime());
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

/** datetime-local value (Oslo wall time) -> UTC ISO instant. */
export function fromLocalInput(value: string): string {
  const [date, time] = value.split('T');
  const [y, mo, d] = date.split('-').map(Number);
  const [h, mi] = time.split(':').map(Number);
  const wallAsUtc = Date.UTC(y, mo - 1, d, h, mi);
  // The offset has to be sampled at the instant we are solving for, so guess with
  // the offset at the naive reading, then resample. One correction settles every
  // wall time except the hour that repeats when the clocks go back, where either
  // answer is defensible.
  const guess = wallAsUtc - osloOffsetMs(wallAsUtc);
  return new Date(wallAsUtc - osloOffsetMs(guess)).toISOString();
}

/** 0.42 -> "42 %". */
export function fmtPct(p: number): string {
  return `${Math.round(p * 100)} %`;
}

/** Delay interval "5–31 min" (en dash); single value when p50 === p90. */
export function fmtInterval(p50: number | null, p90: number | null): string {
  if (p50 === null && p90 === null) return '–';
  if (p50 !== null && p90 !== null && p50 !== p90) return `${p50}–${p90} min`;
  return `${p90 ?? p50} min`;
}

// Mirrors varsel.py's _CANCEL_LIKELY. Above this cancellation probability the /varsel push states the
// cancellation; below it, the delay interval. Only used to mark which figure the push quoted.
export const CANCEL_LIKELY = 0.5;

/** Which figure the /varsel push text would have quoted for this snapshot, or null if it would not
 *  have quoted one. Mirrors varsel.py's `_deterministic_body`: the cancellation probability above
 *  CANCEL_LIKELY, otherwise the delay interval — and neither when no delay figure is known (the
 *  falsy check, not a null check, matches `_delay_phrase`, which also treats 0 as absent). Returns
 *  null unless the snapshot actually crosses the alert threshold: with no push there is no text. */
export function varselDriver(snap: {
  cancel_probability: number;
  delay_p50_min: number | null;
  delay_p90_min: number | null;
  would_alert: boolean;
}): 'cancel' | 'delay' | null {
  if (!snap.would_alert) return null;
  if (snap.cancel_probability >= CANCEL_LIKELY) return 'cancel';
  return snap.delay_p50_min || snap.delay_p90_min ? 'delay' : null;
}
