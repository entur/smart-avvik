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
