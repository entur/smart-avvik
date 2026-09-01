// Display helpers and the page's label vocabulary. All dates are read in Europe/Oslo, matching the
// rest of the stack — a rush is an Oslo-clock thing and the API counts in Oslo days.

import type { IssueSlot, Level, StatSlot, Verdict } from './api';

// Module scope, not per render: an Intl.DateTimeFormat is expensive to construct and the day table
// formats two of these per row over as many as ~250 rows.
const dateFmt = new Intl.DateTimeFormat('nb-NO', {
  timeZone: 'Europe/Oslo',
  day: '2-digit',
  month: 'short',
});

const weekdayFmt = new Intl.DateTimeFormat('nb-NO', {
  timeZone: 'Europe/Oslo',
  weekday: 'long',
});

const timeFmt = new Intl.DateTimeFormat('nb-NO', {
  timeZone: 'Europe/Oslo',
  hour: '2-digit',
  minute: '2-digit',
});

// The realized share is a percentage with one decimal, in Norwegian notation (comma) — it is the
// figure the level was derived from, and rounding it to whole percent would collapse the
// distinction between a 9,6 % and a 10,4 % day at a boundary the models care about.
const shareFmt = new Intl.NumberFormat('nb-NO', {
  style: 'percent',
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

/** The en dash every missing figure renders as. Never "0 %": a rate with an empty denominator is
 *  unknown, and 0 % would read as "we got everything wrong" (R17). */
export const DASH = '–';

/** ISO date ('2026-09-01') or instant -> "01. sep". */
export function fmtDate(iso: string | null): string {
  return iso ? dateFmt.format(new Date(iso)) : DASH;
}

/** ISO date -> "tirsdag". A bare date parses as UTC midnight, which is the same calendar day in
 *  Oslo year-round, so the weekday is safe to read off it. */
export function fmtWeekday(iso: string | null): string {
  return iso ? weekdayFmt.format(new Date(iso)) : DASH;
}

/** ISO instant -> "06:31" in Oslo time. */
export function fmtTime(iso: string | null): string {
  return iso ? timeFmt.format(new Date(iso)) : DASH;
}

/** 0.93 -> "93 %"; null -> en dash. */
export function fmtPct(v: number | null): string {
  return v === null || v === undefined ? DASH : `${Math.round(v * 100)} %`;
}

/** The realized trouble10 share, one decimal: 0.213 -> "21,3 %". */
export function fmtShare(v: number | null): string {
  return v === null || v === undefined ? DASH : shareFmt.format(v);
}

/** Minutes of warning, signed: 179 -> "+179 min", -1 -> "-1 min", null -> en dash.
 *
 *  The sign is not decoration. Varslingstid is measured from each rush's own start, so the morning
 *  issues lead by about a minute either side of zero by construction — an unsigned "1 min" would
 *  read as a minute of warning where there was a minute of lateness. */
export function fmtLead(v: number | null): string {
  if (v === null || v === undefined) return DASH;
  const m = Math.round(v);
  return `${m > 0 ? '+' : ''}${m} min`;
}

/** Mirrored from rushvarsel_verdicts.VERDICT_LABEL — the canonical source. Retyping them here is
 *  how a rename ships to one surface only, so any edit belongs in that module first. */
export const VERDICT_LABEL: Record<Verdict, string> = {
  traff: 'Traff',
  traff_nesten: 'Traff nesten',
  falsk_alarm: 'Falsk alarm',
  bom: 'Bom',
  riktig_stille: 'Riktig stille',
  venter: 'Venter',
  ukjent: 'Ukjent',
  ikke_vurdert: 'Ikke vurdert',
};

export type BadgeVariant = 'success' | 'near' | 'warning' | 'negative' | 'neutral' | 'muted';

/** Chip variant per verdict. `traff_nesten` gets its own OUTLINED variant rather than sharing
 *  `falsk_alarm`'s fill: it is a near-success and the other an outright failure, and collapsing
 *  them destroys exactly the distinction the eight-state taxonomy exists for. `bom` is negative
 *  because a warning that never went out is the failure the traveller actually feels. */
export const VERDICT_VARIANT: Record<Verdict, BadgeVariant> = {
  traff: 'success',
  traff_nesten: 'near',
  falsk_alarm: 'warning',
  bom: 'negative',
  riktig_stille: 'neutral',
  venter: 'muted',
  ukjent: 'muted',
  ikke_vurdert: 'muted',
};

/** Why a verdict is not an outcome. Only the three muted states have one — they are the states a
 *  reader will otherwise take for a judgement, and `.rh-badge--muted` carries `cursor: help`
 *  precisely so they can be asked about. */
export const VERDICT_HELP: Partial<Record<Verdict, string>> = {
  venter:
    'Fasit for dette rushet er ikke satt ennå. Nattjobben forsøker på nytt i ti dager, så dette ' +
    'kan fortsatt bli avklart.',
  ukjent:
    'Fasit kan ikke lenger fastslås — kilden ga aldri nok avganger, eller forsøkene har gitt seg. ' +
    'Det telles ikke som «riktig stille»: da ville manglende data sett ut som en riktig avgjørelse.',
  ikke_vurdert:
    'Ingen varselrunde ble vurdert for denne utstedelsen — varslingen var slått av, eller dagen ' +
    'er eldre enn runde-loggen. Det betyr ikke at et varsel var unødvendig.',
};

/** togvarsel's own level vocabulary in Norwegian. Adjectival ("grønt nivå"), matching how
 *  /togmelding words its levels. */
export const LEVEL_LABEL: Record<Level, string> = {
  green: 'grønt',
  yellow: 'gult',
  red: 'rødt',
};

/** The stats table's row labels: the blended total, then the four issue times. */
export const SLOT_LABEL: Record<StatSlot, string> = {
  total: 'Totalt',
  '0630': 'kl 06:30',
  '0700': 'kl 07:00',
  '1200': 'kl 12:00',
  '1400': 'kl 14:00',
};

/** The four issue slots in issue order — the day table's four columns, and the order the stats
 *  table's slot rows follow the total in. */
export const ISSUE_SLOTS: IssueSlot[] = ['0630', '0700', '1200', '1400'];

/** A level as a label, falling back to the raw value: the API types it as a free string, and an
 *  unrecognised level should show as itself rather than as "undefined". */
export function levelLabel(level: string | null): string {
  if (!level) return DASH;
  return LEVEL_LABEL[level as Level] ?? level;
}

/** The level chip's class. Only the three known levels get a modifier — an unrecognised one keeps
 *  the neutral ring `.rh-level` already defaults to. */
export function levelClass(level: string | null): string {
  return level && level in LEVEL_LABEL ? `rh-level rh-level--${level}` : 'rh-level';
}
