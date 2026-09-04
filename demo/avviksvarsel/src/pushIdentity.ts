// What the service worker needs to renew a push subscription on its own (2026-09-04).
//
// The browser fires `pushsubscriptionchange` in the SERVICE WORKER when it replaces or revokes a
// subscription — with no page open, and therefore with no access to localStorage (the device id),
// to React state (the brand) or to the config endpoint's answer (the VAPID key). Without these the
// worker can re-subscribe but cannot tell the server whose subscription the new endpoint is, so the
// device stays dark until the user happens to open the app. The page therefore writes the three
// into IndexedDB — the one store both contexts share — whenever it registers the device, and clears
// them when the user turns notifications off or erases everything, so the worker never resurrects
// a registration the user ended.
//
// public/sw.js holds its own copy of the four names below (it is a plain script outside the Vite
// bundle and cannot import this module); test/pushIdentity.test.mjs pins the two copies equal.
// Keep this file free of runtime imports and of non-erasable TS syntax, so `node --test` can
// type-strip it.

export const PUSH_DB_NAME = 'avviksvarsel';
export const PUSH_DB_VERSION = 1;
export const PUSH_STORE = 'push';
export const PUSH_IDENTITY_KEY = 'identity';

export interface PushIdentity {
  device_id: string;
  brand: string;
  /** The VAPID public key as `pushManager.subscribe` takes it — bytes, so the worker needs no
   *  base64url decoding of its own. Structured clone carries a Uint8Array through IndexedDB. */
  application_server_key: Uint8Array;
}

/** The slice of IndexedDB this needs; `indexedDB` satisfies it. Injected so tests can fake it. */
export interface IndexedDbLike {
  open(name: string, version?: number): IDBOpenDBRequest;
}

function defaultDb(): IndexedDbLike | null {
  return typeof indexedDB === 'undefined' ? null : indexedDB;
}

function openDb(idb: IndexedDbLike): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = idb.open(PUSH_DB_NAME, PUSH_DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(PUSH_STORE)) db.createObjectStore(PUSH_STORE);
    };
    request.onerror = () => reject(request.error ?? new Error('indexedDB.open failed'));
    request.onsuccess = () => resolve(request.result);
  });
}

function settle<T>(request: IDBRequest<T>, db: IDBDatabase): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onerror = () => {
      db.close();
      reject(request.error ?? new Error('indexedDB request failed'));
    };
    request.onsuccess = () => {
      db.close();
      resolve(request.result);
    };
  });
}

/**
 * Store what the worker needs to renew this device's subscription. Called after every successful
 * device registration, so the record follows a brand change and a new key alike. Rejects when the
 * browser has no IndexedDB or refuses the write; callers treat that as "no renewal on this
 * browser" and carry on — the registration itself has already succeeded.
 */
export async function rememberPushIdentity(
  identity: PushIdentity,
  idb: IndexedDbLike | null = defaultDb(),
): Promise<void> {
  if (!idb) throw new Error('IndexedDB is not available');
  const db = await openDb(idb);
  const store = db.transaction(PUSH_STORE, 'readwrite').objectStore(PUSH_STORE);
  await settle(store.put(identity, PUSH_IDENTITY_KEY), db);
}

/**
 * Forget it — «Skru av» and «Slett alt». The worker then answers a `pushsubscriptionchange` with
 * nothing, which is what keeps it from re-registering a device the user has ended. Quiet when
 * there is nothing to forget or no IndexedDB at all.
 */
export async function forgetPushIdentity(idb: IndexedDbLike | null = defaultDb()): Promise<void> {
  if (!idb) return;
  const db = await openDb(idb);
  const store = db.transaction(PUSH_STORE, 'readwrite').objectStore(PUSH_STORE);
  await settle(store.delete(PUSH_IDENTITY_KEY), db);
}
