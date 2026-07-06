// Time/number helpers. All display is Europe/Oslo; the demo window (Apr–Jun 2026) is
// entirely CEST, so the datetime-local <-> UTC conversion can use a fixed +02:00 offset
// instead of pulling in a tz library.

const OSLO_OFFSET = '+02:00';

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
  const d = new Date(iso);
  const oslo = new Date(d.getTime() + 2 * 3600_000); // CEST
  return oslo.toISOString().slice(0, 16);
}

/** datetime-local value (Oslo wall time) -> UTC ISO instant. */
export function fromLocalInput(value: string): string {
  return new Date(`${value}:00${OSLO_OFFSET}`).toISOString();
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
