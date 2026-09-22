// The «I dag» strip's pure half: which class a circle gets, what a screen reader hears, and where
// the «nå» marker goes (plan 2026-09-22-001, U3). Kept free of React so `node --test` can reach it.
//
// The strip's four-value level vocabulary (red / yellow / green / grey) lives HERE and never in
// `levels.ts`: that file is a byte-identical duplicate across two apps, pinned against the
// server's `togvarsel._LEVEL_TEXT`, and a fourth level there fails `test_togvarsel`.

import type { JourneyStatus, TrainStatus } from '../api';
import { fmtTime, lineCode } from '../format.ts';

/** The server grades no-SX figures only this far ahead (journey_status.NOSX_HORIZON_MIN). */
const NOSX_HORIZON_MS = 120 * 60 * 1000;

/** `dr-fact` is a filled circle, `dr-ring` a coloured ring; the level picks the colour. */
export function circleClass(train: TrainStatus): string {
  const fill = train.state === 'confirmed' ? 'dr-fact' : 'dr-ring';
  return `dr-circle ${fill} dr-${train.level}${train.cancelled ? ' dr-cancelled' : ''}`;
}

/** What the train is and how it looks, in one line: «L1 07:48, fare for over 10 min forsinkelse
 *  eller innstilling». Doubles as the tapped circle's detail line and as its aria-label. With the
 *  server's `now`, a forecast for a train that has already left (under way, its arrival not yet
 *  recorded) says so instead of reading as a train still to come. */
export function circleLabel(train: TrainStatus, now?: string): string {
  const name = `${lineCode(train.line) ?? 'Tog'} ${fmtTime(train.departure_time)}`;
  const underway =
    now !== undefined &&
    train.state === 'predicted' &&
    !train.cancelled &&
    Date.parse(train.departure_time) <= Date.parse(now);
  return `${name}, ${underway ? 'underveis, ' : ''}${stateText(train)}`;
}

function stateText(train: TrainStatus): string {
  if (train.cancelled) return train.state === 'confirmed' ? 'innstilt' : 'meldt innstilt';
  if (train.state === 'unknown') return 'ikke vurdert';
  if (train.state === 'confirmed') {
    if (train.delay_min === null) return 'gikk, forsinkelse ikke registrert';
    return train.delay_min <= 0 ? 'kom fram i rute' : `kom fram ${train.delay_min} min forsinket`;
  }
  if (train.level === 'red') return 'fare for over 10 min forsinkelse eller innstilling';
  const typ = train.delay_p50_min ?? 0;
  return typ > 0 ? `ventet ${typ} min forsinket` : 'ventet i rute';
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

/** The legend's trailing text: the count, or when the model will start saying anything. */
export function caption(status: JourneyStatus): string {
  const trains = status.trains;
  if (trains.length === 0) return 'Ingen direkte Vy-tog i tidsrommet i dag';
  const first = Date.parse(trains[0].departure_time);
  const now = Date.parse(status.now);
  if (trains.every((t) => t.state === 'unknown') && first > now) {
    return `Vurderes fra ${fmtTime(new Date(first - NOSX_HORIZON_MS).toISOString())}`;
  }
  return trains.length === 1 ? '1 avgang' : `${trains.length} avganger`;
}
