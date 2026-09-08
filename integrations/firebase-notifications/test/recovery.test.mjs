// The recovery sweep, and the staleness cutoff that bounds it.
//
// Scenario 7 of the unit's test plan: the sweep processes valid pending work after the trigger's
// retries are exhausted, and the cutoff stops it processing anything past expiry.

import { test } from "node:test";
import assert from "node:assert/strict";

import { DEFAULT_PROCESSING_CUTOFF_MS } from "@entur/kix-notifications-client";

import {
  DEFAULT_SWEEP_LIMIT,
  DEFAULT_SWEEP_MIN_AGE_MS,
  runRecoverySweep,
} from "../src/recovery.ts";
import { NOW, makeEvent, memoryAdapters } from "./helpers/adapters.mjs";

test("a pending record the trigger never processed is picked up and dispatched", async () => {
  // The record was accepted and nothing ever ran for it -- an Eventarc incident, a redeploy
  // mid-flight, a worker that exhausted the retry window against a store that was down all night.
  // Nothing about that state is an error anywhere a partner would look, which is why the sweep
  // exists at all.
  const later = new Date(NOW.getTime() + DEFAULT_SWEEP_MIN_AGE_MS + 60_000);
  const harness = memoryAdapters({ now: () => later });
  const { record } = await harness.accepted(
    makeEvent({ valid_until: "2026-09-09T23:00:00+00:00" }),
  );
  assert.equal(harness.stores.state.inbox.get(record.eventId).status, "pending");

  const result = await runRecoverySweep(harness.adapters, { now: () => later });
  assert.deepEqual(result, { examined: 1, dispatched: 1, suppressed: 0, expired: 0, failed: 0 });
  assert.equal(harness.dispatched.length, 1);
  assert.equal(harness.stores.state.inbox.get(record.eventId).status, "dispatched");
});

test("a record younger than the minimum age is left to the trigger", async () => {
  // The trigger is probably working on it RIGHT NOW. Sweeping it would race for no benefit: both
  // would reach the same decision and the second would find it already made. The sweep is the net,
  // not the path.
  const harness = memoryAdapters();
  await harness.accepted();
  const result = await runRecoverySweep(harness.adapters, { now: () => NOW });
  assert.equal(result.examined, 0);
  assert.equal(harness.dispatched.length, 0);
});

test("the staleness cutoff stops the sweep dispatching anything past expiry", async () => {
  const harness = memoryAdapters();
  const { record } = await harness.accepted();
  const wayLater = new Date(NOW.getTime() + DEFAULT_PROCESSING_CUTOFF_MS + 60_000);
  const result = await runRecoverySweep(harness.adapters, { now: () => wayLater });
  assert.equal(result.examined, 1);
  assert.equal(result.expired, 1);
  assert.equal(result.dispatched, 0);
  assert.equal(harness.dispatched.length, 0);
  assert.equal(harness.stores.state.inbox.get(record.eventId).status, "expired");
});

test("a record the sweep expires leaves `pending` and is never re-read", async () => {
  // Why there is no separate sweep horizon: one pass settles an ancient record, so it drops out of
  // the pending query by itself. A second definition of "too old" would be a second thing to keep in
  // step with the first.
  const harness = memoryAdapters();
  await harness.accepted();
  const wayLater = new Date(NOW.getTime() + DEFAULT_PROCESSING_CUTOFF_MS + 60_000);
  await runRecoverySweep(harness.adapters, { now: () => wayLater });
  const second = await runRecoverySweep(harness.adapters, { now: () => wayLater });
  assert.equal(second.examined, 0);
});

test("a record that was already dispatched is not dispatched again by a sweep", async () => {
  const later = new Date(NOW.getTime() + DEFAULT_SWEEP_MIN_AGE_MS + 60_000);
  const harness = memoryAdapters({ now: () => later });
  const { record } = await harness.accepted(
    makeEvent({ valid_until: "2026-09-09T23:00:00+00:00" }),
  );
  await runRecoverySweep(harness.adapters, { now: () => later });
  const again = await runRecoverySweep(harness.adapters, { now: () => later });
  assert.equal(again.examined, 0, "the record is no longer pending");
  assert.equal(harness.dispatched.length, 1);
  assert.equal(harness.stores.state.inbox.get(record.eventId).status, "dispatched");
});

test("one record's failure does not abandon the rest of the backlog", async () => {
  // Per-record isolation, and the same argument the sending side makes about its own pass: an
  // unreadable document suppresses that record's work and nothing else. A sweep that propagated the
  // first failure would process the backlog in the order of its worst record.
  const later = new Date(NOW.getTime() + DEFAULT_SWEEP_MIN_AGE_MS + 60_000);
  let seen = 0;
  const harness = memoryAdapters({
    now: () => later,
    async dispatch(request) {
      seen += 1;
      if (seen === 1) {
        throw new Error("firestore unavailable");
      }
      return undefined;
    },
  });
  for (const revision of [1, 2, 3]) {
    await harness.accepted(
      makeEvent({
        revision,
        alert_id: `sub_a:VYG:L1:2026-09-09:${revision}`,
        valid_until: "2026-09-09T23:00:00+00:00",
      }),
    );
  }
  const errors = [];
  const result = await runRecoverySweep(harness.adapters, {
    now: () => later,
    onError: (eventId, error) => errors.push([eventId, error.message]),
  });
  assert.equal(result.examined, 3);
  assert.equal(result.failed, 1);
  assert.equal(result.dispatched, 2);
  assert.equal(errors.length, 1);
});

test("a failed record stays pending and is retried by the NEXT sweep", async () => {
  const later = new Date(NOW.getTime() + DEFAULT_SWEEP_MIN_AGE_MS + 60_000);
  let attempts = 0;
  const harness = memoryAdapters({
    now: () => later,
    async dispatch() {
      attempts += 1;
      if (attempts === 1) {
        throw new Error("transient");
      }
    },
  });
  await harness.accepted(makeEvent({ valid_until: "2026-09-09T23:00:00+00:00" }));
  assert.equal((await runRecoverySweep(harness.adapters, { now: () => later })).failed, 1);
  assert.equal((await runRecoverySweep(harness.adapters, { now: () => later })).dispatched, 1);
});

test("the sweep is bounded, so a backlog cannot become the outage", async () => {
  const later = new Date(NOW.getTime() + DEFAULT_SWEEP_MIN_AGE_MS + 60_000);
  const harness = memoryAdapters({ now: () => later });
  for (let index = 0; index < 5; index += 1) {
    await harness.accepted(
      makeEvent({
        revision: index + 1,
        alert_id: `sub_a:VYG:L1:2026-09-09:${index}`,
        valid_until: "2026-09-09T23:00:00+00:00",
      }),
    );
  }
  const result = await runRecoverySweep(harness.adapters, { now: () => later, limit: 2 });
  assert.equal(result.examined, 2);
  assert.equal(DEFAULT_SWEEP_LIMIT, 100);
});
