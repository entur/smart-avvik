// Display vocabulary and formatting for Togmeldingen.
//
// LEVEL_TEXT and the percentage phrase below are MIRRORED server-side by
// togvarsel._LEVEL_TEXT / togvarsel._feedback_snapshot: the feedback context persists the exact
// text the traveller read, so an edit here has to be made there too.
// disruptions/api/test_togvarsel.py greps this file for both and fails if they drift.

export const LEVEL_LABEL: Record<string, string> = {
  green: 'GRØNN',
  yellow: 'GUL',
  red: 'RØD',
};

export const LEVEL_TEXT: Record<string, string> = {
  green: 'liten fare for forstyrrelser',
  yellow: 'noe usikkert',
  red: 'stor fare for forstyrrelser',
};

// The announced-deviation classes togvarsel.known_deviations_for lets through.
export const CLS_LABEL: Record<string, string> = {
  buss_for_tog: 'Buss for tog',
  stengt: 'Stengt strekning',
  faerre_avganger: 'Færre avganger',
  innstilt: 'Innstillinger',
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
  return `${LEVEL_TEXT[level] ?? level} (ca. ${Math.round(p * 100)} % sjanse for mye trøbbel)`;
}

/** An ISO timestamp as an Oslo wall-clock time — the forecast's own timezone, not the reader's. */
export function osloTime(iso: string): string {
  return new Date(iso).toLocaleTimeString('no-NO', {
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'Europe/Oslo',
  });
}

/** 'kl. 06:30 og kl. 07:00' — the overdue list, read as prose. */
export function issueTimeList(issues: string[], joiner = ', '): string {
  return issues.map(issueTime).join(joiner);
}
