// A departure with a change on /varsel (multi-leg U10): the «I dag» strip's trip rows, the alert
// card's parts in the plan's order, and the card's texts against the push template. The template's
// side is a GENERATED fixture (test/fixtures/trip-wording.json), rebuilt from
// notification_wording.deterministic_body and notification_title by test_notification_wording.py,
// so a wording change on either side fails one of the two suites.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { circleClass, circleLabel } from '../src/components/dagensReiseModel.ts';
import {
  NOT_EVALUATED,
  alertDriver,
  arrivalLine,
  legRows,
  notEvaluatedNote,
  tripLevel,
  tripNote,
  verdict,
} from '../src/components/tripModel.ts';
import { trainLabel } from '../src/format.ts';

// 2026-10-27 is CET, so Oslo is UTC+1.
const at = (hhmm) => {
  const [h, m] = hhmm.split(':').map(Number);
  return new Date(Date.UTC(2026, 9, 27, h - 1, m)).toISOString();
};

function leg(code, from, to, dep, arr, overrides = {}) {
  return {
    mode: 'rail',
    line: `VYG:Line:${code}`,
    public_code: code,
    from_name: from,
    to_name: to,
    start_time: null,
    end_time: null,
    board_stop_place: `NSR:StopPlace:${from}`,
    alight_stop_place: `NSR:StopPlace:${to}`,
    aimed_departure: at(dep),
    aimed_arrival: at(arr),
    cancelled: false,
    data_source: 'prediction',
    ...overrides,
  };
}

/** AE1: the R13 07:17 Lier -> Drammen, a 4-minute change, the R12 07:30 to Oslo S at 08:05. */
function trip(overrides = {}) {
  return {
    legs: [leg('R13', 'Lier', 'Drammen', '07:17', '07:26'), leg('R12', 'Drammen', 'Oslo S', '07:30', '08:05')],
    transfers: [{ stop_place: 'NSR:StopPlace:Drammen', stop_name: 'Drammen', walk_min: 0, slack_min: 4 }],
    expected_arrival: at('08:05'),
    arrives_via: null,
    problem: null,
    ...overrides,
  };
}

const problem = (kind, index) => ({ kind, index, probability: 0.8, threshold: 0.65 });

/** A trip row on the strip, in its first leg's identity (U9). */
function row(overrides = {}, tripOverrides = {}) {
  return {
    departure_time: at('07:17'),
    arrival_time: at('08:05'),
    operator: 'VYG',
    line: 'VYG:Line:R13',
    jkey: 't17',
    state: 'predicted',
    level: 'green',
    cancelled: false,
    delay_min: null,
    expected_departure_time: null,
    delay_p50_min: 0,
    impact_probability: 0.1,
    alert_worthy: false,
    observed_at: null,
    removed_from_plan: false,
    bus_for_train: false,
    trip: trip(tripOverrides),
    ...overrides,
  };
}

test('the trip model renders «Bytte i Drammen» with the legs in order, and names the problem', () => {
  assert.equal(tripNote(row()), 'Bytte i Drammen');
  assert.deepEqual(legRows(trip(), false), [
    { text: 'Tog R13 07:17 fra Lier til Drammen' },
    { text: 'Tog R12 07:30 fra Drammen til Oslo S' },
  ]);
  // The transfer at fault on the strip, the leg at fault on the card.
  const missed = row({ level: 'red', impact_probability: 0.8, alert_worthy: true }, { problem: problem('missed_connection', 0) });
  assert.equal(circleLabel(missed), 'R13 07:17, fare for å miste byttet i Drammen');
  const card = { mode: 'Tog', line: 'VYG:Line:R13', departure_time: at('07:17'), cancel_probability: null,
    delay_p50_min: null, delay_p90_min: null, impact_probability: 0.1, trip_confirmed: true,
    trip: trip({ problem: problem('leg_cancelled', 1) }) };
  assert.equal(alertDriver(card), 'tog R12 07:30 fra Drammen er innstilt');
});

test('a trip on the strip is keyed on its first leg, and an unknown leg greys it with the leg named', () => {
  const unknown = row({ state: 'unknown', level: 'grey', impact_probability: null }, {
    legs: [
      leg('500', 'Drøbak', 'Oslo bussterminal', '06:40', '07:25', { mode: 'bus', line: 'RUT:Line:500', data_source: 'none' }),
      leg('R12', 'Oslo S', 'Asker', '07:40', '08:05'),
    ],
    transfers: [{ stop_place: 'NSR:StopPlace:Obt', stop_name: 'Oslo bussterminal', walk_min: 6, slack_min: 7 }],
  });
  assert.equal(circleLabel(unknown), 'R13 07:17, ikke vurdert');
  assert.equal(circleClass(unknown), 'dr-circle dr-ring dr-grey');
  assert.equal(tripNote(unknown), 'Bytte i Oslo bussterminal · buss 500 mangler sanntid');
  // «mangler sanntid» is the grey row's own word: an arrived trip's past legs carry no live data
  // either, and saying so under «kom fram i rute» would only be noise.
  const arrived = row({ state: 'confirmed', level: 'green', delay_min: 0 }, { legs: unknown.trip.legs, transfers: unknown.trip.transfers });
  assert.equal(tripNote(arrived), 'Bytte i Oslo bussterminal');
});

