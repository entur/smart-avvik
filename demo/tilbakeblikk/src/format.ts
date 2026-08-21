// Display helpers. All times are shown in Europe/Oslo, matching the rest of the stack — and on this
// page that is not merely a convention: the window itself is an Oslo day and an Oslo time of day,
// so a helper that quietly worked in UTC would put the picker and the data a couple of hours apart.

import type { Decision, OutcomeState, ReplayStatus, ReplayedMessage, Verdict } from './api';

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

/** Today in Europe/Oslo as '2026-08-21' — the value the day picker opens on, and the value the API
 *  falls back to when `day` is omitted. Formatted rather than derived from the local clock: a
 *  developer on another offset would otherwise open the page on a day the API is not looking at. */
export function osloToday(): string {
  return osloDayFmt.format(new Date());
}

/** `days` days before today in Europe/Oslo. Used only for the date input's `min`, which mirrors the
 *  API's own lookback bound (historikk._MAX_WINDOW_DAYS): a day further back is a 400, and a picker
 *  that offers it is a picker that misreports what the surface can answer. */
export function osloDaysAgo(days: number): string {
  const d = new Date(`${osloToday()}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

/** The API's 'HH:MM:SS' as the 'HH:MM' an `<input type="time">` holds.
 *
 *  Used to write the RESOLVED window back into the two inputs. Both ends are quantized server-side
 *  (from down, to up, 15-minute grid) and `step={900}` keeps the picker on that grid — but a typed
 *  14:07 still reaches the API, is silently floored to 14:00, and would otherwise leave the control
 *  claiming a minute the data does not cover. */
export function timeInputValue(apiTime: string): string {
  return apiTime.slice(0, 5);
}

/** An instant read against an operating day: "kl. 00:31" when it falls on that day, and
 *  "11. aug. kl. 23:31" when it does not.
 *
 *  The date appears precisely when it differs. A decision taken the evening before is the case a
 *  bare clock time would misrepresent, and it is not rare: a wide registered window is scored from
 *  local midnight, so leads of ten hours and more are ordinary. */
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
    'Fasit er ikke på plass ennå. Nattjobben kjører kl. 04:00 UTC og forsøker på nytt i ti dager, ' +
    'så nesten alt i et vindu fra i dag står her.',
  ukjent:
    'Vi fant ingen registrert avgang for dette toget, og forsøkene har gitt seg. Det telles ikke ' +
    'som «riktig stille» — da ville manglende data sett ut som en riktig avgjørelse.',
};

/** What every window-scoped figure on a card means, in one sentence the card can hang on a marker.
 *
 *  It is the page's most likely misreading: the per-syklus figures are counted over the cycles
 *  INSIDE the chosen window, while the verdict badge beside them comes whole from the nightly job.
 *  «Varslet ikke» next to «Traff» is therefore not a contradiction — it means the alerting happened
 *  before the window opened. */
export const WINDOW_SCOPE_HELP =
  'Regnet bare over syklusene inne i det valgte vinduet. Utfallet ved siden av er derimot hentet ' +
  'for hele avgjørelsens levetid fra nattjobben, så «varslet ikke» sammen med «traff» betyr at ' +
  'varselet gikk før vinduet åpnet — ikke at noe er galt.';

/** The tooltip behind the header's second time: how many cycles scored this train inside the
 *  window, and when the first and last of them did. Undefined for a single look, where the time on
 *  the card is the whole story.
 *
 *  It repeats the last look on purpose. A `title` on a non-interactive element becomes that
 *  element's accessible NAME rather than its description, so anything the tooltip leaves out is not
 *  merely unannounced — it is replaced. */
export function fmtLooks(d: Decision): string | undefined {
  if (!d.n_decisions || d.n_decisions < 2 || !d.first_decided_at) return undefined;
  const first = fmtWhen(d.first_decided_at, d.call_date);
  const last = fmtWhen(d.last_decided_at, d.call_date);
  return `Vurdert ${d.n_decisions} ganger i vinduet — første gang ${first}, sist ${last}.`;
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
 *  line for no reader's benefit. Same reduction /demo and /historikk do, so the pages agree.
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
 *  appears precisely when it differs from the reader's own day. The window on this page is a
 *  single day of DECIDING, not of running, so a card in it can easily be about tomorrow morning's
 *  train: the date is not decoration here either. */
export function fmtDeparture(d: Decision): string {
  if (!d.departure_time) return '–';
  const day = d.call_date ?? osloDay(d.departure_time);
  const time = fmtTime(d.departure_time);
  return day === osloToday() ? time : `${fmtDate(day)} ${time}`;
}

/** What each replay status is called on a card. Every value gets one: a status with no label is an
 *  empty quote, and an empty quote is indistinguishable from "the model wrote nothing" — which is
 *  the one thing none of these ten states means. */
export const REPLAY_STATUS_LABEL: Record<ReplayStatus, string> = {
  ok: 'Skrevet nå',
  no_input: 'Ingen syklus å spille av',
  unreadable_input: 'Ulesbart grunnlag',
  disabled: 'Ingen modell kunne spørres',
  unavailable: 'Ingen backend svarte',
  error: 'Feil mot backend',
  rejected: 'Svaret ble forkastet',
  ungrounded: 'Svaret var ikke forankret',
  capped: 'Utenfor rammen i denne runden',
  rate_limited: 'Minuttkvoten er brukt opp',
  deadline: 'Tiden løp ut',
};

/** The tooltip behind each status. They say what a reader must know to act: whether to wait, to
 *  reload, to look at a deploy switch — or that there is nothing to wait for at all. */
export const REPLAY_STATUS_HELP: Record<ReplayStatus, string> = {
  ok: 'Modellen svarte, og teksten under er svaret.',
  no_input:
    'Ingen varselverdig syklus inne i vinduet lagret et skrivegrunnlag, så det finnes ingenting å ' +
    'spille av. Det betyr ikke «ingen melding»: en syklus under terskelen lagrer også et grunnlag, ' +
    'men den er aldri den som spilles av.',
  unreadable_input:
    'Det lagrede skrivegrunnlaget lot seg ikke tolke. Raden er tatt vare på som den ble skrevet, ' +
    'men den kan ikke spilles av.',
  disabled:
    'Ingen backend kunne spørres — VARSEL_LLM_PROVIDER=off, manglende prosjekt, eller alle ' +
    'backender er låst av. Det betyr ikke at dette miljøet sender standardformuleringen til ' +
    'reisende: det er en annen bryter (VARSEL_LLM_ENABLED), og denne siden spør aldri om den.',
  unavailable: 'Ingen av backendene kunne forsøkes for denne raden.',
  error: 'Forbigående feil mot backend. Last siden på nytt — feil hurtiglagres bare kort.',
  rejected:
    'Modellen svarte, men svaret var tomt, et avslag, eller for langt for et varsel (300 tegn). ' +
    'Teksten under er det som ble kastet. Den er aldri sendt til noen.',
  ungrounded:
    'Modellen svarte med tall som ikke finnes i grunnlaget, så svaret ble kastet framfor å sendes. ' +
    'Teksten under er det som ble kastet. Dette er statusen en promptendring oftest lager.',
  capped:
    'Forespørselen brukte opp rammen sin på modellkall. Last siden på nytt: allerede skrevne ' +
    'tekster kommer fra hurtigbufferen, så neste runde bruker rammen lenger ned i lista.',
  rate_limited:
    'Avspilling har sin egen kvote per minutt, slik at den aldri kan spise av kvoten varsel-løypa ' +
    'bruker til ekte push. Vent et minutt og last på nytt.',
  deadline:
    'Forespørselen har en tidsfrist, og det var ikke rom for et kall til innenfor den. Last på ' +
    'nytt for å fylle flere.',
};

/** The statuses where a model DID answer and the answer was thrown away. Those two are exactly what
 *  a prompt edit produces, so the card shows the discarded text rather than a bare badge. */
export function showsDiscarded(status: ReplayStatus): boolean {
  return status === 'ungrounded' || status === 'rejected';
}

/** The statuses that mean "not attempted, ask again" — a bound that bound, not a verdict about the
 *  prompt. Counted on the page so a half-filled list reads as a budget rather than as a failure. */
export function isBounded(status: ReplayStatus): boolean {
  return status === 'capped' || status === 'rate_limited' || status === 'deadline';
}

/** What identifies a replayed subject across the two responses: the four fields /messages echoes
 *  back for exactly this purpose. Not unique on its own — see alignMessages. */
function subjectKey(s: {
  situation_number: string | null;
  line: string | null;
  mode: string | null;
  departure_time: string | null;
}): string {
  return `${s.situation_number}|${s.line}|${s.mode}|${s.departure_time}`;
}

/** Pair each decision with its regenerated wording.
 *
 *  /decisions and /messages are position-aligned by construction — same window, same filters, same
 *  ORDER BY, same LIMIT — but they are two round trips against a cache with its own TTL, so the page
 *  ASSERTS the alignment instead of trusting it. Position first, since that is the contract; a
 *  content match second, and only when it is UNAMBIGUOUS.
 *
 *  Ambiguity is the normal case, not a defect: the subject's real grain includes `journey_digest`,
 *  which is redacted, so two journeys registered over the same train look identical from out here.
 *  That is the same fact that keeps the list keyed on position. When the position does not match and
 *  the content matches more than once, the answer is null — the card says the wording could not be
 *  attached, which is the honest reading and cheaper than quoting one train's prose under another's.
 */
export function alignMessages(
  decisions: Decision[],
  messages: ReplayedMessage[],
): Array<ReplayedMessage | null> {
  return decisions.map((d, i) => {
    const atPosition = messages[i];
    if (atPosition && subjectKey(atPosition) === subjectKey(d)) return atPosition;
    const matches = messages.filter((m) => subjectKey(m) === subjectKey(d));
    return matches.length === 1 ? matches[0] : null;
  });
}
