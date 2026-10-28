// A departure with a change on /varsel (plan 2026-10-24-001, U10): the «I dag» strip's trip rows
// and the alert card's parts, pure so `node --test` reaches them. The card's texts restate the push
// template (notification_wording.notification_title and deterministic_body) word for word, pinned
// by the generated test/fixtures/trip-wording.json; the strip's are the plan's state table.

import type { Alert, AlternativeLeg, Journey, TrainStatus, TripFacts, TripLeg } from '../api';
import { CANCEL_LIKELY, fmtDelay, fmtPct, fmtTime, LEG_MODE_NOUN, lineCode, trainLabel, withDestination } from '../format.ts';
import { capitalize } from '../levels.ts';

/** The line under the strip, or the card's footnote, while the pair's trips with a change have not
 *  been judged (no plan yet, or Journey Planner down). The copy table's words. */
export const NOT_EVALUATED = 'Reiser med bytte er ikke vurdert ennå.';

/** «tog RE11» — a transit leg as a departure board names it, mirroring varsel.py's
 *  `_first_leg_label`: lower-case noun mid-clause, and no noun at all for a mode this codebase has
 *  no word for (a guessed vehicle is worse than a missing one). The alternative box and the trip's
 *  own legs both name a leg with it. */
export function legLabel(leg: AlternativeLeg): string | null {
  const noun = leg.mode ? LEG_MODE_NOUN[leg.mode] : undefined;
  const code = withDestination(leg.public_code ?? lineCode(leg.line), leg.destination);
  if (noun && code) return `${noun} ${code}`;
  return code ?? noun ?? null;
}

/** notification_wording.train_label: «tog R12 07:30», «R12 07:30», «tog kl 07:30». */
function boardLabel(noun: string | undefined, code: string | null, hhmm: string, destination?: string | null): string {
  if (code) {
    const named = withDestination(code, destination);
    return noun ? `${noun} ${named} ${hhmm}` : `${named} ${hhmm}`;
  }
  return noun ? `${noun} kl ${hhmm}` : `kl ${hhmm}`;
}

// The template's verbs (notification_wording.outcome_verb), and how it names trouble on a later leg.
const TRANSFER_PROBLEMS = ['missed_connection', 'leg_cancelled', 'leg_removed'];
const LEG_PROBLEM: Record<string, string> = {
  leg_cancelled: 'er innstilt',
  leg_removed: 'er tatt ut av rutetabellen',
  leg_departure: 'kan bli forsinket eller innstilt',
};

/** Does the template quote the first train's figures? Not on a trip whose reason is not the first
 *  train's departure, nor on a confirmed one, nor without a figure of ours (llm_message's guard). */
function quotesFigures(alert: Alert): boolean {
  const trip = alert.trip;
  if (!trip) return true;
  return !(alert.trip_confirmed || (trip.problem?.kind ?? 'departure') !== 'departure' || alert.impact_probability == null);
}

/** Where the change at fault is: transfer `i` for a missed connection, the change onto leg `i` for a
 *  later leg's reason, none for the first train's own departure (llm_message._trip_block). */
function changeAt(trip: TripFacts): string | null {
  const p = trip.problem;
  if (!p || p.kind === 'departure') return null;
  const at = p.kind === 'missed_connection' ? p.index : p.index - 1;
  return trip.transfers[at]?.stop_name ?? null;
}

/** The template's transfer detail line (notification_wording._trip_detail). */
function tripDetail(trip: TripFacts, confirmed: boolean): string | null {
  const p = trip.problem;
  if (!p) return null;
  if (p.kind === 'missed_connection') {
    let line = confirmed ? 'mister overgangen' : 'kan miste overgangen';
    const change = trip.transfers[p.index];
    if (change?.stop_name) line += ` i ${change.stop_name}`;
    if (change && change.slack_min >= 0) line += ` (${change.slack_min} min planlagt byttetid)`;
    return line;
  }
  const verb = LEG_PROBLEM[p.kind];
  if (!verb) return null;
  const leg = trip.legs[p.index];
  const hhmm = leg ? fmtTime(leg.aimed_departure) : '';
  let label = hhmm
    ? boardLabel(LEG_MODE_NOUN[leg.mode ?? ''], leg.public_code || lineCode(leg.line), hhmm, leg.destination)
    : 'en del av reisen';
  if (leg?.from_name) label += ` fra ${leg.from_name}`;
  return `${label} ${verb}`;
}

