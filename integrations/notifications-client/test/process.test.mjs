// The trigger half: policy, state, eligibility, dispatch -- and the error classification that makes
// the staleness cutoff mean anything.
//
// With a retry-enabled trigger, a THROWN error is redelivered for up to 24 hours. So which failures
// throw is not a style question: throw on a payload fault and one bad event re-runs every few
// minutes for a day, each run another chance of a duplicate notification about a train that left
// hours ago; return normally on a store outage and the event is lost. The split here is the one the
// sending side already made between payload failures and transient ones, expressed as "everything
// this function decides for itself returns; everything thrown propagates".

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_PROCESSING_CUTOFF_MS,
  TerminalReceiverError,
  process as processRecord,
  receive,
} from "../src/client.ts";
import { createMemoryStores } from "../src/ledger.ts";
import { TEST_SECRET, makeEvent } from "./helpers/events.mjs";
import { NOW, signedRequest } from "./helpers/signed.mjs";

function harness(overrides = {}) {
  const stores = createMemoryStores();
  const dispatched = [];
  const recipientCalls = [];
  const adapters = {
    inbox: stores.inbox,
    ledger: stores.ledger,
    suppressions: stores.suppressions,
    async recipients(event) {
      recipientCalls.push(["recipients", event.event_id]);
      return ["device-1"];
    },
    async testRecipients(event) {
      recipientCalls.push(["testRecipients", event.event_id]);
      return ["test-device"];
    },
    async eligibility() {
      return { ok: true };
    },
    async dispatch(request) {
      dispatched.push(request);
    },
    now: () => NOW,
    ...overrides,
  };
  return { stores, adapters, dispatched, recipientCalls };
}

async function accept(stores, event, now = NOW) {
  const result = await receive(
    { secrets: [TEST_SECRET], inbox: stores.inbox, now: () => now },
    signedRequest({ event, now }),
  );
  assert.equal(result.status, 200, result.detail);
  return stores.inbox.get(event.event_id);
}

test("a valid event is dispatched once, and the state is applied", async () => {
  const { stores, adapters, dispatched } = harness();
  const event = makeEvent({ revision: 4 });
  const outcome = await processRecord(adapters, await accept(stores, event));
  assert.deepEqual(outcome, { outcome: "dispatched", recipients: 1 });
  assert.equal(dispatched.length, 1);
  assert.equal(dispatched[0].display, "alert");
  assert.deepEqual(await stores.ledger.standing(event.alert_id), { revision: 4, phase: "predicted" });
  assert.equal(stores.state.inbox.get(event.event_id).status, "dispatched");
});

test("AE5, second half: a redelivery after restart produces ONE native decision", async () => {
  // "After restart" is modelled by re-entering `process` on the record that survived -- which is
  // exactly what the trigger does when a worker dies, and what Eventarc does when it redelivers.
  // Three layers stop a second notification and they stop it at three different points, which is
  // why none of them is redundant: the inbox `create` at the HTTP boundary, the freshness read here,
  // and the ledger's own duplicate rule for the window between the two settles (below).
  const { stores, adapters, dispatched } = harness();
  const record = await accept(stores, makeEvent({ revision: 4 }));
  await processRecord(adapters, record);
  const second = await processRecord(adapters, record);
  assert.deepEqual(second, { outcome: "already_settled", status: "dispatched" });
  assert.equal(dispatched.length, 1, "one native decision, one dispatch");
});

test("rule 1 still bites in the window between the ledger settle and the inbox settle", async () => {
  // A crash there leaves the ledger finished and the inbox `pending`, so the freshness read lets the
  // re-entry through and the duplicate rule is the thing that stops a second notification. Narrow,
  // real, and the reason the rule is not made redundant by the guard above it.
  const { stores, adapters, dispatched } = harness();
  const record = await accept(stores, makeEvent({ revision: 4 }));
  await adapters.ledger.decide(record.event);
  await adapters.ledger.settle(record.eventId);

  const outcome = await processRecord(adapters, record);
  assert.equal(outcome.outcome, "suppressed");
  assert.equal(outcome.rule, "duplicate_event");
  assert.equal(dispatched.length, 0);
});

