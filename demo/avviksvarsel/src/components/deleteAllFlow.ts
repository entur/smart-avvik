// The «Slett alt» teardown, as a plain function over injected steps.
//
// It lives outside VarselStatus because the ORDER is the load-bearing part and this repo has no
// component-test harness — a plain module is the only thing test/deleteAllFlow.test.mjs can reach
// (the alertFeedbackState.ts pattern). Keep it free of runtime imports and of non-erasable TS
// syntax, so `node --test` can type-strip it.
//
// Server first. The client half — unsubscribing, and forgetting the device id — runs only after
// the server has confirmed, which makes the failure path a true no-op: the id that identifies the
// data is still in localStorage, so the user can simply tap again. The reverse order would delete
// the only handle on data that is still stored, on exactly the request that failed.

export interface DeleteAllSteps {
  /** DELETE /varsel-api/devices/{id}/data — journeys, device record and dedup entries in one go. */
  deleteServerData: () => Promise<unknown>;
  /**
   * Unsubscribe this browser. Best-effort — see below. Must be safe to call when there is no
   * subscription at all (`push.disablePush` looks one up and returns quietly when there is none),
   * so this flow does not look it up a second time itself.
   */
  unsubscribePush: () => Promise<void>;
  /** Last step: remove the stored device id and reset app state. Never runs on the failure path. */
  forgetDevice: () => void;
}

export type DeleteAllOutcome = { status: 'deleted' } | { status: 'failed'; message: string };

/**
 * The window during which no background path may write to the server.
 *
 * VarselStatus's subscription-reconciliation effect is a plain `useEffect`, not one of the
 * busy-gated button handlers, so nothing in «Slett alt» would otherwise suspend it: its
 * `getExistingSubscription()` promise can already be in flight when the user confirms, and when it
 * resolves it calls `registerDevice`. The server's upsert has no tombstone, so that recreates the
 * device record — with a live push subscription — seconds after the dialog said «Alt er slettet»,
 * and invisibly: the call's rejection is swallowed, and the DELETE it undid genuinely succeeded.
 *
 * The flow owns the window rather than the component so that "closed before the first await, and
 * reopened only once every step has settled" is a tested property (test/deleteAllFlow.test.mjs)
 * instead of a hand-maintained pair of assignments. Shaped `{ current }` so the component passes
 * the `useRef(false)` it already keeps for its double-submit guard straight in — one flag, so the
 * two can never disagree about whether a teardown is running.
 */
export interface DeleteAllGate {
  current: boolean;
}

export async function runDeleteAll(
  steps: DeleteAllSteps,
  gate?: DeleteAllGate,
): Promise<DeleteAllOutcome> {
  // Synchronously, before the first await: the reconciliation effect's `.then` callback is a
  // microtask, so a gate closed one tick later is a gate that was open when it mattered.
  if (gate) gate.current = true;
  try {
    return await teardown(steps);
  } finally {
    // `finally`, so a throwing `forgetDevice` (localStorage.removeItem, which a locked-down
    // profile can refuse) cannot leave reconciliation suspended for the rest of the session.
    if (gate) gate.current = false;
  }
}

async function teardown(steps: DeleteAllSteps): Promise<DeleteAllOutcome> {
  try {
    await steps.deleteServerData();
  } catch (e) {
    // Nothing client-side has changed yet, so this is a clean retry point.
    return { status: 'failed', message: e instanceof Error ? e.message : String(e) };
  }

  // Best-effort, and deliberately not fatal: the server record is already gone, and a browser
  // subscription the server has no record of receives nothing. A push-disabled deployment (no
  // VAPID keys, so no subscription was ever made) takes the same path and must still delete.
  try {
    await steps.unsubscribePush();
  } catch {
    // ignored on purpose — see above
  }

  steps.forgetDevice();
  return { status: 'deleted' };
}
