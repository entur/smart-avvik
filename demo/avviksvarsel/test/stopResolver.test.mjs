// The from/to pickers' resolver: ranking, the cap, and — the part that shipped wrong — which rows
// are folded into one.
//
// Tested here rather than through AddJourneyModal because this repo has no component-test harness:
// `node --test` type-strips plain .ts modules (the deleteAllFlow.ts + deviceIdentity.ts pattern).
//
// What was wrong (found 2026-09-03): the resolver de-duplicated on `place_id` ALONE, while the
// server (main.refresh_varsel_stops) collapses on the NSR parent place AND the name and keeps
// different names under one parent apart on purpose. «Nationaltheatret stasjon» (rail) and
// «Nationaltheatret» (bus/tram/metro) share one parent; both prefix-match the query, the busier
// bus row ranked first, and the train station was dropped from the list — the picker offered the
// place as bus only. Ski stasjon and Oslo lufthavn stasjon were hidden the same way.

import assert from 'node:assert/strict';
import test from 'node:test';

import { MAX_SHOWN, ambiguousNames, rankStops, stopLabel } from '../src/components/stopResolver.ts';

function stop(stop_place, stop_name, n_calls, { mode = 'Buss', place_id = stop_place, region = 'Oslo' } = {}) {
  return { stop_place, stop_name, mode, n_calls, place_id, region };
}

// The two rows prd served for the place on 2026-09-03, verbatim.
const NATIONALTHEATRET_RAIL = stop('NSR:StopPlace:288', 'Nationaltheatret stasjon', 591, {
  mode: 'Tog',
  place_id: 'NSR:StopPlace:58404',
});
const NATIONALTHEATRET_BUS = stop('NSR:StopPlace:4076', 'Nationaltheatret', 1887, {
  mode: 'Buss',
  place_id: 'NSR:StopPlace:58404',
});

test('two rows of one place under different names both survive — the rail side is not hidden behind the busier bus side', () => {
  const offered = rankStops([NATIONALTHEATRET_BUS, NATIONALTHEATRET_RAIL], 'Nationaltheatret');
  assert.deepEqual(
    offered.map((s) => s.stop_place),
    ['NSR:StopPlace:4076', 'NSR:StopPlace:288'],
  );
});

test('the full rail name reaches the rail row even when the bus name contains it (Ski stasjon)', () => {
  const rail = stop('NSR:StopPlace:1', 'Ski stasjon', 378, { mode: 'Tog', place_id: 'P' });
  const bus = stop('NSR:StopPlace:2', 'Ski stasjon Jernbaneveien', 666, { mode: 'Buss', place_id: 'P' });
  assert.deepEqual(
    rankStops([rail, bus], 'Ski stasjon').map((s) => s.stop_name),
    ['Ski stasjon Jernbaneveien', 'Ski stasjon'],
  );
});

test('two rows of one place under ONE name collapse to the busiest — what an older server would serve', () => {
  const a = stop('NSR:StopPlace:57120', 'Nyvoll ferjekai', 12, { place_id: 'P' });
  const b = stop('NSR:StopPlace:57070', 'Nyvoll ferjekai', 30, { place_id: 'P' });
  assert.deepEqual(
    rankStops([a, b], 'nyvoll').map((s) => s.stop_place),
    ['NSR:StopPlace:57070'],
  );
});

test("the collapse is case- and whitespace-insensitive on the name, like the server's", () => {
  const a = stop('A', 'Nyvoll ferjekai', 12, { place_id: 'P' });
  const b = stop('B', ' NYVOLL FERJEKAI ', 30, { place_id: 'P' });
  assert.equal(rankStops([a, b], 'nyvoll').length, 1);
});

test('unnamed rows are never merged, even under one place', () => {
  const a = stop('A', null, 1, { place_id: 'P' });
  const b = stop('B', null, 2, { place_id: 'P' });
  assert.equal(rankStops([a, b], '').length, 2);
});

test('same-named rows at DIFFERENT places are both offered, and the region label tells them apart', () => {
  const a = stop('A', 'Sentrum', 10, { place_id: 'P1', region: 'Bergen' });
  const b = stop('B', 'Sentrum', 20, { place_id: 'P2', region: 'Tromsø' });
  const offered = rankStops([a, b], 'sentrum');
  assert.equal(offered.length, 2);
  const ambiguous = ambiguousNames([a, b]);
  assert.deepEqual(
    offered.map((s) => stopLabel(s, ambiguous)),
    ['Sentrum (Tromsø)', 'Sentrum (Bergen)'],
  );
});

test('different names under one place are not ambiguous — no region suffix', () => {
  const ambiguous = ambiguousNames([NATIONALTHEATRET_BUS, NATIONALTHEATRET_RAIL]);
  assert.equal(stopLabel(NATIONALTHEATRET_RAIL, ambiguous), 'Nationaltheatret stasjon');
  assert.equal(stopLabel(NATIONALTHEATRET_BUS, ambiguous), 'Nationaltheatret');
});

test('prefix beats word-start beats substring, then traffic', () => {
  const prefix = stop('A', 'Asker stasjon', 1);
  const wordStart = stop('B', 'Nye Asker', 500);
  const substring = stop('C', 'Rasker vei', 900);
  const busierPrefix = stop('D', 'Asker sentrum', 50);
  assert.deepEqual(
    rankStops([substring, wordStart, prefix, busierPrefix], 'asker').map((s) => s.stop_place),
    ['D', 'A', 'B', 'C'],
  );
});

test('a query nothing matches offers nothing; an empty query offers the busiest first', () => {
  const a = stop('A', 'Oslo S', 800);
  const b = stop('B', 'Lysaker', 300);
  assert.deepEqual(rankStops([a, b], 'zzz'), []);
  assert.deepEqual(
    rankStops([b, a], '').map((s) => s.stop_place),
    ['A', 'B'],
  );
});

test('the list is capped at MAX_SHOWN', () => {
  const pool = Array.from({ length: MAX_SHOWN + 25 }, (_, i) => stop(`S${i}`, `Stopp ${i}`, i));
  assert.equal(rankStops(pool, 'stopp').length, MAX_SHOWN);
});