test("two workers racing on one record make ONE decision; dispatch stays at-least-once", async () => {
  // What is guaranteed, measured rather than assumed, and the two halves are not the same claim.
  //
  // ONE DECISION: the ledger decides once and replays that decision verbatim, so both workers apply
  // the same revision and phase and the standing state is written once. That is AE5's wording and
  // it is the property that matters -- a second DECISION could differ from the first, and a train
  // presented as both cancelled and merely late is the failure this channel must never produce.
  //
  // AT-LEAST-ONCE DISPATCH: the replay is what lets a retry after a transient dispatch failure
  // deliver at all, and the price of that is that two workers genuinely running at the same instant
  // can both dispatch. A lease would close the window and was deliberately not added: it trades a
  // duplicate for a LOST notification whenever a leaseholder dies, and this product exists to make
  // sure a traveller hears about a cancelled train. The partner's own per-train notification tag
  // absorbs the duplicate, which is what the published contract already tells them to expect.
  const { stores, adapters, dispatched } = harness();
  const event = makeEvent({ revision: 4 });
  const record = await accept(stores, event);
  await Promise.all([processRecord(adapters, record), processRecord(adapters, record)]);

  assert.equal(stores.state.decisions.size, 1, "one decision");
  assert.deepEqual(await stores.ledger.standing(event.alert_id), { revision: 4, phase: "predicted" });
  assert.deepEqual(
    new Set(dispatched.map((request) => `${request.event.event_id}|${request.display}`)).size,
    1,
    "every dispatch carried the same decision",
  );
  assert.ok(dispatched.length >= 1);

  // Once the record is settled, a further entry stops at the freshness read and dispatches nothing.
  const before = dispatched.length;
  const after = await processRecord(adapters, record);
  assert.equal(after.outcome, "already_settled");
  assert.equal(dispatched.length, before);
});

test("a transient dispatch failure is retried and eventually delivered, not swallowed", async () => {
  // The failure mode this guards: treating a decided-but-unfinished event as a duplicate. That
  // turns every FCM 503 into a notification the traveller never receives, silently, because a
  // suppression looks exactly like a correct suppression in a log.
  let attempts = 0;
  const { stores, adapters, dispatched } = harness({
    async dispatch(request) {
      attempts += 1;
      if (attempts === 1) {
        throw new Error("fcm unavailable");
      }
      dispatched.push(request);
    },
  });
  const record = await accept(stores, makeEvent({ revision: 4 }));
  await assert.rejects(() => processRecord(adapters, record), /fcm unavailable/);
  assert.equal(dispatched.length, 0);

  const retry = await processRecord(adapters, record);
  assert.equal(retry.outcome, "dispatched");
  assert.equal(dispatched.length, 1);
  assert.equal(attempts, 2);
});

test("a TERMINAL dispatch failure is recorded and not retried", async () => {
  const { stores, adapters } = harness({
    async dispatch() {
      throw new TerminalReceiverError("the device token is not registered");
    },
  });
  const record = await accept(stores, makeEvent());
  const outcome = await processRecord(adapters, record);
  assert.equal(outcome.outcome, "suppressed");
  assert.equal(outcome.rule, "dispatch_refused");
  assert.equal(stores.state.suppressions.at(-1).rule, "dispatch_refused");
});

test("an unclassified error THROWS -- the safe direction for a store fault", async () => {
  const { stores, adapters } = harness({
    ledger: {
      async decide() {
        throw new Error("firestore deadline exceeded");
      },
      async settle() {},
      async standing() {
        return null;
      },
    },
  });
  const record = await accept(stores, makeEvent());
  await assert.rejects(() => processRecord(adapters, record), /deadline exceeded/);
});

test("an event past valid_until expires at ENTRY: no policy, no dispatch, and no throw", async () => {
  const { stores, adapters, dispatched } = harness({
    now: () => new Date("2026-09-09T06:00:01Z"),
  });
  const event = makeEvent({ valid_until: "2026-09-09T06:00:00+00:00" });
  const outcome = await processRecord(adapters, await accept(stores, event));
  assert.equal(outcome.outcome, "expired");
  assert.match(outcome.detail, /past valid_until/);
  assert.equal(dispatched.length, 0);
  assert.equal(await stores.ledger.standing(event.alert_id), null, "no state was applied");
  assert.equal(stores.state.inbox.get(event.event_id).status, "expired");
});

test("the own staleness cutoff stops a permanently failing record retrying for a full day", async () => {
  // The scenario the cutoff exists for: a bug in `dispatch` that throws on every attempt. Without a
  // cutoff the trigger would re-run it for 24 hours. With one, the record is abandoned as soon as it
  // is older than the cutoff -- and it is abandoned WITHOUT a throw, which is what actually ends the
  // retries.
  let clock = NOW;
  const { stores, adapters, dispatched } = harness({
    now: () => clock,
    async dispatch() {
      throw new Error("a bug that never recovers");
    },
  });
  // A validity horizon well past the cutoff, so this test is about the cutoff and not about
  // `valid_until` -- otherwise it would pass with no cutoff at all.
  const event = makeEvent({ valid_until: "2026-09-09T23:00:00+00:00" });
  const record = await accept(stores, event);

  await assert.rejects(() => processRecord(adapters, record));
  clock = new Date(NOW.getTime() + DEFAULT_PROCESSING_CUTOFF_MS - 1000);
  await assert.rejects(() => processRecord(adapters, record), "still inside the cutoff");

  clock = new Date(NOW.getTime() + DEFAULT_PROCESSING_CUTOFF_MS + 1000);
  const outcome = await processRecord(adapters, record);
  assert.equal(outcome.outcome, "expired");
  assert.match(outcome.detail, /after acceptance/);
  assert.equal(dispatched.length, 0);
});

