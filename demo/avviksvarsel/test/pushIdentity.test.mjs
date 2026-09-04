// The page side of subscription renewal (src/pushIdentity.ts), and its parity with the service
// worker's copy of the store names. The worker is a plain script outside the bundle and cannot
// import the module, so the four names exist twice; a drift between them is a renewal that reads
// an empty store and silently never re-registers anyone.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  PUSH_DB_NAME,
  PUSH_DB_VERSION,
  PUSH_IDENTITY_KEY,
  PUSH_STORE,
  forgetPushIdentity,
  rememberPushIdentity,
} from '../src/pushIdentity.ts';
import { fakeIndexedDb } from './helpers/fakeIndexedDb.mjs';

const SW = readFileSync(new URL('../public/sw.js', import.meta.url), 'utf8');

function swConstant(name) {
  const match = SW.match(new RegExp(`^const ${name} = (.+);$`, 'm'));
  assert.ok(match, `public/sw.js must define ${name}`);
  return JSON.parse(match[1].replace(/^'(.*)'$/, '"$1"'));
}

test('the service worker uses the same store names as the page', () => {
  assert.equal(swConstant('PUSH_DB_NAME'), PUSH_DB_NAME);
  assert.equal(swConstant('PUSH_DB_VERSION'), PUSH_DB_VERSION);
  assert.equal(swConstant('PUSH_STORE'), PUSH_STORE);
  assert.equal(swConstant('PUSH_IDENTITY_KEY'), PUSH_IDENTITY_KEY);
});

test('remembering stores the identity where the worker reads it, creating the store on first use', async () => {
  const idb = fakeIndexedDb();
  const identity = {
    device_id: 'device-1',
    brand: 'entur',
    application_server_key: new Uint8Array([1, 2, 3]),
  };
  await rememberPushIdentity(identity, idb);

  assert.deepEqual(idb.opens, [[PUSH_DB_NAME, PUSH_DB_VERSION]]);
  assert.equal(idb.stores.get(PUSH_STORE).get(PUSH_IDENTITY_KEY), identity);
  assert.equal(idb.closes, 1, 'the connection is closed after the write');
});

test('remembering again replaces the record — a brand change follows the device', async () => {
  const idb = fakeIndexedDb();
  const key = new Uint8Array([1]);
  await rememberPushIdentity({ device_id: 'device-1', brand: 'entur', application_server_key: key }, idb);
  await rememberPushIdentity({ device_id: 'device-1', brand: 'none', application_server_key: key }, idb);

  assert.equal(idb.stores.get(PUSH_STORE).get(PUSH_IDENTITY_KEY).brand, 'none');
});

test('forgetting empties the store, and is quiet when there is nothing to forget', async () => {
  const idb = fakeIndexedDb({ push: { identity: { device_id: 'device-1' } } });
  await forgetPushIdentity(idb);
  assert.equal(idb.stores.get(PUSH_STORE).has(PUSH_IDENTITY_KEY), false);

  await forgetPushIdentity(idb);
  await forgetPushIdentity(null);
});

test('remembering without IndexedDB rejects rather than pretending', async () => {
  await assert.rejects(
    rememberPushIdentity(
      { device_id: 'device-1', brand: 'entur', application_server_key: new Uint8Array() },
      null,
    ),
    /IndexedDB/,
  );
});
