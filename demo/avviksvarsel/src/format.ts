// Display helpers. All times are shown in Europe/Oslo. Weekdays are 0=Monday … 6=Sunday,
// matching the backend (varsel.py).

export const WEEKDAY_SHORT = ['Man', 'Tir', 'Ons', 'Tor', 'Fre', 'Lør', 'Søn'];
export const WEEKDAYS = [0, 1, 2, 3, 4, 5, 6];

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

/** Delay phrasing for an alert: "typisk 6 min forsinket" (omitted when unknown/zero). */
export function fmtDelay(typ: number | null): string | null {
  if (!typ) return null;
  return `typisk ${typ} min forsinket`;
}
