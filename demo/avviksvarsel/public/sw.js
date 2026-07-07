/* Avviksvarsel service worker (scope /varsel/).
 *
 * Two jobs only — there is no offline/caching layer (the app is a thin online client):
 *   push              show the notification the server pushed (title/body/url in the payload)
 *   notificationclick focus an already-open app window, or open /varsel/
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
    icon: '/varsel/icons/pwa-192.png',
    badge: '/varsel/icons/badge-72.png',
    lang: 'nb',
    // Same situation replaces its previous notification instead of stacking.
    tag: data.situation_number || undefined,
    data: { url: data.url || '/varsel/' },
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
            return client.focus();
          }
        }
        return self.clients.openWindow ? self.clients.openWindow(url) : undefined;
      }),
  );
});
