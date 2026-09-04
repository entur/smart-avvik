// Did the user turn notifications off on THIS device on purpose?
//
// «Skru av» unsubscribes the browser and deletes the server's device record, but it cannot revoke
// the browser permission — that stays `granted`. So on the next visit the status card sees
// permission granted and no subscription, which is exactly what a subscription the push service
// expired looks like, and since 2026-09-04 that state is repaired silently (VarselStatus's
// reconciliation effect re-subscribes without a tap). Without this flag the repair would undo the
// user's choice on their next visit. Set by «Skru av», cleared by «Slå på varsler» and by «Slett
// alt»; read by the repair path only.
//
// A plain module over an injected storage, like deviceIdentity.ts, so test/pushOptOut.test.mjs
// can reach it — this repo has no component-test harness. Keep it free of runtime imports and of
// non-erasable TS syntax, so `node --test` can type-strip it.

import type { DeviceIdStorage } from './deviceIdentity';

export const PUSH_OFF_KEY = 'avviksvarsel.push_off';

/** Record that the user turned notifications off here. */
export function markPushOff(storage: DeviceIdStorage): void {
  try {
    storage.setItem(PUSH_OFF_KEY, '1');
  } catch {
    // A storage that refuses writes cannot remember the choice; the repair path then behaves as it
    // did before this flag existed. Nothing to surface — «Skru av» itself has already succeeded.
  }
}

/** The user turned notifications back on (or erased everything): forget the choice. */
export function clearPushOff(storage: DeviceIdStorage): void {
  try {
    storage.removeItem(PUSH_OFF_KEY);
  } catch {
    // see markPushOff
  }
}

/**
 * True when the user turned notifications off on purpose, so a missing subscription must NOT be
 * repaired. A storage that refuses reads answers false: nothing can have been persisted there,
 * and the repair only ever re-subscribes a browser whose permission is already granted.
 */
export function isPushOff(storage: DeviceIdStorage): boolean {
  try {
    return storage.getItem(PUSH_OFF_KEY) === '1';
  } catch {
    return false;
  }
}
