// Display helpers. All times are shown in Europe/Oslo, matching the rest of the stack.

import type { Decision, OutcomeState, Verdict } from './api';

const timeFmt = new Intl.DateTimeFormat('nb-NO', {
  timeZone: 'Europe/Oslo',
  hour: '2-digit',
  minute: '2-digit',
});

const dateFmt = new Intl.DateTimeFormat('nb-NO', {
  timeZone: 'Europe/Oslo',
  day: '2-digit',
  month: 'short',
});

/** ISO instant -> "08:16" in Oslo time. */
export function fmtTime(iso: string | null): string {
  return iso ? timeFmt.format(new Date(iso)) : '–';
}

/** ISO date or instant -> "30. jul". */
export function fmtDate(iso: string | null): string {
  return iso ? dateFmt.format(new Date(iso)) : '–';
}

const osloDayFmt = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Europe/Oslo',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});

/** ISO instant -> the Europe/Oslo calendar day it falls on, as '2026-08-12' — the shape
 *  `call_date` already arrives in, so the two compare directly. */
function osloDay(iso: string): string {
  return osloDayFmt.format(new Date(iso));
}

/** An instant read against an operating day: "kl. 00:31" when it falls on that day, and
 *  "11. aug. kl. 23:31" when it does not.
 *
 *  The date appears precisely when it differs. An alert sent the evening before is the case a bare
 *  clock time would misrepresent, and it is not rare: a wide registered window is scored from local
 *  midnight, so leads of ten hours and more are ordinary. */
export function fmtWhen(iso: string | null, day: string | null): string {
  if (!iso) return '–';
  return day !== null && osloDay(iso) === day
    ? `kl. ${fmtTime(iso)}`
    : `${fmtDate(iso)} kl. ${fmtTime(iso)}`;
}

/** 0.93 -> "93 %"; null -> "–". */
export function fmtPct(p: number | null): string {
  return p === null || p === undefined ? '–' : `${Math.round(p * 100)} %`;
}

/** Minutes -> "48 min"; null -> "–". */
export function fmtMin(m: number | null): string {
  return m === null || m === undefined ? '–' : `${m} min`;
}

export function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

export const VERDICT_LABEL: Record<Verdict, string> = {
  hit: 'Traff',
  false_alarm: 'Falsk alarm',
  miss: 'Bom',
  correct_reject: 'Riktig stille',
};

/** Linje Alert/Chip variants. `miss` is negative because a missed disruption is the failure the
 *  traveller actually feels; a false alarm is merely noise. */
export const VERDICT_VARIANT: Record<Verdict, 'success' | 'warning' | 'negative' | 'neutral'> = {
  hit: 'success',
  false_alarm: 'warning',
  miss: 'negative',
  correct_reject: 'neutral',
};

/** What to show when there is no verdict. Kept separate from VERDICT_LABEL so the UI can never
 *  accidentally render an unresolved decision as a real outcome. */
export const OUTCOME_STATE_LABEL: Record<OutcomeState, string> = {
  avklart: 'Avklart',
  venter: 'Venter på fasit',
  ukjent: 'Ukjent utfall',
};

export const OUTCOME_STATE_HELP: Record<OutcomeState, string> = {
  avklart: 'Utfallet er hentet fra sanntidsdata.',
  venter:
    'Fasit er ikke på plass ennå. Nattjobben forsøker på nytt i ti dager, så dette kan fortsatt ' +
    'bli avklart.',
  ukjent:
    'Vi fant ingen registrert avgang for dette toget, og forsøkene har gitt seg. Det telles ikke ' +
    'som «riktig stille» — da ville manglende data sett ut som en riktig avgjørelse.',
};

/** The tooltip behind "Sist vurdert": how many cycles scored this train, and when the first and
 *  last of them did. Undefined for a single look, where the time on the card is the whole story.
 *
 *  It repeats the last look on purpose. A `title` on a non-interactive element becomes that
 *  element's accessible NAME rather than its description, so anything the tooltip leaves out is
 *  not merely unannounced — it is replaced. */
export function fmtLooks(d: Decision): string | undefined {
  if (!d.n_decisions || d.n_decisions < 2 || !d.first_decided_at) return undefined;
  const first = fmtWhen(d.first_decided_at, d.call_date);
  const last = fmtWhen(d.last_decided_at, d.call_date);
  return `Vurdert ${d.n_decisions} ganger — første gang ${first}, sist ${last}.`;
}

/** Realized outcome as a short phrase. Never claims "gikk fint" when the outcome is unknown. */
export function fmtOutcome(d: Decision): string {
  if (d.actual_trouble === null) return OUTCOME_STATE_LABEL[d.outcome_state];
  if (d.actual_cancelled) return 'Innstilt';
  if (d.actual_dep_delay_min !== null && d.actual_dep_delay_min >= 1) {
    return `${d.actual_dep_delay_min} min forsinket`;
  }
  return 'I rute';
}

/** The line ref reduced to what a traveller calls it: "VYG:Line:L2" -> "L2". The codespace and the
 *  `Line` literal name the publisher, not the train, and they push the times off the header's first
 *  line for no reader's benefit. Same reduction /demo does, so the two pages name a line alike.
 *
 *  The registered journey's own from/to are deliberately not served by this public API, so a
 *  decision is identified by its train — which makes this most of its name, and the reason a
 *  missing line still renders as the page's "–" rather than collapsing the pill away. */
export function lineCode(line: string | null): string {
  if (!line) return '–';
  const parts = line.split(':');
  return parts[parts.length - 1];
}

/** The scheduled departure as the header shows it: "10:16" when the train runs today, and
 *  "18. aug. 10:16" when it does not.
 *
 *  Unlabelled and first, where a timetable puts it — so the date has to carry itself, and it
 *  appears precisely when it differs from the reader's own day. That is the rule fmtWhen already
 *  applies to the time one field over, and it is not decoration here: the default window on this
 *  page is fourteen days, so most cards in a full list are not today's. */
export function fmtDeparture(d: Decision): string {
  if (!d.departure_time) return '–';
  const day = d.call_date ?? osloDay(d.departure_time);
  const time = fmtTime(d.departure_time);
  return day === osloDayFmt.format(new Date()) ? time : `${fmtDate(day)} ${time}`;
}
