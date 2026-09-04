import assert from 'node:assert/strict';
import test from 'node:test';

const handlers = new Map();
const shown = [];

globalThis.self = {
  addEventListener(type, handler) {
    handlers.set(type, handler);
  },
  skipWaiting() {},
  clients: {
    claim() {},
    matchAll: async () => [],
    openWindow: async () => undefined,
  },
  registration: {
    showNotification: async (title, options) => shown.push({ title, options }),
  },
};

await import('../public/sw.js');

function dispatch(type, event) {
  let pending;
  handlers.get(type)({
    ...event,
    waitUntil(promise) {
      pending = promise;
    },
  });
  return pending;
}

test('push exposes an Android journey action with the deep link as notification data', async () => {
  const url = '/varsel/?journey=journey-1&situation=S1';
  await dispatch('push', {
    data: { json: () => ({ title: 'Oslo → Bergen', body: 'Signalfeil', url }) },
  });

  assert.deepEqual(shown[0].options.actions, [{ action: 'view-journey', title: 'Se reisen' }]);
  assert.deepEqual(shown[0].options.data, { url });
});

test('notification tap navigates and focuses an already-open app window', async () => {
  const calls = [];
  const navigatedClient = {
    focus: async () => calls.push(['focus-navigated']),
  };
  const client = {
    url: 'https://example.test/varsel/',
    navigate: async (url) => {
      calls.push(['navigate', url]);
      return navigatedClient;
    },
    focus: async () => calls.push(['focus']),
  };
  self.clients.matchAll = async () => [client];

  await dispatch('notificationclick', {
    action: '',
    notification: {
      close: () => calls.push(['close']),
      data: { url: '/varsel/?journey=journey-1&situation=S1' },
    },
  });

  assert.deepEqual(calls, [
    ['close'],
    ['focus'],
    ['navigate', '/varsel/?journey=journey-1&situation=S1'],
    ['focus-navigated'],
  ]);
});

test('notification tap starts focus before a delayed navigation finishes', async () => {
  const calls = [];
  let finishNavigation;
  const navigation = new Promise((resolve) => {
    finishNavigation = resolve;
  });
  const client = {
    url: 'https://example.test/varsel/',
    navigate: (url) => {
      calls.push(['navigate', url]);
      return navigation;
    },
    focus: async () => calls.push(['focus']),
  };
  self.clients.matchAll = async () => [client];

  const pending = dispatch('notificationclick', {
    notification: { close() {}, data: { url: '/varsel/?journey=journey-2' } },
  });
  await Promise.resolve();
  assert.deepEqual(calls, [
    ['focus'],
    ['navigate', '/varsel/?journey=journey-2'],
  ]);

  finishNavigation(client);
  await pending;
});

test('notification tap still focuses when navigation rejects', async () => {
  const calls = [];
  const client = {
    url: 'https://example.test/varsel/',
    navigate: async () => {
      throw new Error('navigation denied');
    },
    focus: async () => calls.push('focus'),
  };
  self.clients.matchAll = async () => [client];

  await dispatch('notificationclick', {
    notification: { close() {}, data: { url: '/varsel/?journey=journey-3' } },
  });

  assert.deepEqual(calls, ['focus']);
});

test('notification tap focuses an open app when navigate is unavailable', async () => {
  const calls = [];
  const client = {
    url: 'https://example.test/varsel/',
    focus: async () => calls.push('focus'),
  };
  self.clients.matchAll = async () => [client];

  await dispatch('notificationclick', {
    notification: { close() {}, data: { url: '/varsel/?journey=journey-4' } },
  });

  assert.deepEqual(calls, ['focus']);
});

test('Android action opens the notification deep link when the app is closed', async () => {
  const opened = [];
  self.clients.matchAll = async () => [];
  self.clients.openWindow = async (url) => opened.push(url);

  await dispatch('notificationclick', {
    action: 'view-journey',
    notification: {
      close() {},
      data: { url: '/varsel/?journey=journey-1&situation=S1' },
    },
  });

  assert.deepEqual(opened, ['/varsel/?journey=journey-1&situation=S1']);
});