/** What the card says happens, after the train's label: «Innstilt», «Typisk 15–27 min forsinket»,
 *  or for a trip the leg or change at fault. Word for word `deterministic_body`'s detail, in its
 *  order: a fact first, then the trip's own line, then the first train's figures. */
export function alertDriver(alert: Alert): string | null {
  if (alert.cancelled) return 'Innstilt';
  if (alert.destination_cancelled) return 'Kjører ikke helt fram';
  if (alert.trip) {
    const detail = tripDetail(alert.trip, Boolean(alert.trip_confirmed));
    if (detail || !quotesFigures(alert)) return detail;
  }
  if ((alert.cancel_probability ?? 0) >= CANCEL_LIKELY) return `${fmtPct(alert.cancel_probability ?? 0)} sjanse for innstilling`;
  return fmtDelay(alert.delay_p50_min, alert.delay_p90_min);
}

/** «Ventet framme kl 08:20», or «Ankomst ukjent» when nothing in the plan gets them there; null on a
 *  direct train and on a first train that does not run. */
export function arrivalLine(alert: Alert): string | null {
  if (!alert.trip || alert.cancelled) return null;
  return alert.trip.expected_arrival ? `Ventet framme kl ${fmtTime(alert.trip.expected_arrival)}` : 'Ankomst ukjent';
}

/** The verdict a trip's card opens on, as the push title says it: «Tog R13 07:17 kan miste
 *  overgangen i Drammen, framme kl 08:20». Null on a direct train, whose card is unchanged. */
export function verdict(alert: Alert): string | null {
  const trip = alert.trip;
  if (!trip || !alert.departure_time) return null;
  const kind = trip.problem?.kind ?? 'departure';
  let verb = 'mulig avvik';
  if (alert.cancelled) verb = 'er innstilt';
  else if (alert.destination_cancelled) verb = 'kjører ikke helt fram';
  else if (alert.trip_confirmed) verb = 'mister overgangen';
  else if (TRANSFER_PROBLEMS.includes(kind)) verb = 'kan miste overgangen';
  else if (quotesFigures(alert) && (alert.cancel_probability ?? 0) >= CANCEL_LIKELY) verb = 'kan bli innstilt';
  else if (quotesFigures(alert) && (alert.delay_p50_min || alert.delay_p90_min)) verb = 'kan bli forsinket';
  const train = trainLabel(alert.mode, alert.line, alert.departure_time, alert.destination);
  const title = verb === 'mulig avvik' ? `${train}: ${verb}` : `${train} ${verb}`;
  if (alert.cancelled) return title;
  const at = verb.includes('overgangen') ? changeAt(trip) : null;
  const tail = at ? ` i ${at}` : '';
  return trip.expected_arrival
    ? `${title}${tail}, framme kl ${fmtTime(trip.expected_arrival)}`
    : `${title}${tail}, ankomst ukjent`;
}

/** One leg as the card lists it: «Tog R12 07:30 fra Drammen til Oslo S · mangler sanntid». */
function legText(leg: TripLeg): string {
  const label = legLabel(leg);
  const head = label ? capitalize(label) : 'Etappe';
  let text = `${head} ${fmtTime(leg.aimed_departure)}`;
  if (leg.from_name) text += ` fra ${leg.from_name}`;
  if (leg.to_name) text += ` til ${leg.to_name}`;
  if (leg.cancelled) text += ' · innstilt';
  if (leg.data_source === 'none') text += ' · mangler sanntid';
  return text;
}

export type LegRow = { text: string } | { more: number; label: string };

/** The legs in travel order. With more than two, the middle ones collapse to «+N bytter» until the
 *  traveller opens them: the first leg is the one to catch, the last the one that gets them there. */
export function legRows(trip: TripFacts, expanded: boolean): LegRow[] {
  const rows = trip.legs.map((leg) => ({ text: legText(leg) }));
  if (expanded || rows.length <= 2) return rows;
  const hidden = rows.length - 2;
  return [rows[0], { more: hidden, label: `+${hidden} ${hidden === 1 ? 'bytte' : 'bytter'}` }, rows[rows.length - 1]];
}

