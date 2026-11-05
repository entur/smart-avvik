// U8 scenario 1, end to end: a subscription produces a signed event and a native notification with
// no real disruption.
//
// "With no real disruption" is `POST /notification-api/v1/test-events` (R15): a synthetic event for
// a subscription created `test: true`, signed over exactly the bytes the route emits, dialled at the
// client's callback. So the body below is `disruptions/api/notification_api.py`'s `_synthetic_event`
// itself, READ from the sample that function was dumped into (`contracts/test-event-sample.json`)
// rather than transcribed -- a fixture that drifted from that function would prove the package
// works on an event nobody sends, which is exactly what this one did until 2026-09-09. See the
// block above `SAMPLE` for the eight fields it had drifted in and why nothing caught them.
//
// WHY THIS FILE IS IN `npm test` AND NOT IN `test/emulator/`, decided rather than defaulted:
//
//   * What the scenario proves is the PROTOCOL -- signature over raw bytes, idempotent inbox, the
//     three suppression rules, the test-recipient gate, one dispatch. None of that is Firestore.
//     `test/emulator/` exists for the Firestore-backed adapters' transaction semantics, which is a
//     different question, and one the plan itself says an emulator cannot settle ("a simple locking
//     approach ... does not mirror production concurrency").
//   * This is the first thing a client runs. Behind `test:emulator` it would need Java 21+ and the
//     Firebase CLI to answer "does the quickstart work at all?", which is exactly the barrier the
//     vendored package exists to remove.
//   * The "declared client runtime" half (KTD11: compiled CommonJS, `.d.ts`, Node >= 24) is already
//     half-covered right here and fully covered next door. `test/helpers/adapters.mjs` imports
//     `@entur/smart-avvik-notifications-client` through the npm `file:` link, whose `main` is
//     `./dist/index.js` and which `pretest` builds -- so every client-side symbol this file touches
//     (`signatureHeader`, `createMemoryStores`, `receive`, `process`) runs here as COMPILED
//     CommonJS on Node 24, not as a `.ts` source. This package's own modules load as `.ts`; their
//     compiled form is `npm run verify:dist` (`test/dist/packagedArtifact.test.mjs`).

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

import { signatureHeader } from "@entur/smart-avvik-notifications-client";

import { createInboxWorker, createNotificationReceiver } from "../src/receiver.ts";
import { NOW, TEST_SECRET, encode, fakeResponse, memoryAdapters } from "./helpers/adapters.mjs";

/**
 * The sample event, dumped from the real producer and shipped in the client package's `contracts/`.
 *
 * **It is read rather than transcribed, and that is the whole point of this block.** The fixture
 * below WAS a hand-written transcription of `disruptions/api/notification_api.py::_synthetic_event`,
 * announced as one "field for field" in its own docstring -- and by 2026-09-09 it had drifted from
 * that function in eight places: `call.aimed_departure` for `aimed_departure_time`, invented
 * `call.operator`/`call.line`, missing `call.stop_order`/`departs_tomorrow`,
 * `probabilities.cancel` for `cancellation`, missing `delay_ge10`/`alert_threshold`, `phase:
 * "predicted"` where the producer sends `confirmed`, `effect: null` where it sends `"cancelled"`,
 * two empty `situations` lists that are never empty, a `valid_until` an hour out, and a title and
 * body neither producer writes. Nothing failed: `payload.call` and `payload.probabilities` are
 * deliberately OPEN objects in `event.schema.json`, so a wrongly named key inside them is
 * schema-VALID -- which makes this file, read by a client as the worked example of a real payload,
 * the last place a transcription belongs.
 *
 * Resolved through `@entur/smart-avvik-notifications-client` rather than through the repository, because
 * that dependency is present in a vendored copy of this package and the repository is not.
 */
const SAMPLE = JSON.parse(
  fs.readFileSync(
    path.join(
      path.dirname(
        createRequire(import.meta.url).resolve("@entur/smart-avvik-notifications-client/package.json"),
      ),
      "contracts",
      "test-event-sample.json",
    ),
    "utf8",
  ),
);

const FROM_STOP = SAMPLE.event.stop_place;
const TO_STOP = "NSR:StopPlace:59872";
const SAMPLE_NOTIFICATION = SAMPLE.event.payload.notification;

/**
 * The body `POST /test-events` emits, re-stamped onto this suite's clock.
 *
 * Two substitutions, and only two. `event_id`, `alert_id` and `journey_ref` are minted per call on
 * that route (`test-<nonce>` / `KIX:Test:<nonce>`) precisely so two test events cannot be read as
 * two revisions of one train, so the nonce is a parameter here for the same reason. And the sample's
 * instants are moved onto `NOW` **preserving the producer's own offsets** -- `valid_until` is the
 * earlier of `occurred_at + 60 min` and the departure, which for this event is the departure -- so a
 * change to that rule travels here rather than being restated as a literal.
 */
