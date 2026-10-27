// fmtDelay is notification_wording.delay_phrase word for word; test_notification_wording.py's
// test_an_on_time_median_quotes_no_delay pins the same cases on the push side.

import assert from 'node:assert/strict';
import test from 'node:test';

import { fmtDelay } from '../src/format.ts';

test('an on-time median quotes no delay, whatever the p90', () => {
  assert.equal(fmtDelay(0, 3), null);
  assert.equal(fmtDelay(0, 0), null);
  assert.equal(fmtDelay(null, null), null);
});

test('the interval, and one number when only one is known or they coincide', () => {
  assert.equal(fmtDelay(1, 3), 'Typisk 1–3 min forsinket');
  assert.equal(fmtDelay(9, 9), 'Typisk 9 min forsinket');
  assert.equal(fmtDelay(null, 12), 'Typisk 12 min forsinket');
});
