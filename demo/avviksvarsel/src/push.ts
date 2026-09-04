// Web Push + service-worker plumbing for the PWA.
//
// iOS caveat (Safari >= 16.4): Web Push only works when the app has been added to the Home
// Screen and opened from there (display-mode: standalone). The status card uses isIOS() +
// isStandalone() to detect that and show install instructions instead of a dead "enable" button.

const SW_URL = '/varsel/sw.js';
const SW_SCOPE = '/varsel/';

export function isPushSupported(): boolean {
  return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
}

export function isIOS(): boolean {
  return /iphone|ipad|ipod/i.test(navigator.userAgent);
}

export function isStandalone(): boolean {
  return (
    window.matchMedia('(display-mode: standalone)').matches ||
    // iOS Safari exposes navigator.standalone when launched from the Home Screen.
    (navigator as unknown as { standalone?: boolean }).standalone === true
  );
}

/** VAPID application server key (base64url) -> Uint8Array for pushManager.subscribe.
 *  The `<ArrayBuffer>` argument is required: subscribe() takes a BufferSource, and the
 *  default `Uint8Array<ArrayBufferLike>` also admits SharedArrayBuffer, which is not one. */
function urlBase64ToUint8Array(base64String: string): Uint8Array<ArrayBuffer> {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) output[i] = raw.charCodeAt(i);
  return output;
}

export async function registerServiceWorker(): Promise<ServiceWorkerRegistration> {
  return navigator.serviceWorker.register(SW_URL, { scope: SW_SCOPE });
}

export async function getExistingSubscription(): Promise<PushSubscription | null> {
  if (!isPushSupported()) return null;
  const reg = await navigator.serviceWorker.getRegistration(SW_SCOPE);
  return reg ? reg.pushManager.getSubscription() : null;
}

/** The VAPID public key in the form `pushManager.subscribe` takes — and the form the service
 *  worker stores for renewing the subscription on its own (see pushIdentity.ts). */
export function applicationServerKey(vapidPublicKey: string): Uint8Array<ArrayBuffer> {
  return urlBase64ToUint8Array(vapidPublicKey);
}

/** Subscribe this browser, reusing an existing subscription when there is one. Needs permission
 *  to be `granted` already; no prompt is shown. This is the half `enablePush` runs after asking,
 *  and the whole of the silent repair VarselStatus performs when permission is granted but the
 *  browser has lost its subscription (2026-09-04). */
export async function subscribePush(vapidPublicKey: string): Promise<PushSubscription> {
  const reg = await registerServiceWorker();
  await navigator.serviceWorker.ready;
  const existing = await reg.pushManager.getSubscription();
  return (
    existing ??
    (await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: applicationServerKey(vapidPublicKey),
    }))
  );
}

/** Ask for permission (from a user gesture) and subscribe. Returns the subscription JSON to POST
 *  to the server, or throws with a Norwegian message the UI can show. */
export async function enablePush(vapidPublicKey: string): Promise<PushSubscriptionJSON> {
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') {
    throw new Error('Du må tillate varsler for at appen skal kunne varsle deg.');
  }
  return (await subscribePush(vapidPublicKey)).toJSON();
}

export async function disablePush(): Promise<void> {
  const sub = await getExistingSubscription();
  if (sub) await sub.unsubscribe();
}
