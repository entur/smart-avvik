// The «I dag» strip's pure half (plan 2026-09-22-001, U3): circle classes, the Norwegian labels a
// screen reader hears, and where «nå» goes. `node --test` type-strips the .ts module directly (the
// stopResolver.ts pattern); the React component on top of it has no harness here.

import assert from 'node:assert/strict';
import test from 'node:test';

import { caption, circleClass, circleLabel, splitAtNow, updatedAt } from '../src/components/dagensReiseModel.ts';

// 2026-09-22 is CEST, so Oslo is UTC+2.
function train(hhmm, overrides = {}) {
  const [h, m] = hhmm.split(':').map(Number);
  const dep = new Date(Date.UTC(2026, 8, 22, h - 2, m)).toISOString();
  return {
    departure_time: dep,
    arrival_time: null,
    operator: 'VYG',
    line: 'VYG:Line:L1',
    jkey: `j${hhmm}`,
    state: 'predicted',
    level: 'green',
    cancelled: false,
    delay_min: null,
    expected_departure_time: null,
    delay_p50_min: null,
    impact_probability: 0.05,
    alert_worthy: false,
    observed_at: null,
    ...overrides,
  };
}

function oslo(hhmm) {
  return train(hhmm).departure_time;
}

test('every (state, level) pair maps to a fill class and a colour class', () => {
  for (const state of ['confirmed', 'predicted', 'unknown']) {
    for (const level of ['red', 'yellow', 'green', 'grey']) {
      const cls = circleClass(train('07:00', { state, level }));
      assert.equal(cls, `dr-circle ${state === 'confirmed' ? 'dr-fact' : 'dr-ring'} dr-${level}`);
    }
  }
  assert.equal(
    circleClass(train('07:18', { state: 'confirmed', level: 'red', cancelled: true })),
    'dr-circle dr-fact dr-red dr-cancelled',
  );
});

test('the label names the line, the time and what is known', () => {
  assert.equal(circleLabel(train('07:18', { state: 'confirmed', level: 'red', cancelled: true })), 'L1 07:18, innstilt');
  assert.equal(circleLabel(train('07:18', { level: 'red', cancelled: true })), 'L1 07:18, meldt innstilt');
  assert.equal(
    circleLabel(train('08:11', { line: 'VYG:Line:R13', state: 'unknown', level: 'grey' })),
    'R13 08:11, ikke vurdert',
  );
  assert.equal(
    circleLabel(train('07:48', { level: 'red' })),
    'L1 07:48, fare for over 10 min forsinkelse eller innstilling',
  );
  assert.equal(circleLabel(train('08:18', { level: 'yellow', delay_p50_min: 4 })), 'L1 08:18, ventet 4 min forsinket');
  assert.equal(circleLabel(train('08:33')), 'L1 08:33, ventet i rute');
  assert.equal(circleLabel(train('07:33', { state: 'confirmed', level: 'yellow', delay_min: 5 })), 'L1 07:33, kom fram 5 min forsinket');
  assert.equal(circleLabel(train('07:03', { state: 'confirmed', delay_min: 0 })), 'L1 07:03, kom fram i rute');
  assert.equal(
    circleLabel(train('07:03', { state: 'confirmed', level: 'grey' })),
    'L1 07:03, gikk, forsinkelse ikke registrert',
  );
  assert.equal(circleLabel(train('07:03', { line: null })), 'Tog 07:03, ventet i rute');
});

test('a forecast for a train that has left reads as under way, and only a forecast does', () => {
  const now = oslo('07:42');
  assert.equal(
    circleLabel(train('07:03', { level: 'yellow', delay_p50_min: 6 }), now),
    'L1 07:03, underveis, ventet 6 min forsinket',
  );
  assert.equal(circleLabel(train('07:48', { level: 'yellow', delay_p50_min: 6 }), now), 'L1 07:48, ventet 6 min forsinket');
  assert.equal(circleLabel(train('07:03', { state: 'confirmed', delay_min: 2 }), now), 'L1 07:03, kom fram 2 min forsinket');
  assert.equal(circleLabel(train('07:03', { level: 'red', cancelled: true }), now), 'L1 07:03, meldt innstilt');
});

test('splitAtNow counts the trains at or before now', () => {
  const trains = [train('07:03'), train('07:18'), train('07:48')];
  assert.equal(splitAtNow([], oslo('07:42')), 0);
  assert.equal(splitAtNow(trains, oslo('06:10')), 0, 'all future');
  assert.equal(splitAtNow(trains, oslo('09:30')), 3, 'all past');
  assert.equal(splitAtNow(trains, oslo('07:42')), 2);
  assert.equal(splitAtNow(trains, oslo('07:18')), 2, 'a train due exactly now has left');
});

function status(trains, now, as_of = {}) {
  return {
    now,
    date: '2026-09-22',
    window: { start: oslo('07:00'), end: oslo('09:00') },
    reason: null,
    operators: ['VYG'],
    as_of: { plan_refreshed_at: null, predictions_refreshed_at: null, actuals_refreshed_at: null, ...as_of },
    trains,
  };
}

test('updatedAt is the newest source, and null when none is stamped', () => {
  assert.equal(updatedAt(status([], oslo('07:42'))), null);
  assert.equal(
    updatedAt(status([], oslo('07:42'), { plan_refreshed_at: oslo('07:05'), actuals_refreshed_at: oslo('07:35') })),
    oslo('07:35'),
  );
});

test('the caption counts trains, or says when the model starts saying anything', () => {
  assert.equal(caption(status([train('07:03'), train('07:18')], oslo('07:42'))), '2 avganger');
  assert.equal(caption(status([train('07:03')], oslo('07:42'))), '1 avgang');
  assert.equal(caption(status([], oslo('07:42'))), 'Ingen direkte Vy-tog i tidsrommet i dag');
  const grey = [train('14:03', { state: 'unknown', level: 'grey' }), train('14:18', { state: 'unknown', level: 'grey' })];
  assert.equal(caption(status(grey, oslo('07:42'))), 'Vurderes fra 12:03');
  assert.equal(caption(status(grey, oslo('14:30'))), '2 avganger', 'a window already under way is counted');
});
