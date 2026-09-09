// U8 scenario 1, end to end: a subscription produces a signed event and a native notification with
// no real disruption.
//
// "With no real disruption" is `POST /notification-api/v1/test-events` (R15): a synthetic event for
// a subscription created `test: true`, signed over exactly the bytes the route emits, dialled at the
// partner's callback. So the body below is a transcription of `disruptions/api/partner_api.py`'s
// `_synthetic_event` rather than an invented fixture -- a fixture that drifted from that function
// would prove the package works on an event nobody sends.
//
// WHY THIS FILE IS IN `npm test` AND NOT IN `test/emulator/`, decided rather than defaulted:
//
//   * What the scenario proves is the PROTOCOL -- signature over raw bytes, idempotent inbox, the
//     three suppression rules, the test-recipient gate, one dispatch. None of that is Firestore.
//     `test/emulator/` exists for the Firestore-backed adapters' transaction semantics, which is a
//     different question, and one the plan itself says an emulator cannot settle ("a simple locking
//     approach ... does not mirror production concurrency").
//   * This is the first thing a partner runs. Behind `test:emulator` it would need Java 21+ and the
//     Firebase CLI to answer "does the quickstart work at all?", which is exactly the barrier the
//     vendored package exists to remove.
//   * The "declared partner runtime" half (KTD11: compiled CommonJS, `.d.ts`, Node >= 24) is already
//     half-covered right here and fully covered next door. `test/helpers/adapters.mjs` imports
//     `@entur/kix-notifications-client` through the npm `file:` link, whose `main` is
//     `./dist/index.js` and which `pretest` builds -- so every client-side symbol this file touches
//     (`signatureHeader`, `createMemoryStores`, `receive`, `process`) runs here as COMPILED
//     CommonJS on Node 24, not as a `.ts` source. This package's own modules load as `.ts`; their
//     compiled form is `npm run verify:dist` (`test/dist/packagedArtifact.test.mjs`).

import { test } from "node:test";
import assert from "node:assert/strict";

import { signatureHeader } from "@entur/kix-notifications-client";

import { createInboxWorker, createNotificationReceiver } from "../src/receiver.ts";
import { NOW, TEST_SECRET, encode, fakeResponse, memoryAdapters } from "./helpers/adapters.mjs";

const FROM_STOP = "NSR:StopPlace:337";
const TO_STOP = "NSR:StopPlace:59872";

/**
 * The body `POST /test-events` emits, field for field.
 *
 * `event_id`, `alert_id` and `journey_ref` are random on that route (`test-<nonce>` /
 * `KIX:Test:<nonce>`) precisely so two test events cannot be read as two revisions of one train; the
 * nonce is a parameter here for the same reason.
 */
function syntheticEvent(nonce = "0123456789ab") {
  return {
    schema_version: "1",
    event_id: `test-${nonce}`,
    type: "journey.alert.created",
    phase: "predicted",
    alert_id: `test-${nonce}`,
    revision: 21_000_000,
    occurred_at: NOW.toISOString(),
    valid_until: new Date(NOW.getTime() + 60 * 60 * 1000).toISOString(),
    subscription_id: "morgentur",
    subscription_version: 1,
    partner_ref: "commuter-4711",
    test: true,
    operator: "VYG",
    line: "VYG:Line:L1",
    journey_ref: `KIX:Test:${nonce}`,
    call_date: "2026-09-09",
    stop_place: FROM_STOP,
    stop_order: 1,
    situations: [],
    payload: {
      notification: {
        title: "Testvarsel: Tog L1 07:18 er innstilt",
        body:
          "Dette er et testvarsel fra Entur. " +
          `${FROM_STOP} → ${TO_STOP}. Ingen avganger er berørt.`,
        action: null,
      },
      call: {
        operator: "VYG",
        line: "VYG:Line:L1",
        stop_place: FROM_STOP,
        aimed_departure: NOW.toISOString(),
      },
      probabilities: { impact: 1.0, cancel: 1.0 },
      delay: null,
      effect: null,
      situations: [],
      alternative_journey: null,
    },
  };
}

/**
 * The request the sender puts on the wire.
 *
 * The bytes are built ONCE and signed unmodified, which is the sender's own rule (KTD7): a signature
 * over re-serialised JSON is a signature over a different document the moment anything reorders a
 * key. `webhook-id` is the delivery id and is stable across attempts; the timestamp and signature
 * are minted per attempt.
 */