test('a trip with an unknown leg is never green, whatever level the row arrives with', () => {
  const noLive = { legs: [leg('R13', 'Lier', 'Drammen', '07:17', '07:26', { data_source: 'none' }), trip().legs[1]] };
  for (const level of ['green', 'yellow']) {
    const r = row({ level, delay_p50_min: 6 }, noLive);
    assert.equal(tripLevel(r), 'grey', level);
    assert.equal(circleClass(r), 'dr-circle dr-ring dr-grey', level);
    assert.equal(circleLabel(r), 'R13 07:17, ikke vurdert', level);
  }
  // Red is the gate's, as for a direct train: a reason on a known leg still alerts (KTD8).
  assert.equal(tripLevel(row({ level: 'red' }, noLive)), 'red');
  // A fact is a fact: an arrival recorded at the destination is graded by its delay.
  assert.equal(tripLevel(row({ state: 'confirmed', level: 'green', delay_min: 0 }, noLive)), 'green');
  // The same legs judged, green stays green.
  assert.equal(tripLevel(row()), 'green');
});

test('every row of the state table renders its text and its level', () => {
  const red = { level: 'red', impact_probability: 0.8, alert_worthy: true };
  const rows = [
    // [what, row, level, text]
    ['every leg known, nothing in trouble', row(), 'green', 'ventet i rute'],
    ['late within the yellow band', row({ level: 'yellow', delay_p50_min: 7 }), 'yellow', 'ventet 7 min forsinket fram'],
    ['a leg unknown', row({ state: 'unknown', level: 'grey', impact_probability: null }), 'grey', 'ikke vurdert'],
    ['change likely missed', row(red, { problem: problem('missed_connection', 0) }), 'red', 'fare for å miste byttet i Drammen'],
    ['later leg cancelled', row({ ...red, cancelled: true, impact_probability: 1 }, { problem: problem('leg_cancelled', 1) }),
      'red', 'toget videre fra Drammen er innstilt'],
    ['later leg out of the plan', row({ ...red, cancelled: true, impact_probability: 1 }, { problem: problem('leg_removed', 1) }),
      'red', 'toget videre fra Drammen er tatt ut av rutetabellen'],
    ['a continuation within 10 min holds', row({ level: 'yellow', delay_p50_min: 6 }, { arrives_via: 'Asker', expected_arrival: at('08:11') }),
      'yellow', 'fram 08:11 via Asker'],
    ['change missed after departure (AE8)', row({ ...red, state: 'confirmed', impact_probability: null, alert_worthy: false, observed_at: at('07:26') },
      { problem: problem('missed_connection', 0), expected_arrival: null }), 'red', 'byttet i Drammen ble ikke nådd'],
    ['arrived on time', row({ state: 'confirmed', delay_min: 0, observed_at: at('08:05') }), 'green', 'kom fram i rute'],
    ['arrived late', row({ state: 'confirmed', level: 'yellow', delay_min: 6, observed_at: at('08:05') }), 'yellow', 'kom fram 6 min forsinket'],
  ];
  for (const [what, r, level, text] of rows) {
    assert.equal(tripLevel(r), level, what);
    assert.ok(circleClass(r).includes(`dr-${level}`), what);
    assert.equal(circleLabel(r), `R13 07:17, ${text}`, what);
  }
  // The server keeps the verdict's problem on a row it grades otherwise; the row's own state speaks.
  const stale = { problem: problem('missed_connection', 0) };
  assert.equal(circleLabel(row({ state: 'confirmed', delay_min: 0 }, stale)), 'R13 07:17, kom fram i rute');
  assert.equal(circleLabel(row({ state: 'unknown', level: 'grey' }, stale)), 'R13 07:17, ikke vurdert');
  // Outside the table, still worded: the first train's own figure, and a later leg's.
  assert.equal(circleLabel(row(red, { problem: problem('departure', 0) })),
    'R13 07:17, fare for over 10 min forsinkelse eller innstilling');
  assert.equal(circleLabel(row(red, { problem: problem('leg_departure', 1) })),
    'R13 07:17, toget videre fra Drammen kan bli forsinket eller innstilt');
  assert.equal(circleLabel(row({ ...red, cancelled: true, state: 'confirmed' }, { problem: problem('leg_cancelled', 0) })),
    'R13 07:17, innstilt');
});

