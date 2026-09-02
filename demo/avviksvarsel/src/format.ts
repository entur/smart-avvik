// Display helpers. All times are shown in Europe/Oslo. Weekdays are 0=Monday … 6=Sunday,
// matching the backend (varsel.py).

export const WEEKDAY_SHORT = ['Man', 'Tir', 'Ons', 'Tor', 'Fre', 'Lør', 'Søn'];
export const WEEKDAYS = [0, 1, 2, 3, 4, 5, 6];

/** The two rush windows togvarsel forecasts, as the push bodies name them
 *  (togvarsel_notify._RUSH_NAME) — capitalised here because the card starts a line with it. */
export const RUSH_NAME: Record<string, string> = {
  am: 'Morgenrushet',
  pm: 'Ettermiddagsrushet',
};

const timeFmt = new Intl.DateTimeFormat('nb-NO', {
  timeZone: 'Europe/Oslo',
  hour: '2-digit',
  minute: '2-digit',
});

/** ISO instant -> "08:16" in Oslo time. */
export function fmtTime(iso: string | null): string {
  if (!iso) return '';
  return timeFmt.format(new Date(iso));
}

/** 0.42 -> "42 %". */
export function fmtPct(p: number): string {
  return `${Math.round(p * 100)} %`;
}

/** Compact weekday summary: contiguous runs collapse ("Man–Fre"); all seven -> "Hver dag". */
export function formatDays(days: number[]): string {
  const sorted = [...new Set(days)].sort((a, b) => a - b);
  if (sorted.length === 7) return 'Hver dag';
  if (sorted.length === 0) return '';
  // Split into contiguous runs.
  const runs: number[][] = [];
  for (const d of sorted) {
    const last = runs[runs.length - 1];
    if (last && d === last[last.length - 1] + 1) last.push(d);
    else runs.push([d]);
  }
  return runs
    .map((run) =>
      run.length >= 3
        ? `${WEEKDAY_SHORT[run[0]]}–${WEEKDAY_SHORT[run[run.length - 1]]}`
        : run.map((d) => WEEKDAY_SHORT[d]).join(', '),
    )
    .join(', ');
}

/** The line ref reduced to what a traveller calls it: "VYG:Line:L1" -> "L1". The same reduction
 *  /demo and /historikk make, so every surface names a line alike — and word-for-word varsel.py's
 *  `_local_line_code`, which is what the push body is built from.
 *
 *  Null when there is no ref to reduce: `_train_label` then falls back to "Tog kl 09:24" rather
 *  than inventing a line, and this card has to fall back the same way or the two disagree. */
export function lineCode(line: string | null): string | null {
  if (!line) return null;
  const parts = line.split(':');
  return parts[parts.length - 1] || null;
}

const osloDay = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Europe/Oslo',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});

/** Is this departure on the Oslo calendar day after `now`'s? Exactly tomorrow, like varsel.py's
 *  `_departs_tomorrow`: a push about the 07:18 routinely goes out the evening before, by design,
 *  and without the word it reads as tonight's train (design review 2026-09-01, finding 48). */
export function departsTomorrow(iso: string | null, now: Date = new Date()): boolean {
  if (!iso) return false;
  const tomorrow = new Date(now.getTime() + 24 * 60 * 60 * 1000);
  return osloDay.format(new Date(iso)) === osloDay.format(tomorrow);
}

/** "Tog L1 09:24" — which train the alert is about, as a departure board names it. Word for word
 *  varsel.py's `_train_label`, "kl"-less line form and all: the card sits under the notification
 *  this text came from, so a difference between them reads as two different trains. Gains
 *  «i morgen» when the departure is tomorrow, exactly as the push wording does. */
export function trainLabel(mode: string | null, line: string | null, iso: string | null): string {
  const code = lineCode(line);
  const label = code ? `${mode ?? 'Tog'} ${code} ${fmtTime(iso)}` : `${mode ?? 'Tog'} kl ${fmtTime(iso)}`;
  return departsTomorrow(iso) ? `${label} i morgen` : label;
}

/** The Journey Planner's transport modes as lower-case Norwegian nouns — varsel.py's
 *  `_LEG_MODE_NOUN`, so the card's «Ta tog RE11 07:24 i stedet» and the push's «Alternativ tog
 *  RE11 07:24» name the vehicle alike. Unknown modes yield no noun rather than a guessed one. */
export const LEG_MODE_NOUN: Record<string, string> = {
  rail: 'tog',
  bus: 'buss',
  coach: 'buss',
  tram: 'trikk',
  metro: 'T-bane',
  water: 'båt',
};

// Mirrors varsel.py's _CANCEL_LIKELY: above a 50 % cancellation probability the wording states the
// cancellation instead of the delay interval.
export const CANCEL_LIKELY = 0.5;

/** Delay phrasing from the exceedance-stack interval: "Typisk 15–27 min forsinket" (collapsed when
 *  p50 and p90 coincide, omitted when neither is known). Word-for-word varsel.py's `_delay_phrase`,
 *  capital T included, so the card and the push it describes cannot read differently. */
export function fmtDelay(p50: number | null, p90: number | null): string | null {
  if (!p50 && !p90) return null;
  if (p50 && p90 && p90 > p50) return `Typisk ${p50}–${p90} min forsinket`;
  return `Typisk ${p90 ?? p50} min forsinket`;
}
