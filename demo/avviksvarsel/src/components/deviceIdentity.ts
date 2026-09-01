// The stable per-device id, as a plain module over an injected storage.
//
// Journeys and push subscriptions are keyed to this id, so who may create one is the whole
// contract: only a REGISTRATION action may mint — adding a journey, or turning push on. «Slett
// alt» removes the id while the 60 s poll and the focus/visibilitychange listeners keep running,
// so a single getter that minted for every caller would put a fresh id back in localStorage
// seconds after the deletion (the focus listener fires the moment the user returns to the tab)
// and quietly recreate the identity the user just erased. Reading and minting are therefore two
// functions, and the background paths only ever call the first.
//
// It lives outside App.tsx because that rule was a comment and nothing else, and this repo has no
// component-test harness — a plain module is the only thing test/deviceIdentity.test.mjs can reach
// (the deleteAllFlow.ts + alertFeedbackState.ts pattern). Keep it free of runtime imports and of
// non-erasable TS syntax, so `node --test` can type-strip it.

export const DEVICE_ID_KEY = 'avviksvarsel.device_id';

/** The slice of the Web Storage API this needs. `localStorage` satisfies it structurally. */
export interface DeviceIdStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/**
 * The stored id, or null. Never writes — this is what every background path calls.
 *
 * A storage that refuses reads (Safari with all cookies blocked, some enterprise profiles) throws
 * rather than returning null, and reads as "no id", which is what it is: nothing can have been
 * persisted. App reads this from a `useState` initialiser, so an escaping throw is a blank page.
 */
export function readDeviceId(storage: DeviceIdStorage): string | null {
  try {
    return storage.getItem(DEVICE_ID_KEY);
  } catch {
    return null;
  }
}

/**
 * Create and persist a new id. Registration actions only.
 *
 * Deliberately not wrapped in a try/catch: a mint that cannot be persisted would hand back an id
 * the next reload cannot find, orphaning whatever was registered under it. The caller is a button
 * handler with an error path, so the failure belongs on screen.
 */
export function mintDeviceId(storage: DeviceIdStorage): string {
  const id =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `dev-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  storage.setItem(DEVICE_ID_KEY, id);
  return id;
}

/** Last step of «Slett alt». Synchronous, which is what makes `isStoredDeviceId` a usable guard. */
export function forgetDeviceId(storage: DeviceIdStorage): void {
  storage.removeItem(DEVICE_ID_KEY);
}

/**
 * Is `id` still the live device id?
 *
 * The staleness guard for anything issued in the background: a journey poll, a subscription
 * reconciliation. Storage is the oracle rather than React state because it moves *synchronously*
 * — «Slett alt» has removed the id before any re-render, so a response that resolves in the sliver
 * between the deletion and the repaint is already stale here. It also makes a second tab's
 * deletion visible.
 *
 * `null` is never live, including against an empty storage where `null === null` would say it was.
 * Narrowing (`id is string`) so a caller that guards a write on it does not also need a non-null
 * assertion to hand the id on.
 */
export function isStoredDeviceId(id: string | null, storage: DeviceIdStorage): id is string {
  return id !== null && readDeviceId(storage) === id;
}