/** The strip's level for a trip row. The server's, except that a forecast resting on a leg nothing
 *  could judge this cycle is never green or yellow (R13, KTD14): it is grey. Red stays the gate's, and
 *  a confirmed arrival is a fact, whatever its legs' live data was. */
export function tripLevel(train: TrainStatus): TrainStatus['level'] {
  const unknownLeg = train.trip?.legs.some((leg) => leg.data_source === 'none') ?? false;
  return train.state === 'predicted' && train.level !== 'red' && unknownLeg ? 'grey' : train.level;
}

function names(list: string[]): string {
  return list.length === 1 ? list[0] : `${list.slice(0, -1).join(', ')} og ${list[list.length - 1]}`;
}

/** What a trip row says, by the plan's state table, in the direct row's precedence: a reason the row
 *  carries, an arrival recorded, nothing to say, then the forecast. Null on a direct train. */
export function tripStateText(train: TrainStatus): string | null {
  const trip = train.trip;
  if (!trip) return null;
  // The reason speaks only on a red row: the server keeps the verdict's problem on a row it grades
  // otherwise, an arrival recorded since or a no-SX figure past the horizon (journey_status).
  const p = train.level === 'red' ? trip.problem : null;
  const change = (i: number) => trip.transfers[i]?.stop_name;
  // «toget videre fra Drammen»: where the traveller boards the leg at fault.
  const onward = (i: number) => {
    const from = trip.legs[i]?.from_name ?? change(i - 1);
    return from ? `toget videre fra ${from}` : 'toget videre';
  };
  if (p?.kind === 'leg_removed') return p.index > 0 ? `${onward(p.index)} er tatt ut av rutetabellen` : 'tatt ut av rutetabellen';
  if (p?.kind === 'leg_cancelled') {
    if (p.index > 0) return `${onward(p.index)} er innstilt`;
    return train.state === 'confirmed' ? 'innstilt' : 'meldt innstilt';
  }
  if (p?.kind === 'missed_connection') {
    const at = change(p.index);
    if (train.state === 'confirmed') return at ? `byttet i ${at} ble ikke nådd` : 'byttet ble ikke nådd';
    return at ? `fare for å miste byttet i ${at}` : 'fare for å miste byttet';
  }
  if (train.state === 'confirmed') {
    if (train.delay_min === null) return 'gikk, forsinkelse ikke registrert';
    return train.delay_min <= 0 ? 'kom fram i rute' : `kom fram ${train.delay_min} min forsinket`;
  }
  if (tripLevel(train) === 'grey') return 'ikke vurdert';
  if (p?.kind === 'leg_departure') return `${onward(p.index)} kan bli forsinket eller innstilt`;
  if (train.level === 'red') return 'fare for over 10 min forsinkelse eller innstilling';
  if (trip.arrives_via) {
    const when = trip.expected_arrival ? ` ${fmtTime(trip.expected_arrival)}` : '';
    return `fram${when} via ${trip.arrives_via}`;
  }
  const typ = train.delay_p50_min ?? 0;
  return train.level === 'yellow' && typ > 0 ? `ventet ${typ} min forsinket fram` : 'ventet i rute';
}

/** The line under a trip row's label: «Bytte i Drammen», and on a grey row each leg that has no live
 *  data, «buss 500 mangler sanntid». Null on a direct train. */
export function tripNote(train: TrainStatus): string | null {
  const trip = train.trip;
  if (!trip) return null;
  const at = trip.transfers.map((t) => t.stop_name).filter((n): n is string => Boolean(n));
  const parts = [at.length > 0 ? `Bytte i ${names(at)}` : 'Bytte underveis'];
  if (train.state === 'unknown' || tripLevel(train) === 'grey') {
    for (const leg of trip.legs) {
      if (leg.data_source === 'none') parts.push(`${legLabel(leg) ?? 'en etappe'} mangler sanntid`);
    }
  }
  return parts.join(' · ');
}

/** «Reiser med bytte er ikke vurdert ennå.» when the server says so, and not beside a service warning,
 *  which then says it already. Null for an older server, which sends no `transfers`. */
export function notEvaluatedNote(journey: Pick<Journey, 'transfers' | 'warning'>): string | null {
  return journey.transfers === 'not_evaluated' && !journey.warning ? NOT_EVALUATED : null;
}
