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

export async function runDeleteAll(steps: DeleteAllSteps): Promise<DeleteAllOutcome> {
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