test('a direct first leg is today\'s row, and an unevaluated pair says so under the strip', () => {
  // AE7: a direct row carries no trip, and reads exactly as before.
  const direct = { ...row(), trip: null, line: 'VYG:Line:L1' };
  assert.equal(circleLabel(direct), 'L1 07:17, ventet i rute');
  assert.equal(tripNote(direct), null);
  const { trip: _omitted, ...older } = direct;
  assert.equal(circleLabel(older), 'L1 07:17, ventet i rute', 'a server that predates the field');
  assert.equal(NOT_EVALUATED, 'Reiser med bytte er ikke vurdert ennå.');
  assert.equal(notEvaluatedNote({ transfers: 'not_evaluated', warning: null }), NOT_EVALUATED);
  for (const transfers of ['evaluated', 'none', null, undefined]) {
    assert.equal(notEvaluatedNote({ transfers, warning: null }), null, String(transfers));
  }
  // The service warning already says it, in the copy table's own words.
  assert.equal(notEvaluatedNote({ transfers: 'not_evaluated', warning: 'Vi fant …' }), null);
});

test('three legs collapse the middle one to «+1 bytte», and four to «+2 bytter», in travel order', () => {
  const three = trip({
    legs: [
      leg('500', 'Drøbak', 'Oslo bussterminal', '06:40', '07:25', { mode: 'bus', line: 'RUT:Line:500' }),
      leg('R12', 'Oslo S', 'Lysaker', '07:40', '07:47', { destination: 'Kongsberg' }),
      leg('31', 'Lysaker', 'Fornebu', '07:55', '08:05', { mode: 'bus', line: 'RUT:Line:31', data_source: 'none' }),
    ],
  });
  assert.deepEqual(legRows(three, false), [
    { text: 'Buss 500 06:40 fra Drøbak til Oslo bussterminal' },
    { more: 1, label: '+1 bytte' },
    { text: 'Buss 31 07:55 fra Lysaker til Fornebu · mangler sanntid' },
  ]);
  assert.deepEqual(legRows(three, true).map((r) => r.text), [
    'Buss 500 06:40 fra Drøbak til Oslo bussterminal',
    'Tog R12 Kongsberg 07:40 fra Oslo S til Lysaker',
    'Buss 31 07:55 fra Lysaker til Fornebu · mangler sanntid',
  ]);
  const four = trip({ legs: [...three.legs.slice(0, 2), leg('R11', 'Lysaker', 'Sandvika', '07:50', '07:55'), three.legs[2]] });
  assert.deepEqual(legRows(four, false)[1], { more: 2, label: '+2 bytter' });
  assert.equal(legRows(trip(), false).length, 2, 'two legs are never collapsed');
  // A cancelled leg says so in the list too.
  const cancelled = trip({ legs: [trip().legs[0], { ...trip().legs[1], cancelled: true }] });
  assert.equal(legRows(cancelled, false)[1].text, 'Tog R12 07:30 fra Drammen til Oslo S · innstilt');
});

test('an alert without the trip fields keeps today\'s driver, and has no verdict or arrival line', () => {
  const direct = { mode: 'Tog', line: 'VYG:Line:L1', departure_time: at('07:18'), cancel_probability: 0.1,
    delay_p50_min: 4, delay_p90_min: 9, impact_probability: 0.7 };
  assert.equal(alertDriver(direct), 'Typisk 4–9 min forsinket');
  assert.equal(alertDriver({ ...direct, cancel_probability: 0.62 }), '62 % sjanse for innstilling');
  assert.equal(alertDriver({ ...direct, cancelled: true }), 'Innstilt');
  assert.equal(alertDriver({ ...direct, destination_cancelled: true }), 'Kjører ikke helt fram');
  assert.equal(verdict(direct), null);
  assert.equal(arrivalLine(direct), null);
  assert.equal(verdict({ ...direct, trip: null }), null);
});

test('the card says what the push template says about a trip, word for word', () => {
  const fixture = JSON.parse(readFileSync(new URL('./fixtures/trip-wording.json', import.meta.url), 'utf8'));
  assert.ok(fixture.cases.length >= 10, 'the generated fixture is there');
  for (const { name, alert, title, body } of fixture.cases) {
    assert.equal(verdict(alert), title, name);
    // The body's own shape (deterministic_body): «<train>: <driver>», then the arrival line. The
    // card puts the same two texts on its detail line and the line under it.
    const train = trainLabel(alert.mode, alert.line, alert.departure_time, alert.destination);
    const driver = alertDriver(alert);
    const lines = [driver ? `${train}: ${driver}` : train, arrivalLine(alert)].filter(Boolean);
    assert.equal(lines.join('\n'), body, name);
  }
});