test("an ineligible subscription is suppressed BEFORE native handoff, after the state is applied", async () => {
  // The flowchart's own order. An ineligible event still carried the newest fact about that train,
  // so forgetting it would let a later, OLDER revision be applied on top of nothing.
  const { stores, adapters, dispatched, recipientCalls } = harness({
    async eligibility() {
      return { ok: false, reason: "subscription deleted" };
    },
  });
  const event = makeEvent({ revision: 6 });
  const outcome = await processRecord(adapters, await accept(stores, event));
  assert.equal(outcome.outcome, "suppressed");
  assert.equal(outcome.rule, "ineligible");
  assert.equal(dispatched.length, 0);
  assert.deepEqual(recipientCalls, [], "no recipient was looked up");
  assert.deepEqual(await stores.ledger.standing(event.alert_id), { revision: 6, phase: "predicted" });
});

test("a departed train is suppressed by its own valid_until, which is bounded by the departure", async () => {
  const { stores, adapters, dispatched } = harness({ now: () => new Date("2026-09-09T07:30:00Z") });
  const departed = makeEvent({ valid_until: "2026-09-09T07:18:00+00:00" });
  const outcome = await processRecord(adapters, await accept(stores, departed, new Date("2026-09-09T07:10:00Z")));
  assert.equal(outcome.outcome, "expired");
  assert.equal(dispatched.length, 0);
});

test("a test event never reaches the real recipient map", async () => {
  // R15: the marker rides INSIDE the signed body so the receiver can refuse it before native
  // dispatch, and it is immutable at the sender. The assertion is a call count, because "it went to
  // the test map" and "it never touched the live map" are two different claims and only the second
  // one is the requirement.
  const { stores, adapters, dispatched, recipientCalls } = harness();
  const outcome = await processRecord(adapters, await accept(stores, makeEvent({ test: true })));
  assert.equal(outcome.outcome, "dispatched");
  assert.equal(dispatched.length, 1);
  assert.deepEqual(
    recipientCalls.map(([which]) => which),
    ["testRecipients"],
  );
});

test("a test event is suppressed entirely where the adapter has not opted in", async () => {
  const { stores, adapters, dispatched, recipientCalls } = harness({ testRecipients: undefined });
  const outcome = await processRecord(adapters, await accept(stores, makeEvent({ test: true })));
  assert.equal(outcome.outcome, "suppressed");
  assert.equal(outcome.rule, "test_event_not_opted_in");
  assert.equal(dispatched.length, 0);
  assert.deepEqual(recipientCalls, []);
});

test("a live event never reaches the TEST recipient map either", async () => {
  const { stores, adapters, recipientCalls } = harness();
  await processRecord(adapters, await accept(stores, makeEvent({ test: false })));
  assert.deepEqual(
    recipientCalls.map(([which]) => which),
    ["recipients"],
  );
});

test("a subscription that maps to no recipient is recorded, not dispatched to nobody", async () => {
  const { stores, adapters, dispatched } = harness({
    async recipients() {
      return [];
    },
  });
  const outcome = await processRecord(adapters, await accept(stores, makeEvent()));
  assert.equal(outcome.outcome, "suppressed");
  assert.equal(outcome.rule, "no_recipient");
  assert.equal(dispatched.length, 0);
});

test("AE16 end to end: the confirmation is dispatched and the late forecast is not", async () => {
  const { stores, adapters, dispatched } = harness();
  const alertId = "sub_a:VYG:L1:2026-09-09:1";
  const confirmed = makeEvent({
    alert_id: alertId,
    revision: 12,
    phase: "confirmed",
    type: "journey.alert.updated",
  });
  const forecast = makeEvent({ alert_id: alertId, revision: 11, phase: "predicted" });

  await processRecord(adapters, await accept(stores, confirmed));
  const late = await processRecord(adapters, await accept(stores, forecast));

  assert.equal(dispatched.length, 1);
  assert.equal(dispatched[0].event.phase, "confirmed");
  assert.equal(dispatched[0].display, "silent", "a confirmation defaults to silent");
  assert.equal(late.outcome, "suppressed");
  assert.equal(late.rule, "stale_revision");
});