function signedRequest(event, { now = NOW, secrets = [TEST_SECRET] } = {}) {
  const body = encode(event);
  const timestampSeconds = Math.floor(now.getTime() / 1000);
  return {
    body,
    request: {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "content-length": String(body.byteLength),
        "webhook-id": event.event_id,
        "webhook-timestamp": String(timestampSeconds),
        "webhook-signature": signatureHeader({
          secrets,
          webhookId: event.event_id,
          timestampSeconds,
          body,
        }),
      },
      rawBody: body,
      body: JSON.parse(body.toString("utf8")),
    },
  };
}

/** The four things an integrator supplies. Everything else in the chain is the package's. */
function integratorAdapters({ optIn, dispatched, seen }) {
  return {
    async recipients(event) {
      seen.push(["recipients", event.event_id]);
      throw new Error("the LIVE recipient map must never be reached for a test event (R15)");
    },
    ...(optIn
      ? {
          async testRecipients(event) {
            seen.push(["testRecipients", event.event_id]);
            return ["test-phone-1"];
          },
        }
      : {}),
    async eligibility() {
      seen.push(["eligibility"]);
      return { ok: true };
    },
    async dispatch(request) {
      dispatched.push(request);
    },
  };
}

function harness({ optIn = true } = {}) {
  const dispatched = [];
  const seen = [];
  const base = memoryAdapters(integratorAdapters({ optIn, dispatched, seen }));
  // `memoryAdapters` seeds a `dispatch` of its own; the override above is the one in effect, so the
  // recorded array is this one.
  return { ...base, dispatched, seen };
}

function receiverFor(adapters) {
  return createNotificationReceiver({
    secrets: () => [TEST_SECRET],
    adapters,
    now: () => NOW,
  });
}

test("scenario 1: a signed synthetic event becomes one native notification", async () => {
  const { adapters, stores, dispatched, seen } = harness();
  const event = syntheticEvent();
  const { request } = signedRequest(event);

  // 1. The HTTP half. Persist, answer, stop -- no dispatch happens on the request path, because a
  //    Cloud Function that has returned its response cannot be relied on to keep working.
  const response = fakeResponse();
  await receiverFor(adapters)(request, response);
  assert.equal(response.sent.status, 200);
  assert.equal(response.sent.body.outcome, "accepted");
  assert.equal(dispatched.length, 0, "the request handler must dispatch nothing itself");

  const record = await stores.inbox.get(event.event_id);
  assert.equal(record.status, "pending");
  assert.equal(record.event.test, true);

  // 2. The trigger half, which is where the notification decision is made.
  const worker = createInboxWorker(adapters);
  await worker.run({ data: { data: () => record }, params: { eventId: record.eventId } });

  assert.equal(dispatched.length, 1, "exactly one native notification");
  const [call] = dispatched;
  assert.deepEqual(call.recipients, ["test-phone-1"]);
  // The test recipient map was consulted and the live one was not. R15's marker rides inside the
  // signed body precisely so this decision can be made before native dispatch.
  assert.deepEqual(
    seen.filter(([which]) => which.endsWith("ecipients")),
    [["testRecipients", event.event_id]],
  );

  // 3. The notification is ready to display: the Norwegian title and body are the sender's, byte for
  //    byte, and no second request to us was needed to render them (R7).
  assert.equal(call.display, "alert", "a predicted disruption is presented as an alert");
  assert.equal(call.event.payload.notification.title, "Testvarsel: Tog L1 07:18 er innstilt");
  assert.equal(
    call.event.payload.notification.body,
    `Dette er et testvarsel fra Entur. ${FROM_STOP} → ${TO_STOP}. Ingen avganger er berørt.`,
  );
  assert.equal(call.event.partner_ref, "commuter-4711", "the partner's own handle rides along");
  assert.equal(await stores.inbox.get(event.event_id).then((r) => r.status), "dispatched");
});

