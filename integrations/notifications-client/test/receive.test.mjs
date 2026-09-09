// The request half of the receiver: the published decision order, and the ordering claims that only
// a counted spy can prove.
//
// Scenario 2 of the unit's test plan is entirely about ORDER rather than about outcomes -- the
// inbox is persisted before the 2xx, an oversized request reaches neither the verifier nor the
// store -- so most of these assertions are call counts on instrumented collaborators, and a test
// that only checked the status code would pass against a receiver that verified an 8 MB body first.

import { test } from "node:test";
import assert from "node:assert/strict";

import { DEFAULT_MAX_BODY_BYTES, receive } from "../src/client.ts";
import { createMemoryStores } from "../src/ledger.ts";
import { OTHER_DESTINATION_SECRET, TEST_SECRET, TEST_SECRET_NEXT, encode, makeEvent } from "./helpers/events.mjs";
import { NOW, signedRequest } from "./helpers/signed.mjs";

function countingInbox(base, calls) {
  return new Proxy(base, {
    get(target, property) {
      const value = Reflect.get(target, property);
      if (typeof value !== "function") {
        return value;
      }
      return (...args) => {
        calls.push(property);
        return value.apply(target, args);
      };
    },
  });
}

test("a well-formed signed request is accepted and persisted", async () => {
  const stores = createMemoryStores();
  const request = signedRequest();
  const result = await receive(
    { secrets: [TEST_SECRET], inbox: stores.inbox, now: () => NOW },
    request,
  );
  assert.equal(result.status, 200);
  assert.equal(result.outcome, "accepted");

  const stored = stores.state.inbox.get(request.event.event_id);
  assert.equal(stored.status, "pending");
  assert.equal(stored.webhookId, "dlv_test_0001");
  assert.equal(stored.receivedAt, NOW.toISOString());
  // The exact bytes, not a re-serialization: this is what a later "did the signature really cover
  // this?" question is answered from.
  assert.deepEqual(Buffer.from(stored.bodyBase64, "base64"), request.body);
});

test("an oversized request is 413 and reaches NEITHER the verifier NOR the store", async () => {
  // The proof is in what the request LACKS. It carries no signature headers and a body that is not
  // JSON, so a receiver that verified before checking the size would answer 401 and one that parsed
  // first would answer 400. Only a receiver that checks the size first can answer 413 here -- and
  // the spy on the inbox closes the third door. This is stronger than a mock of the verifier: it
  // exercises the production path with nothing stubbed.
  const stores = createMemoryStores();
  const inboxCalls = [];
  const result = await receive(
    {
      secrets: [TEST_SECRET],
      inbox: countingInbox(stores.inbox, inboxCalls),
      maxBodyBytes: 100,
      now: () => NOW,
    },
    { headers: {}, body: Buffer.alloc(200, 0x41), declaredBytes: null },
  );
  assert.equal(result.status, 413);
  assert.equal(result.outcome, "too_large");
  assert.deepEqual(inboxCalls, [], "no store call was made");
  assert.equal(stores.state.inbox.size, 0);

  // And with a declared length over the ceiling the body need not exist at all: a transport that can
  // refuse before buffering passes `body: null`, and there is then nothing to verify or to store.
  const declaredOnly = await receive(
    {
      secrets: [TEST_SECRET],
      inbox: countingInbox(stores.inbox, inboxCalls),
      maxBodyBytes: 100,
      now: () => NOW,
    },
    { headers: {}, body: null, declaredBytes: 100_000 },
  );
  assert.equal(declaredOnly.status, 413);
  assert.deepEqual(inboxCalls, []);
});

test("a body that exceeds the ceiling is 413 even when Content-Length under-declares it", async () => {
  // Content-Length is attacker-controlled. Trusting it alone lets an over-limit body buy its way
  // past the ceiling for the price of one header.
  const stores = createMemoryStores();
  const request = signedRequest();
  const result = await receive(
    { secrets: [TEST_SECRET], inbox: stores.inbox, maxBodyBytes: 10, now: () => NOW },
    { ...request, declaredBytes: 1 },
  );
  assert.equal(result.status, 413);
  assert.equal(stores.state.inbox.size, 0);
});