test("AE17 end to end: two subscriptions about one train are both dispatched", async () => {
  const { stores, adapters, dispatched } = harness();
  const morning = makeEvent({
    alert_id: "sub_morning:VYG:L1:2026-09-09:1",
    subscription_id: "sub_morning",
    revision: 40,
  });
  const evening = makeEvent({
    alert_id: "sub_evening:VYG:L1:2026-09-09:1",
    subscription_id: "sub_evening",
    revision: 3,
  });
  await processRecord(adapters, await accept(stores, morning));
  await processRecord(adapters, await accept(stores, evening));
  assert.equal(dispatched.length, 2);
  assert.deepEqual(
    dispatched.map((request) => request.event.subscription_id).sort(),
    ["sub_evening", "sub_morning"],
  );
});

test("every drop leaves a suppression record with its rule and a reason", async () => {
  const { stores, adapters } = harness({
    async eligibility() {
      return { ok: false, reason: "departure has passed" };
    },
  });
  await processRecord(adapters, await accept(stores, makeEvent()));
  const record = stores.state.suppressions.at(-1);
  assert.equal(record.rule, "ineligible");
  assert.equal(record.detail, "departure has passed");
  assert.ok(record.at);
  assert.ok(record.alertId);
});

test("each attempt is counted on the record, so a stuck event is visible to an operator", async () => {
  const { stores, adapters } = harness({
    async dispatch() {
      throw new Error("transient");
    },
  });
  const record = await accept(stores, makeEvent());
  await assert.rejects(() => processRecord(adapters, record));
  await assert.rejects(() => processRecord(adapters, record));
  assert.equal(stores.state.inbox.get(record.eventId).attempts, 2);
});

test("an EXPIRED test event reaches neither recipient map and does no native work", async () => {
  // Scenario 3's fourth case, and it is worth its own test because the two guards are independent:
  // the staleness cut runs at entry, before the policy and before the `test` marker is consulted at
  // all. So an expired test event must produce no lookup of ANY kind -- not the live map, and not
  // the isolated test map either.
  const { stores, adapters, dispatched, recipientCalls } = harness({
    now: () => new Date("2026-09-09T06:00:01Z"),
  });
  const event = makeEvent({ test: true, valid_until: "2026-09-09T06:00:00+00:00" });
  const outcome = await processRecord(adapters, await accept(stores, event));
  assert.equal(outcome.outcome, "expired");
  assert.deepEqual(recipientCalls, []);
  assert.equal(dispatched.length, 0);
  assert.equal(await stores.ledger.standing(event.alert_id), null, "no state was applied");
});

test("a re-entry on a DISPATCHED record does not rewrite its terminal status", async () => {
  // The trigger's snapshot is the document AS CREATED, so `record.status` is `pending` however many
  // times the trigger re-fires. Without the freshness read at entry, a redelivery after a crash
  // walks the whole function again and settles the inbox `suppressed` -- destroying the only record
  // that the notification was in fact sent. No duplicate notification results, which is exactly why
  // this is easy to miss.
  const { stores, adapters, dispatched } = harness();
  const record = await accept(stores, makeEvent({ revision: 4 }));
  await processRecord(adapters, record);
  assert.equal(stores.state.inbox.get(record.eventId).status, "dispatched");

  const again = await processRecord(adapters, record);
  assert.deepEqual(again, { outcome: "already_settled", status: "dispatched" });
  assert.equal(stores.state.inbox.get(record.eventId).status, "dispatched");
  assert.equal(dispatched.length, 1);
});

test("a re-entry PAST valid_until does not rewrite a dispatched record as expired", async () => {
  // The nastier half. The staleness cut runs at entry, ahead of the ledger, so without the freshness
  // read a late redelivery would settle a successfully dispatched event as `expired` -- which reads
  // to an operator as a notification that was never sent.
  let clock = NOW;
  const { stores, adapters, dispatched } = harness({ now: () => clock });
  const record = await accept(stores, makeEvent({ revision: 4 }));
  await processRecord(adapters, record);
  assert.equal(stores.state.inbox.get(record.eventId).status, "dispatched");

  clock = new Date("2026-09-09T09:00:00Z");
  const late = await processRecord(adapters, record);
  assert.deepEqual(late, { outcome: "already_settled", status: "dispatched" });
  assert.equal(stores.state.inbox.get(record.eventId).status, "dispatched");
  assert.equal(dispatched.length, 1);
});

test("a re-entry on a settled record spends no attempt and no policy read", async () => {
  let decisions = 0;
  const { stores, adapters } = harness();
  const record = await accept(stores, makeEvent({ revision: 4 }));
  await processRecord(adapters, record);
  const attemptsAfterFirst = stores.state.inbox.get(record.eventId).attempts;
  const ledger = adapters.ledger;
  adapters.ledger = {
    ...ledger,
    async decide(...args) {
      decisions += 1;
      return ledger.decide(...args);
    },
  };
  await processRecord(adapters, record);
  assert.equal(decisions, 0, "the ledger was consulted for a record that is already finished");
  assert.equal(stores.state.inbox.get(record.eventId).attempts, attemptsAfterFirst);
});