// ---- pushsubscriptionchange (2026-09-04) ----------------------------------------------------

import { fakeIndexedDb } from './helpers/fakeIndexedDb.mjs';

const KEY_BYTES = new Uint8Array([4, 1, 2, 3]);

function identityStore(identity) {
  return fakeIndexedDb({ push: identity ? { identity } : {} });
}

function renewalHarness({ identity, subscribeResult, fetchStatus = 200 } = {}) {
  const calls = { subscribe: [], fetch: [] };
  globalThis.indexedDB = identityStore(identity);
  self.registration.pushManager = {
    subscribe: async (options) => {
      calls.subscribe.push(options);
      return subscribeResult ?? { toJSON: () => ({ endpoint: 'https://push.example/new' }) };
    },
  };
  globalThis.fetch = async (url, init) => {
    calls.fetch.push([url, init]);
    return { ok: fetchStatus < 400, status: fetchStatus };
  };
  return calls;
}

test('a subscription change re-subscribes with the stored key and re-registers the device', async () => {
  const calls = renewalHarness({
    identity: { device_id: 'device-1', brand: 'none', application_server_key: KEY_BYTES },
  });

  await dispatch('pushsubscriptionchange', {});

  assert.equal(calls.subscribe.length, 1);
  assert.equal(calls.subscribe[0].userVisibleOnly, true);
  assert.equal(calls.subscribe[0].applicationServerKey, KEY_BYTES);
  assert.equal(calls.fetch.length, 1);
  const [url, init] = calls.fetch[0];
  assert.equal(url, '/varsel-api/devices');
  assert.equal(init.method, 'POST');
  assert.deepEqual(JSON.parse(init.body), {
    device_id: 'device-1',
    subscription: { endpoint: 'https://push.example/new' },
    brand: 'none',
  });
});

test('the old subscription’s own key wins over the stored copy when the browser provides it', async () => {
  const calls = renewalHarness({
    identity: { device_id: 'device-1', brand: 'entur', application_server_key: KEY_BYTES },
  });
  const liveKey = new Uint8Array([9, 9]);

  await dispatch('pushsubscriptionchange', {
    oldSubscription: { options: { applicationServerKey: liveKey } },
  });

  assert.equal(calls.subscribe[0].applicationServerKey, liveKey);
});

test('a replacement the browser hands over is registered without subscribing again', async () => {
  const calls = renewalHarness({
    identity: { device_id: 'device-1', brand: 'entur', application_server_key: KEY_BYTES },
  });

  await dispatch('pushsubscriptionchange', {
    newSubscription: { toJSON: () => ({ endpoint: 'https://push.example/handed-over' }) },
  });

  assert.equal(calls.subscribe.length, 0);
  assert.equal(JSON.parse(calls.fetch[0][1].body).subscription.endpoint, 'https://push.example/handed-over');
});

test('with no stored identity the worker does nothing — «Skru av» and «Slett alt» must stay ended', async () => {
  const calls = renewalHarness({ identity: null });

  await dispatch('pushsubscriptionchange', {});

  assert.deepEqual(calls.subscribe, []);
  assert.deepEqual(calls.fetch, []);
});

test('a missing brand registers as entur, matching the server default for pre-brand records', async () => {
  const calls = renewalHarness({
    identity: { device_id: 'device-1', application_server_key: KEY_BYTES },
  });

  await dispatch('pushsubscriptionchange', {});

  assert.equal(JSON.parse(calls.fetch[0][1].body).brand, 'entur');
});

test('a rejected re-registration rejects waitUntil, so the browser can retry the event', async () => {
  renewalHarness({
    identity: { device_id: 'device-1', brand: 'entur', application_server_key: KEY_BYTES },
    fetchStatus: 503,
  });

  await assert.rejects(dispatch('pushsubscriptionchange', {}), /503/);
});

test('a browser without IndexedDB is left alone', async () => {
  const calls = renewalHarness({ identity: null });
  delete globalThis.indexedDB;

  await dispatch('pushsubscriptionchange', {});

  assert.deepEqual(calls.fetch, []);
});
