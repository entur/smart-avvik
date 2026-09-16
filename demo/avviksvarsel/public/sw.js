/* Smarte reisevarsler service worker (scope /varsel/).
 *
 * Three jobs — there is no offline/caching layer (the app is a thin online client):
 *   push                    show the notification the server pushed (title/body/url/icon in the payload)
 *   notificationclick       focus an already-open app window, or open the url the payload named
 *   pushsubscriptionchange  re-subscribe when the browser replaces or revokes the subscription, and
 *                           tell the server (2026-09-04) — see renewSubscription below
 *
 * ONE service worker serves every brand: the scope is /varsel/, and ?brand= is a query parameter
 * inside it. So the brand cannot be baked in here — it rides on the payload, which the server
 * builds from the brand the device registered under (see varsel.py's _brand_assets). Both fields
 * fall back to the Entur defaults, so a payload from an older API still shows a correct
 * notification rather than a broken icon.
 */

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { body: event.data && event.data.text ? event.data.text() : '' };
  }
  const title = data.title || 'Smarte reisevarsler';
  const options = {
    body: data.body || '',
    icon: data.icon || '/varsel/icons/pwa-192.png',
    // Not branded: Android draws the badge as a silhouette, so it has no colour to carry.
    badge: '/varsel/icons/badge-72.png',
    lang: 'nb',
    // One notification per TRAIN: the server tags a push with the registration and the train it is
    // about (varsel._notification_tag), so a cancellation that confirms an earlier forecast about
    // the same departure REPLACES it on the lock screen instead of stacking beside it (design
    // review 2026-09-01, finding 14). Older payloads carry no tag; the situation number then does
    // what it always did -- the same situation replaces its own previous notification.
    tag: data.tag || data.situation_number || undefined,
    // A replacement must still be noticed: without this a same-tag notification updates silently.
    renotify: Boolean(data.tag || data.situation_number),
    data: { url: data.url || '/varsel/' },
    // Chromium-based Android browsers render this as a button. Safari ignores unsupported
    // notification actions, so tapping the notification itself remains the complete iPhone path.
    actions: [{ action: 'view-journey', title: 'Se reisen' }],
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/varsel/';
  event.waitUntil(
    self.clients
      .matchAll({ type: 'window', includeUncontrolled: true })
      .then((windows) => {
        for (const client of windows) {
          if (client.url.includes('/varsel') && 'focus' in client) {
            // Focusing alone leaves an already-open app on whichever journey it last showed.
            // Start focusing immediately: some browsers delay or reject navigation, and a tap
            // must still bring the app forward. Body taps and Android's action button share this
            // deep-link behavior.
            const focused = Promise.resolve(client.focus()).then(
              () => true,
              () => false,
            );
            if (!('navigate' in client)) return focused;

            let navigation;
            try {
              navigation = client.navigate(url);
            } catch (_error) {
              return focused.then((didFocus) =>
                !didFocus && self.clients.openWindow ? self.clients.openWindow(url) : undefined,
              );
            }
            return Promise.resolve(navigation).then(
              (navigatedClient) =>
                navigatedClient && 'focus' in navigatedClient
                  ? navigatedClient.focus()
                  : focused,
              () =>
                focused.then((didFocus) =>
                  !didFocus && self.clients.openWindow ? self.clients.openWindow(url) : undefined,
                ),
            );
          }
        }
        return self.clients.openWindow ? self.clients.openWindow(url) : undefined;
      }),
  );
});

/* ---- Subscription renewal --------------------------------------------------------------------
 *
 * The push service can retire a subscription at any time (endpoint rotation, an expired
 * subscription, a reinstalled browser). The server's next push then gets a 404/410 and, after a
 * grace window, forgets the device — and the traveller notices nothing, because a missing
 * notification looks exactly like a calm day. The browser announces the change HERE, with no page
 * open, so this is the only code that can repair it in time. The page stores what the repair needs
 * in IndexedDB (src/pushIdentity.ts — the four names below are its copy, and
 * test/pushIdentity.test.mjs pins them equal): the device id, the brand and the VAPID key as bytes.
 * No record means nothing was registered from this browser, or «Skru av»/«Slett alt» ended it,
 * and then the right answer is to do nothing — never to resurrect a registration the user ended.
 */

const PUSH_DB_NAME = 'avviksvarsel';
const PUSH_DB_VERSION = 1;
const PUSH_STORE = 'push';
const PUSH_IDENTITY_KEY = 'identity';

function readPushIdentity() {
  return new Promise((resolve) => {
    if (typeof indexedDB === 'undefined') return resolve(null);
    let open;
    try {
      open = indexedDB.open(PUSH_DB_NAME, PUSH_DB_VERSION);
    } catch (_error) {
      return resolve(null);
    }
    open.onupgradeneeded = () => {
      // First open on this browser creates the store; there is then nothing in it to read.
      if (!open.result.objectStoreNames.contains(PUSH_STORE)) {
        open.result.createObjectStore(PUSH_STORE);
      }
    };
    open.onerror = () => resolve(null);
    open.onsuccess = () => {
      const db = open.result;
      let get;
      try {
        get = db.transaction(PUSH_STORE, 'readonly').objectStore(PUSH_STORE).get(PUSH_IDENTITY_KEY);
      } catch (_error) {
        db.close();
        return resolve(null);
      }
      get.onerror = () => {
        db.close();
        resolve(null);
      };
      get.onsuccess = () => {
        db.close();
        resolve(get.result || null);
      };
    };
  });
}

async function renewSubscription(event) {
  const identity = await readPushIdentity();
  if (!identity || !identity.device_id) return;
  // Some browsers hand over the replacement; the rest expect us to subscribe again, with the key
  // the old subscription used. `oldSubscription` is not always provided either, hence the copy.
  let subscription = event.newSubscription || null;
  if (!subscription) {
    const applicationServerKey =
      (event.oldSubscription &&
        event.oldSubscription.options &&
        event.oldSubscription.options.applicationServerKey) ||
      identity.application_server_key;
    if (!applicationServerKey) return;
    subscription = await self.registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey,
    });
  }
  // The same upsert the page performs. The server keys on device_id, so the new endpoint simply
  // replaces the dead one — and a record it had already dropped comes back under its original
  // signup time (varsel.upsert_device reads it off the tombstone).
  const response = await fetch('/varsel-api/devices', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      device_id: identity.device_id,
      subscription: subscription.toJSON(),
      brand: identity.brand || 'entur',
    }),
  });
  if (!response.ok) throw new Error('re-registering the device failed: ' + response.status);
}

self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil(renewSubscription(event));
});
