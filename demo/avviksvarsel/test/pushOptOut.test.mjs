// «Skru av» leaves the browser permission at `granted`, so afterwards the status card sees exactly
// what an expired subscription looks like — and the silent repair added on 2026-09-04 would turn
// notifications back on at the user's next visit. This flag is what stops it. Tested as a plain
// module, like deviceIdentity.ts.

import assert from 'node:assert/strict';
import test from 'node:test';

import { PUSH_OFF_KEY, clearPushOff, isPushOff, markPushOff } from '../src/components/pushOptOut.ts';

function fakeStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    data,
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => void data.set(key, String(value)),
    removeItem: (key) => void data.delete(key),
  };
}

test('nothing stored means the user did not opt out', () => {
  assert.equal(isPushOff(fakeStorage()), false);
});

test('marking then clearing round-trips', () => {
  const storage = fakeStorage();
  markPushOff(storage);
  assert.equal(isPushOff(storage), true);
  assert.equal(storage.data.get(PUSH_OFF_KEY), '1');
  clearPushOff(storage);
  assert.equal(isPushOff(storage), false);
  assert.equal(storage.data.has(PUSH_OFF_KEY), false);
});

test('a storage that throws reads as "not opted out" and swallows writes', () => {
  const broken = {
    getItem() {
      throw new Error('blocked');
    },
    setItem() {
      throw new Error('blocked');
    },
    removeItem() {
      throw new Error('blocked');
    },
  };
  assert.equal(isPushOff(broken), false);
  assert.doesNotThrow(() => markPushOff(broken));
  assert.doesNotThrow(() => clearPushOff(broken));
});
