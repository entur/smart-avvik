// The «Slett alt» teardown order (plan 2026-08-31-001, U6 / R8).
//
// This repo has no component-test harness, so the sequencing lives in a plain module
// (the src/components/alertFeedbackState.ts + test/feedback.test.mjs pattern) and is
// tested here rather than through VarselStatus. What is worth pinning is not the wording
// but the order: the server call happens FIRST, and nothing client-side moves until it has
// succeeded — otherwise a failed delete leaves a user with no device id, no way to reach
// the data that is still on the server, and no way to retry.

import assert from 'node:assert/strict';
import test from 'node:test';

import { runDeleteAll } from '../src/components/deleteAllFlow.ts';

/** Records every step as it runs, so the assertions can be about order, not just about calls. */
function recorder(overrides = {}) {
  const calls = [];
  const steps = {
    deleteServerData: async () => {
      calls.push('server');
    },
    unsubscribePush: async () => {
      calls.push('unsubscribe');
    },
    forgetDevice: () => {
      calls.push('forget');
    },
    ...overrides,
  };
  return { calls, steps };
}

test('the server call comes first, then the subscription, then the device id', async () => {
  const { calls, steps } = recorder();
  const outcome = await runDeleteAll(steps);
  assert.deepEqual(outcome, { status: 'deleted' });
  assert.deepEqual(calls, ['server', 'unsubscribe', 'forget']);
});

test('a failed server call changes nothing client-side and reports the message', async () => {
  const { calls, steps } = recorder({
    deleteServerData: async () => {
      throw new Error('Får ikke kontakt med tjeneren.');
    },
  });
  const outcome = await runDeleteAll(steps);
  assert.deepEqual(outcome, { status: 'failed', message: 'Får ikke kontakt med tjeneren.' });
  // The whole point of server-first: localStorage, the subscription and the app state are
  // untouched, so tapping «Slett alt» again is a clean retry.
  assert.deepEqual(calls, []);
});

test('a non-Error rejection still yields a showable message', async () => {
  const { steps } = recorder({
    deleteServerData: async () => {
      throw 'boom';
    },
  });
  assert.deepEqual(await runDeleteAll(steps), { status: 'failed', message: 'boom' });
});

test('an unsubscribe with nothing to unsubscribe still forgets the device', async () => {
  // The push-disabled deployment, and the user who never turned notifications on: `disablePush`
  // looks for a subscription, finds none and returns quietly. Both must still delete.
  const { calls, steps } = recorder({ unsubscribePush: async () => {} });
  assert.deepEqual(await runDeleteAll(steps), { status: 'deleted' });
  assert.deepEqual(calls, ['server', 'forget']);
});

test('a failing unsubscribe is tolerated — the data is already gone server-side', async () => {
  // Covers a failing subscription lookup too: `disablePush` does that lookup itself, so both
  // surface here as a rejected unsubscribe.
  const { calls, steps } = recorder({
    unsubscribePush: async () => {
      throw new Error('unsubscribe failed');
    },
  });
  assert.deepEqual(await runDeleteAll(steps), { status: 'deleted' });
  assert.deepEqual(calls, ['server', 'forget']);
});
