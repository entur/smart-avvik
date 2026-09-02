/* Avviksvarsel service worker (scope /varsel/).
 *
 * Two jobs only — there is no offline/caching layer (the app is a thin online client):
 *   push              show the notification the server pushed (title/body/url/icon in the payload)
 *   notificationclick focus an already-open app window, or open the url the payload named
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
  const title = data.title || 'Avviksvarsel';
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