function syntheticEvent(nonce = "0123456789ab") {
  const occurred = Date.parse(SAMPLE.event.occurred_at);
  const restamp = (iso) => new Date(NOW.getTime() + (Date.parse(iso) - occurred)).toISOString();
  const validUntil = restamp(SAMPLE.event.valid_until);
  return {
    ...SAMPLE.event,
    event_id: `test-${nonce}`,
    alert_id: `test-${nonce}`,
    journey_ref: `KIX:Test:${nonce}`,
    occurred_at: NOW.toISOString(),
    valid_until: validUntil,
    call_date: validUntil.slice(0, 10),
    situations: [`KIX:TestEvent:${nonce}`],
    payload: {
      ...SAMPLE.event.payload,
      call: {
        ...SAMPLE.event.payload.call,
        aimed_departure_time: restamp(SAMPLE.event.payload.call.aimed_departure_time),
      },
      situations: SAMPLE.event.payload.situations.map((situation) => ({
        ...situation,
        situation_number: `KIX:TestEvent:${nonce}`,
      })),
    },
  };
}

test("the fixture is the producer's event, not a second description of it", () => {
  // The transcription that used to live here drifted silently for want of exactly this. Key sets
  // rather than values, because the two substitutions above are deliberate and the keys are what a
  // client types their receiver against.
  const built = syntheticEvent();
  assert.deepEqual(Object.keys(built), Object.keys(SAMPLE.event));
  assert.deepEqual(Object.keys(built.payload), Object.keys(SAMPLE.event.payload));
  for (const part of ["notification", "call", "probabilities"]) {
    assert.deepEqual(
      Object.keys(built.payload[part]),
      Object.keys(SAMPLE.event.payload[part]),
      part,
    );
  }
  // `situations` is a list the loop above cannot reach: one element per contributing situation,
  // each with the sample's own key set, and the nonce substitution touching the number alone.
  assert.equal(built.payload.situations.length, SAMPLE.event.payload.situations.length);
  assert.ok(built.payload.situations.length > 0, "a test event names at least one situation");
  built.payload.situations.forEach((situation, index) => {
    const recorded = SAMPLE.event.payload.situations[index];
    assert.deepEqual(Object.keys(situation), Object.keys(recorded), `situations[${index}]`);
    for (const key of Object.keys(recorded)) {
      if (key !== "situation_number") assert.deepEqual(situation[key], recorded[key], key);
    }
  });
  assert.deepEqual(built.situations, built.payload.situations.map((s) => s.situation_number));
  // The three fields the producer states as a coherent confirmed cancellation, asserted here so a
  // regenerated sample that lost the coherence fails in the package a client reads first.
  assert.equal(built.phase, "confirmed");
  assert.equal(built.payload.effect, "cancelled");
  assert.equal(built.payload.probabilities.delay_ge10, null);
  assert.equal(built.payload.delay, null);
  // And the substitutions really are only the ones documented.
  assert.equal(Date.parse(built.valid_until) - NOW.getTime(), 45 * 60 * 1000);
  assert.equal(built.payload.call.aimed_departure_time, built.valid_until);
});

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
  //
  //    `silent`, not `alert`, and the reason is worth knowing before it looks like a bug. The route
  //    emits a coherent CONFIRMED cancellation -- both probabilities 1.0, every delay figure null --
  //    and `defaultDisplay` maps `predicted` to `alert` and every terminal phase to `silent`: a
  //    confirmation updates a warning the traveller already holds rather than raising a new one. The
  //    fixture claimed `predicted` with 1.0/1.0 and a null delay until 2026-09-09, which is a
  //    combination no producer emits, and this assertion read `alert` on the strength of it.
  assert.equal(call.display, "silent", "a confirmed cancellation updates rather than alerts");
  assert.equal(call.event.payload.notification.title, SAMPLE_NOTIFICATION.title);
  assert.equal(call.event.payload.notification.body, SAMPLE_NOTIFICATION.body);
  // The body names both ends of the trip, which is what makes it renderable with no second call.
  assert.ok(call.event.payload.notification.body.includes(FROM_STOP));
  assert.ok(call.event.payload.notification.body.includes(TO_STOP));
  assert.equal(call.event.external_ref, "commuter-4711", "the client's own handle rides along");
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
      `${provided} comes from the package (createMemoryStores / createFirestore*), not the client`,
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
  // At-least-once dispatch is the sender's published contract, so a client WILL see a repeat: a
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
