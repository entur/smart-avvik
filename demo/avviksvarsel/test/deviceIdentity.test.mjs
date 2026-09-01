// The device-id lifecycle: who may mint one, and what counts as the live one.
//
// The rule this pins used to be a comment in App.tsx and nothing else. Only a REGISTRATION action
// may mint — adding a journey, or turning push on. «Slett alt» removes the id while the 60 s poll
// and the focus/visibilitychange listeners keep running, so a getter that minted for every caller
// would put a fresh id back in localStorage seconds after the deletion (the focus listener fires
// the moment the user returns to the tab) and quietly recreate the identity the user just erased.
//
// `isStoredDeviceId` is the other half: a background response has to be dropped when the id it was
// issued for is no longer the live one, or a poll dispatched just before «Slett alt» repaints the
// deleted journeys under the «Alt er slettet» notice — and never clears again, because the rebound
// closure short-circuits on the null id.
//
// Tested here rather than through App because this repo has no component-test harness: `node
// --test` type-strips plain .ts modules (the deleteAllFlow.ts + alertFeedbackState.ts pattern).

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DEVICE_ID_KEY,
  forgetDeviceId,
  isStoredDeviceId,
  mintDeviceId,
  readDeviceId,
} from '../src/components/deviceIdentity.ts';

/** A localStorage stand-in that records every write, so "does not mint" is checkable. */
function fakeStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  const writes = [];
  return {
    writes,
    data,
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => {
      writes.push(['set', key, value]);
      data.set(key, String(value));
    },
    removeItem: (key) => {
      writes.push(['remove', key]);
      data.delete(key);
    },
  };
}

test('reading an existing id returns it and writes nothing', () => {
  const storage = fakeStorage({ [DEVICE_ID_KEY]: 'existing-id' });
  assert.equal(readDeviceId(storage), 'existing-id');
  assert.deepEqual(storage.writes, [], 'a read must never touch storage');
});

test('reading when there is nothing stored returns null and does NOT mint', () => {
  const storage = fakeStorage();
  assert.equal(readDeviceId(storage), null);
  assert.deepEqual(storage.writes, []);
});

test('minting persists a new id and returns it', () => {
  const storage = fakeStorage();
  const id = mintDeviceId(storage);
  assert.equal(typeof id, 'string');
  assert.ok(id.length >= 8, 'the API constrains device_id to 8..100 characters');
  assert.equal(storage.getItem(DEVICE_ID_KEY), id, 'persisted, or a reload orphans the journeys');
  assert.deepEqual(storage.writes, [['set', DEVICE_ID_KEY, id]]);
});

test('two mints are different ids', () => {
  assert.notEqual(mintDeviceId(fakeStorage()), mintDeviceId(fakeStorage()));
});

test('forgetting clears the stored id', () => {
  const storage = fakeStorage({ [DEVICE_ID_KEY]: 'existing-id' });
  forgetDeviceId(storage);
  assert.equal(storage.getItem(DEVICE_ID_KEY), null);
  assert.deepEqual(storage.writes, [['remove', DEVICE_ID_KEY]]);
});

test('a read after forget returns null and does NOT mint', () => {
  // The «Slett alt» invariant. The poll and the focus/visibilitychange listeners keep running
  // after the deletion and they all read; if any of them minted, the erased identity would be
  // back in localStorage before the user had finished reading the confirmation.
  const storage = fakeStorage({ [DEVICE_ID_KEY]: 'existing-id' });
  forgetDeviceId(storage);
  storage.writes.length = 0;
  assert.equal(readDeviceId(storage), null);
  assert.equal(readDeviceId(storage), null);
  assert.deepEqual(storage.writes, []);
});

test('a storage that refuses reads has no id, and still does not mint', () => {
  // A locked-down profile (Safari with all cookies blocked, some enterprise policies) throws on
  // localStorage access rather than returning null. A device with no readable storage has no
  // stored id by definition — and App reads this from a useState initialiser, so a throw there
  // is a blank page.
  const storage = {
    getItem: () => {
      throw new Error('The operation is insecure.');
    },
    setItem: () => assert.fail('must not mint'),
    removeItem: () => {},
  };
  assert.equal(readDeviceId(storage), null);
});

test('the live id is the stored one — and after «Slett alt» nothing is', () => {
  const storage = fakeStorage({ [DEVICE_ID_KEY]: 'a' });
  assert.equal(isStoredDeviceId('a', storage), true);
  assert.equal(isStoredDeviceId('b', storage), false, 'a response for a superseded id is stale');
  forgetDeviceId(storage);
  assert.equal(isStoredDeviceId('a', storage), false, 'the poll must not repaint deleted journeys');
});

test('a null id is never the live one', () => {
  // The staleness guard is asked about the id a fetch was issued for; "no id" can never match,
  // including on an empty storage, where a `null === null` comparison would say it did.
  assert.equal(isStoredDeviceId(null, fakeStorage()), false);
  assert.equal(isStoredDeviceId(null, fakeStorage({ [DEVICE_ID_KEY]: 'a' })), false);
});

test('a freshly minted id is immediately the live one', () => {
  // What makes the guard safe for the re-registration path: minting is synchronous, so the next
  // background response — issued for the old id — is stale the instant a new id exists.
  const storage = fakeStorage({ [DEVICE_ID_KEY]: 'old' });
  forgetDeviceId(storage);
  const fresh = mintDeviceId(storage);
  assert.equal(isStoredDeviceId(fresh, storage), true);
  assert.equal(isStoredDeviceId('old', storage), false);
});