test("the integrator implements no signature, idempotency, ordering or suppression code", async () => {
  // The Success Criteria as an assertion rather than a claim: "sees a synthetic notification ...
  // without writing signature verification, idempotency, revision ordering or duplicate
  // suppression". What an integrator supplies is exactly the recipient lookup and the presentation.
  const { adapters } = harness();
  const supplied = new Set(Object.keys(integratorAdapters({ optIn: true, dispatched: [], seen: [] })));
  assert.deepEqual(
    [...supplied].sort(),
    ["dispatch", "eligibility", "recipients", "testRecipients"],
    "anything else in this list would be protocol work the package failed to absorb",
  );
  for (const provided of ["inbox", "ledger", "suppressions"]) {
    assert.ok(
      !supplied.has(provided),
      `${provided} comes from the package (createMemoryStores / createFirestore*), not the partner`,
    );
    assert.ok(adapters[provided], `${provided} must still be wired`);
  }
});

test("the test marker is inside the signed bytes, so flipping it invalidates the signature", async () => {
  // Why the marker is in the body and not in a header: a header can be stripped or forged by
  // anything on the path, and a receiver that trusted one could route a synthetic event to a real
  // commuter. Under the signature, the only way to present this event as live is to break it.
  const { adapters, stores } = harness();
  const event = syntheticEvent("aaaabbbbcccc");
  const { request } = signedRequest(event);
  const tampered = { ...event, test: false };
  const response = fakeResponse();
  await receiverFor(adapters)(
    { ...request, rawBody: encode(tampered), body: tampered },
    response,
  );
  assert.equal(response.sent.status, 401);
  assert.equal(await stores.inbox.get(event.event_id), null, "nothing is stored for a bad signature");
});

test("without an opt-in test recipient map, a synthetic event reaches nobody", async () => {
  // R15's default. Opting in is a deliberate act by the integrator; absent it, the event is
  // suppressed rather than routed anywhere -- and the LIVE map is not consulted on the way, which is
  // what the throwing `recipients` above is there to catch.
  const { adapters, stores, dispatched, seen } = harness({ optIn: false });
  const event = syntheticEvent("ddddeeeeffff");
  const { request } = signedRequest(event);
  await receiverFor(adapters)(request, fakeResponse());
  const record = await stores.inbox.get(event.event_id);
  await createInboxWorker(adapters).run({
    data: { data: () => record },
    params: { eventId: record.eventId },
  });
  assert.equal(dispatched.length, 0);
  assert.deepEqual(seen.filter(([which]) => which.endsWith("ecipients")), []);
  const settled = await stores.inbox.get(event.event_id);
  assert.equal(settled.status, "suppressed");
  assert.equal(
    stores.state.suppressions.at(-1).rule,
    "test_event_not_opted_in",
    "a suppression is recorded, not silently dropped",
  );
});

test("the same synthetic event delivered again produces no second notification", async () => {
  // At-least-once dispatch is the sender's published contract, so a partner WILL see a repeat: a
  // retry crossing a cycle boundary, a crash between our attempt and its `sent` write, the trigger
  // re-running. One decision, replayed.
  const { adapters, stores, dispatched } = harness();
  const event = syntheticEvent("111122223333");
  const { request } = signedRequest(event);
  const first = fakeResponse();
  await receiverFor(adapters)(request, first);
  const record = await stores.inbox.get(event.event_id);
  const worker = createInboxWorker(adapters);
  await worker.run({ data: { data: () => record }, params: { eventId: record.eventId } });
  assert.equal(dispatched.length, 1);

  // The sender re-attempts under the SAME webhook-id with a fresh timestamp and signature -- which
  // is what makes a retry survive the receiver's five-minute replay window.
  const later = new Date(NOW.getTime() + 4 * 60 * 1000);
  const retry = signedRequest(event, { now: later });
  assert.notEqual(
    retry.request.headers["webhook-signature"],
    request.headers["webhook-signature"],
    "a retry is re-signed",
  );
  assert.equal(retry.request.headers["webhook-id"], request.headers["webhook-id"]);
  const second = fakeResponse();
  await createNotificationReceiver({
    secrets: () => [TEST_SECRET],
    adapters,
    now: () => later,
  })(retry.request, second);
  assert.equal(second.sent.status, 200);
  assert.equal(second.sent.body.outcome, "duplicate");

  // And the trigger re-running over the settled record decides nothing new.
  await worker.run({ data: { data: () => record }, params: { eventId: record.eventId } });
  assert.equal(dispatched.length, 1, "one native decision, however many times the event arrives");
});