test("the default ceiling is two orders of magnitude above a real event", async () => {
  const bytes = encode(makeEvent());
  assert.ok(bytes.byteLength < 2000, `a fixture event is ${bytes.byteLength} bytes`);
  assert.equal(DEFAULT_MAX_BODY_BYTES, 64 * 1024);
});

test("an invalid signature is 401 and writes nothing", async () => {
  const stores = createMemoryStores();
  const inboxCalls = [];
  const request = signedRequest();
  const tampered = { ...request, body: encode({ ...request.event, revision: 99 }) };
  const result = await receive(
    { secrets: [TEST_SECRET], inbox: countingInbox(stores.inbox, inboxCalls), now: () => NOW },
    tampered,
  );
  assert.equal(result.status, 401);
  assert.equal(result.detail, "no_matching_signature");
  assert.deepEqual(inboxCalls, []);
});

test("a stale timestamp is 401 for the WINDOW, distinguishably from a wrong key", async () => {
  const stores = createMemoryStores();
  const request = signedRequest();
  const late = new Date(NOW.getTime() + 301_000);
  const result = await receive(
    { secrets: [TEST_SECRET], inbox: stores.inbox, now: () => late },
    request,
  );
  assert.equal(result.status, 401);
  assert.equal(result.detail, "timestamp_too_old");
  assert.equal(stores.state.inbox.size, 0);
});

test("an unknown client -- no secret for this destination -- is 401 and writes nothing", async () => {
  const stores = createMemoryStores();
  const request = signedRequest();
  const unknown = await receive(
    { secrets: [], inbox: stores.inbox, now: () => NOW },
    request,
  );
  assert.equal(unknown.status, 401);
  assert.equal(unknown.detail, "no_secrets");

  // And another destination's secret does not verify it either: per-destination derivation means
  // one client's leaked receiver secret forges nothing for another.
  const wrongDestination = await receive(
    { secrets: [OTHER_DESTINATION_SECRET], inbox: stores.inbox, now: () => NOW },
    request,
  );
  assert.equal(wrongDestination.status, 401);
  assert.equal(wrongDestination.detail, "no_matching_signature");
  assert.equal(stores.state.inbox.size, 0);
});

test("a rotation in progress: either live generation is accepted", async () => {
  const stores = createMemoryStores();
  for (const signing of [TEST_SECRET, TEST_SECRET_NEXT]) {
    const request = signedRequest({ event: makeEvent({ revision: signing === TEST_SECRET ? 1 : 2 }), secrets: [signing] });
    const result = await receive(
      { secrets: [TEST_SECRET_NEXT, TEST_SECRET], inbox: stores.inbox, now: () => NOW },
      request,
    );
    assert.equal(result.status, 200, signing);
  }
});

test("an envelope this build does not know is 400 and writes nothing", async () => {
  const stores = createMemoryStores();
  const inboxCalls = [];
  const future = { ...makeEvent(), schema_version: "2" };
  const request = signedRequest({ event: future });
  const result = await receive(
    { secrets: [TEST_SECRET], inbox: countingInbox(stores.inbox, inboxCalls), now: () => NOW },
    request,
  );
  assert.equal(result.status, 400);
  assert.match(result.detail, /unknown_schema_version/);
  assert.deepEqual(inboxCalls, []);
});

test("a body carrying a field the schema does not declare is 400", async () => {
  const stores = createMemoryStores();
  const request = signedRequest({ event: { ...makeEvent(), destination_id: "dst_a" } });
  const result = await receive(
    { secrets: [TEST_SECRET], inbox: stores.inbox, now: () => NOW },
    request,
  );
  assert.equal(result.status, 400);
  assert.match(result.detail, /unknown_field: destination_id/);
});

