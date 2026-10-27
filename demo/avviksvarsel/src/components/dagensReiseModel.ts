// The «I dag» strip's pure half: which class a circle gets, what a screen reader hears, and where
// the «nå» marker goes (plan 2026-09-22-001, U3). Kept free of React so `node --test` can reach it.
//
// The strip's four-value level vocabulary (red / yellow / green / grey) lives HERE and never in
// `levels.ts`: that file is a byte-identical duplicate across two apps, pinned against the
// server's `togvarsel._LEVEL_TEXT`, and a fourth level there fails `test_togvarsel`.

import type { JourneyStatus, TrainStatus } from '../api';
import { fmtTime, lineCode, withDestination } from '../format.ts';

/** The server grades no-SX figures only this far ahead (journey_status.NOSX_HORIZON_MIN). */
const NOSX_HORIZON_MS = 120 * 60 * 1000;
/** How long past its expected arrival an unrecorded train may still read as under way. Recorded
 *  calls reach the server up to 15 min late (the actuals rebuild), so less would call a train that
 *  arrived on time «ikke registrert» for no reason; past this the arrival is simply missing, which
 *  is what ET does when Vy short-turns a train or never sends its last calls. */
const ARRIVAL_GRACE_MS = 30 * 60 * 1000;

/** `dr-fact` is a filled circle, `dr-ring` a coloured ring; the level picks the colour. */
export function circleClass(train: TrainStatus): string {
  const fill = train.state === 'confirmed' ? 'dr-fact' : 'dr-ring';
  return `dr-circle ${fill} dr-${train.level}${train.cancelled ? ' dr-cancelled' : ''}`;
}

/** What the train is and how it looks, in one line: «L1 07:48, fare for over 10 min forsinkelse
 *  eller innstilling». Doubles as the tapped circle's detail line and as its aria-label. With the
 *  server's `now`, a forecast for a train that has already left says so: under way while its
 *  arrival is still plausibly ahead, «ankomst ikke registrert» once it is well past it. */
export function circleLabel(train: TrainStatus, now?: string): string {
  const name = `${withDestination(lineCode(train.line), train.destination) ?? 'Tog'} ${fmtTime(train.departure_time)}`;
  const t = now === undefined ? null : Date.parse(now);
  const departed =
    t !== null && train.state === 'predicted' && !train.cancelled && Date.parse(train.departure_time) <= t;
  if (!departed) return `${name}, ${stateText(train)}`;
  if (train.arrival_time !== null) {
    const expected = Date.parse(train.arrival_time) + Math.max(train.delay_p50_min ?? 0, 0) * 60_000;
    if (t - expected > ARRIVAL_GRACE_MS) return `${name}, ankomst ikke registrert`;
  }
  return `${name}, underveis, ${stateText(train)}`;
}

function stateText(train: TrainStatus): string {
  // Not «innstilt»: Vy has not cancelled it, and entur.no shows no cancellation (plan 2026-09-23-001).
  if (train.removed_from_plan) {
    return train.bus_for_train ? 'tatt ut av rutetabellen, buss for tog' : 'tatt ut av rutetabellen';
  }
  if (train.cancelled) return train.state === 'confirmed' ? 'innstilt' : 'meldt innstilt';
  if (train.state === 'unknown') return 'ikke vurdert';
  if (train.state === 'confirmed') {
    if (train.delay_min === null) return 'gikk, forsinkelse ikke registrert';
    return train.delay_min <= 0 ? 'kom fram i rute' : `kom fram ${train.delay_min} min forsinket`;
  }
  if (train.level === 'red') return 'fare for over 10 min forsinkelse eller innstilling';
  // A green forecast reads as practically on time (2026-10-23): the server grades a typical delay
  // under 5 min green (journey_status.YELLOW_DELAY_MIN), so the words follow the circle's colour
  // and «ventet 2 min forsinket» is never said. The price, measured on the lead-time holdout: of
  // the trains a disruption message names that this calls on time, 20 % arrive >= 5 min late and
  // 9 % >= 10 (without a message 8 % and 2.4 %). The number itself stays honest for every other
  // use (expected departure, alternatives), which is why this lives here and not in the SQL.
  const typ = train.delay_p50_min ?? 0;
  return train.level === 'yellow' && typ > 0 ? `ventet ${typ} min forsinket` : 'ventet i rute';
}

/** How many trains are left of «nå»: every train whose aimed departure is at or before `now`. */
export function splitAtNow(trains: TrainStatus[], now: string): number {
  const t = Date.parse(now);
  return trains.filter((train) => Date.parse(train.departure_time) <= t).length;
}

/** The newest instant any source was loaded at, for «Oppdatert HH:MM». */
export function updatedAt(status: JourneyStatus): string | null {
  const stamps = Object.values(status.as_of).filter((s): s is string => s !== null);
  if (stamps.length === 0) return null;
  return stamps.reduce((a, b) => (Date.parse(a) >= Date.parse(b) ? a : b));
}

const osloDate = (iso: string) =>
  new Intl.DateTimeFormat('sv-SE', { timeZone: 'Europe/Oslo' }).format(new Date(iso));

/** «I natt» for a window over midnight, which the strip shows until it ends (KIX-22), else «I dag». */
export function stripTitle(status: JourneyStatus): string {
  const w = status.window;
  return w && osloDate(w.start) !== osloDate(w.end) ? 'I natt' : 'I dag';
}

/** The legend's trailing text: the count, or when the model will start saying anything. */
export function caption(status: JourneyStatus): string {
  const trains = status.trains;
  if (trains.length === 0) return `Ingen direkte tog i tidsrommet ${stripTitle(status).toLowerCase()}`;
  const first = Date.parse(trains[0].departure_time);
  const now = Date.parse(status.now);
  if (trains.every((t) => t.state === 'unknown') && first > now) {
    return `Vurderes fra ${fmtTime(new Date(first - NOSX_HORIZON_MS).toISOString())}`;
  }
  return trains.length === 1 ? '1 avgang' : `${trains.length} avganger`;
}
