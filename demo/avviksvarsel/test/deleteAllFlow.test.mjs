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

// ---- The suspension gate ----------------------------------------------------------------------
//
// VarselStatus's subscription-reconciliation effect is a background `useEffect`, not one of the
// busy-gated button handlers: its getExistingSubscription() promise can already be in flight when
// «Slett alt» is confirmed, and when it resolves it calls registerDevice(). The server's upsert
// has no tombstone, so that recreates the device record — with a live push subscription — seconds
// after the dialog said «Alt er slettet», and the failure is invisible: that call's rejection is
// swallowed and the DELETE it undid genuinely succeeded.
//
// The window a background write must stay out of is a property of the teardown, not of the
// component, so the flow owns it: a `{ current }` box — the shape of the React ref VarselStatus
// already keeps for its double-submit guard — closed synchronously before the first await and
// reopened only once every step has settled, failures included.

/** Stands in for the reconciliation effect: writes only when the gate lets it. */
function reconciler(gate) {
  const writes = [];
  return {
    writes,
    resolveNow: () => {
      if (gate.current) return;
      writes.push('registerDevice');
    },
  };
}

test('the gate closes synchronously and no background write lands during the teardown', async () => {
  const gate = { current: false };
  const { writes, resolveNow } = reconciler(gate);
  const { calls, steps } = recorder({
    // The reconciliation promise can resolve at any point in the flow — before the server has
    // answered, between the DELETE and the unsubscribe, or after the id has been forgotten.
    deleteServerData: async () => {
      calls.push('server');
      resolveNow();
    },
    unsubscribePush: async () => {
      calls.push('unsubscribe');
      resolveNow();
    },
    forgetDevice: () => {
      calls.push('forget');
      resolveNow();
    },
  });

  assert.equal(gate.current, false, 'open before the user confirms');
  const running = runDeleteAll(steps, gate);
  // Synchronously, i.e. before the first await inside the flow: the effect's `.then` callback is
  // a microtask, so a gate closed one tick later is a gate that was open when it mattered.
  assert.equal(gate.current, true, 'closed the moment the teardown starts');

  assert.deepEqual(await running, { status: 'deleted' });
  assert.deepEqual(calls, ['server', 'unsubscribe', 'forget']);
  assert.deepEqual(writes, [], 'nothing re-registered the device mid-delete');
  assert.equal(gate.current, false, 'reopened once the whole flow has settled');
});

test('the gate reopens after a failed server call, so a retry is not locked out', async () => {
  const gate = { current: false };
  const { steps } = recorder({
    deleteServerData: async () => {
      throw new Error('Får ikke kontakt med tjeneren.');
    },
  });
  assert.deepEqual(await runDeleteAll(steps, gate), {
    status: 'failed',
    message: 'Får ikke kontakt med tjeneren.',
  });
  assert.equal(gate.current, false);
});

test('the gate reopens even when the last step throws', async () => {
  // `forgetDevice` reaches localStorage.removeItem, which a locked-down profile can refuse. The
  // error is the caller's to report, but a gate left closed would silently stop subscription
  // reconciliation — and thus brand refreshes — for the rest of the session.
  const gate = { current: false };
  const { steps } = recorder({
    forgetDevice: () => {
      throw new Error('storage is disabled');
    },
  });
  await assert.rejects(runDeleteAll(steps, gate), /storage is disabled/);
  assert.equal(gate.current, false);
});
