// Display vocabulary and formatting for Rushvarsel.
//
// The level vocabulary lives in levels.ts (shared with /varsel as a guarded duplicate) and is
// re-exported here so existing imports keep working. LEVEL_TEXT and the percentage phrase below
// are MIRRORED server-side by togvarsel._LEVEL_TEXT / togvarsel._feedback_snapshot: the feedback
// context persists the exact text the traveller read, so an edit there has to be made in levels.ts
// too. disruptions/api/test_togvarsel.py greps these sources for both and fails if they drift.

import { LEVEL_ADJ, LEVEL_LABEL, LEVEL_TEXT, capitalize } from './levels';

export { LEVEL_ADJ, LEVEL_LABEL, LEVEL_TEXT, capitalize };

// The announced-deviation classes togvarsel.known_deviations_for lets through.
export const CLS_LABEL: Record<string, string> = {
  buss_for_tog: 'Buss for tog',
  stengt: 'Stengt strekning',
  faerre_avganger: 'Færre avganger',
  // «Innstilte tog», not «Innstillinger»: the latter means *settings* everywhere else in Entur
  // (design review 2026-09-01, finding 8).
  innstilt: 'Innstilte tog',
  faerre_vogner: 'Kortere tog',
  ny_kjoererute: 'Ny kjørerute',
};

/** 'green' | 'yellow' | 'red' -> the CSS suffix; anything else falls back to a neutral card. */
export function levelClass(level: string): string {
  return level in LEVEL_TEXT ? level : 'unknown';
}

/** '0700' -> '07:00'. */
export function issueTime(issue: string): string {
  return `${issue.slice(0, 2)}:${issue.slice(2)}`;
}

/**
 * The level sentence exactly as the persisted feedback snapshot records it.
 *
 * `Math.round` and not `toFixed`: the server mirrors this with `int(p * 100 + 0.5)` precisely
 * because JavaScript rounds a non-negative .5 upward where Python's `round` ties to even, and the
 * two texts have to be identical at half a percent.
 */
export function levelSentence(level: string, p: number): string {
  return `${LEVEL_TEXT[level] ?? level} (ca. ${Math.round(p * 100)} % sjanse for mye forstyrrelser)`;
}

/** An ISO timestamp as an Oslo wall-clock time — the forecast's own timezone, not the reader's. */
export function osloTime(iso: string): string {
  return new Date(iso).toLocaleTimeString('no-NO', {
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'Europe/Oslo',
  });
}

/** «1. sep. kl. 14:02» — date AND time, for a timestamp that has to be distinguishable from the
 *  same time a week ago (design review 2026-09-01, finding 32: «Sist oppdatert 22:14» looked the
 *  same today and last Tuesday). */
export function osloDateTime(iso: string): string {
  const d = new Date(iso);
  const date = d.toLocaleDateString('no-NO', {
    day: 'numeric',
    month: 'short',
    timeZone: 'Europe/Oslo',
  });
  return `${date} kl. ${osloTime(iso)}`;
}

/** «for 8 minutter siden», «for 3 timer siden», «i går» … — relative first, because freshness is
 *  the whole point of a forecast. `now` is injectable for tests. */
export function relativeTime(iso: string, now: Date = new Date()): string {
  const diffMin = Math.round((now.getTime() - new Date(iso).getTime()) / 60_000);
  if (diffMin < 1) return 'akkurat nå';
  if (diffMin === 1) return 'for 1 minutt siden';
  if (diffMin < 60) return `for ${diffMin} minutter siden`;
  const hours = Math.round(diffMin / 60);
  if (hours === 1) return 'for 1 time siden';
  if (hours < 24) return `for ${hours} timer siden`;
  const days = Math.round(hours / 24);
  return days === 1 ? 'i går' : `for ${days} dager siden`;
}

/** «1 avgang» / «3 avganger» — never «1 avganger» (design review 2026-09-01, finding 4). */
export function count(n: number, singular: string, plural: string): string {
  return `${n} ${n === 1 ? singular : plural}`;
}

/** 'kl. 06:30 og kl. 07:00' — the overdue list, read as prose. */
export function issueTimeList(issues: string[], joiner = ', '): string {
  return issues.map(issueTime).join(joiner);
}