test("the inbox write happens BEFORE the response, not after it", async () => {
  // The ordering scenario 2 names, observed rather than assumed: the promise the store returns is
  // held open, and `receive` must not have settled while it is pending. A receiver that
  // acknowledged first and stored afterwards would resolve immediately here -- and would
  // acknowledge an event that then existed nowhere.
  const stores = createMemoryStores();
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const slowInbox = {
    ...stores.inbox,
    async put(record) {
      await gate;
      return stores.inbox.put(record);
    },
  };
  let settled = false;
  const pending = receive(
    { secrets: [TEST_SECRET], inbox: slowInbox, now: () => NOW },
    signedRequest(),
  ).then((result) => {
    settled = true;
    return result;
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(settled, false, "the response was produced before the store answered");
  release();
  const result = await pending;
  assert.equal(result.status, 200);
  assert.equal(stores.state.inbox.size, 1);
});

test("a store failure is 503 -- retryable -- and never a 2xx", async () => {
  // The sender retries 5xx and treats every other 4xx as terminal for the event. So the one answer
  // that keeps a store outage recoverable is 503, and the one answer that loses the event silently
  // is a 2xx.
  const stores = createMemoryStores();
  const result = await receive(
    {
      secrets: [TEST_SECRET],
      inbox: {
        ...stores.inbox,
        async put() {
          throw new Error("firestore unavailable");
        },
      },
      now: () => NOW,
    },
    signedRequest(),
  );
  assert.equal(result.status, 503);
  assert.equal(result.outcome, "store_unavailable");
  assert.match(result.detail, /firestore unavailable/);
});

test("AE5, first half: a redelivery of the same event creates ONE inbox record", async () => {
  const stores = createMemoryStores();
  const request = signedRequest();
  const first = await receive(
    { secrets: [TEST_SECRET], inbox: stores.inbox, now: () => NOW },
    request,
  );
  // A retry re-signs under a fresh timestamp and reuses the delivery id, exactly as the sender does.
  const retry = signedRequest({
    event: request.event,
    now: new Date(NOW.getTime() + 200_000),
    webhookId: "dlv_test_0001",
  });
  const second = await receive(
    { secrets: [TEST_SECRET], inbox: stores.inbox, now: () => new Date(NOW.getTime() + 200_000) },
    retry,
  );
  assert.equal(first.outcome, "accepted");
  assert.equal(second.outcome, "duplicate");
  assert.equal(second.status, 200, "a duplicate is acknowledged, not refused");
  assert.equal(stores.state.inbox.size, 1);
});

test("two concurrent deliveries of one event create ONE inbox record", async () => {
  const stores = createMemoryStores();
  const request = signedRequest();
  const config = { secrets: [TEST_SECRET], inbox: stores.inbox, now: () => NOW };
  const [left, right] = await Promise.all([receive(config, request), receive(config, request)]);
  const outcomes = [left.outcome, right.outcome].sort();
  assert.deepEqual(outcomes, ["accepted", "duplicate"]);
  assert.equal(stores.state.inbox.size, 1);
});

test("a repeated signature header is refused rather than guessed at", async () => {
  const stores = createMemoryStores();
  const request = signedRequest();
  const result = await receive(
    { secrets: [TEST_SECRET], inbox: stores.inbox, now: () => NOW },
    {
      ...request,
      headers: {
        ...request.headers,
        "webhook-signature": [request.headers["webhook-signature"], "v1,AAAA"],
      },
    },
  );
  assert.equal(result.status, 401);
  assert.equal(result.detail, "missing_headers");
});

test("a malformed timestamp is refused as malformed, not parsed leniently", async () => {
  const stores = createMemoryStores();
  const request = signedRequest();
  for (const value of ["1789197300abc", "", " 1789197300", "1e9"]) {
    const result = await receive(
      { secrets: [TEST_SECRET], inbox: stores.inbox, now: () => NOW },
      { ...request, headers: { ...request.headers, "webhook-timestamp": value } },
    );
    assert.equal(result.status, 401, value);
    assert.ok(
      ["malformed_timestamp", "missing_headers"].includes(result.detail),
      `${value} -> ${result.detail}`,
    );
  }
});
