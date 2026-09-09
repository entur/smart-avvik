// The Cloud Functions HTTP handler.
//
// The three claims worth testing here are all about what the handler does NOT do: it does not read
// the parsed body, it does not verify or store an oversized request, and it does not leave work
// running after the response.

import { test } from "node:test";
import assert from "node:assert/strict";

import { createNotificationReceiver } from "../src/receiver.ts";
import {
  NOW,
  TEST_SECRET,
  encode,
  fakeResponse,
  makeEvent,
  memoryAdapters,
  signedFunctionRequest,
} from "./helpers/adapters.mjs";

function receiver(harness, options = {}) {
  return createNotificationReceiver({
    secrets: () => [TEST_SECRET],
    adapters: harness.adapters,
    now: () => NOW,
    ...options,
  });
}

test("a signed request is persisted and acknowledged", async () => {
  const harness = memoryAdapters();
  const { request, event } = signedFunctionRequest();
  const response = fakeResponse();
  await receiver(harness)(request, response);
  assert.equal(response.sent.status, 200);
  assert.equal(response.sent.body.outcome, "accepted");
  assert.equal(harness.stores.state.inbox.get(event.event_id).status, "pending");
});

test("the handler dispatches NOTHING itself -- the trigger does that", async () => {
  // The Firebase lifetime rule, asserted rather than trusted. A function that has returned cannot
  // access the CPU; work left running after the response may not run at all, or may resume inside a
  // later request. So the handler's own contract is: persist, answer, stop.
  const harness = memoryAdapters();
  const { request } = signedFunctionRequest();
  await receiver(harness)(request, fakeResponse());
  // Give any stray floating promise a turn. There must not be one.
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(harness.dispatched.length, 0);
});

test("an oversized Content-Length is 413 before the body, the verifier or the store", async () => {
  const harness = memoryAdapters();
  const calls = [];
  const spied = new Proxy(harness.adapters.inbox, {
    get(target, property) {
      const value = Reflect.get(target, property);
      if (typeof value !== "function") return value;
      return (...args) => {
        calls.push(property);
        return value.apply(target, args);
      };
    },
  });
  const handler = createNotificationReceiver({
    secrets: () => {
      throw new Error("the secret resolver must not be reached for an oversized request");
    },
    adapters: { ...harness.adapters, inbox: spied },
    now: () => NOW,
    maxBodyBytes: 100,
  });
  const response = fakeResponse();
  await handler(
    { method: "POST", headers: { "content-length": "100000" }, rawBody: Buffer.alloc(4) },
    response,
  );
  assert.equal(response.sent.status, 413);
  assert.deepEqual(calls, []);
});

test("a raw body over the ceiling is 413 even when Content-Length under-declares it", async () => {
  const harness = memoryAdapters();
  const response = fakeResponse();
  await receiver(harness, { maxBodyBytes: 10 })(
    { method: "POST", headers: { "content-length": "4" }, rawBody: Buffer.alloc(200) },
    response,
  );
  assert.equal(response.sent.status, 413);
  assert.equal(harness.stores.state.inbox.size, 0);
});

test("a request with no rawBody is REFUSED, never re-serialized from the parsed body", async () => {
  // The canonical webhook bug: `JSON.stringify(request.body)` is a different byte string from what
  // was signed -- key order, spacing, escaping -- so a handler that fell back to it would either
  // reject every event or, worse, verify a document nobody sent. Refusing is the only safe answer.
  const harness = memoryAdapters();
  const { request } = signedFunctionRequest();
  const response = fakeResponse();
  await receiver(harness)({ ...request, rawBody: undefined }, response);
  assert.equal(response.sent.status, 400);
  assert.equal(response.sent.body.outcome, "bad_envelope");
  assert.equal(harness.stores.state.inbox.size, 0);
});

test("the parsed body cannot smuggle a different event past the signature", async () => {
  // `request.body` says revision 99; `rawBody` -- what was signed -- says revision 1. The stored
  // record must carry the signed one.
  const harness = memoryAdapters();
  const event = makeEvent({ revision: 1 });
  const { request } = signedFunctionRequest({ event });
  request.body = { ...event, revision: 99 };
  await receiver(harness)(request, fakeResponse());
  assert.equal(harness.stores.state.inbox.get(event.event_id).event.revision, 1);
});

test("a bad signature is 401 and writes nothing", async () => {
  const harness = memoryAdapters();
  const { request, event } = signedFunctionRequest();
  request.rawBody = encode({ ...event, revision: 42 });
  const response = fakeResponse();
  await receiver(harness)(request, response);
  assert.equal(response.sent.status, 401);
  assert.equal(harness.stores.state.inbox.size, 0);
});

test("a store failure is 503 and never a 2xx", async () => {
  const harness = memoryAdapters();
  const handler = createNotificationReceiver({
    secrets: () => [TEST_SECRET],
    adapters: {
      ...harness.adapters,
      inbox: {
        ...harness.adapters.inbox,
        async put() {
          throw new Error("firestore unavailable");
        },
      },
    },
    now: () => NOW,
  });
  const response = fakeResponse();
  await handler(signedFunctionRequest().request, response);
  assert.equal(response.sent.status, 503);
});

test("a GET is 405 without touching anything", async () => {
  const harness = memoryAdapters();
  const response = fakeResponse();
  await receiver(harness)({ method: "GET", headers: {} }, response);
  assert.equal(response.sent.status, 405);
});

test("the outcome hook is given a status and an outcome, never the event or its prose", async () => {
  // The receiver's logging surface. Logging the event would put a traveller's origin, destination
  // and departure time into the client's log retention -- which is exactly what the sending side's
  // own log shapes are built to avoid.
  const harness = memoryAdapters();
  const reported = [];
  await createNotificationReceiver({
    secrets: () => [TEST_SECRET],
    adapters: harness.adapters,
    now: () => NOW,
    onOutcome: (outcome) => reported.push(outcome),
  })(signedFunctionRequest().request, fakeResponse());
  assert.equal(reported.length, 1);
  assert.deepEqual(Object.keys(reported[0]).sort(), ["outcome", "status"]);
  assert.equal(JSON.stringify(reported[0]).includes("innstilt"), false);
});

test("the secret resolver is asked per request, so a rotation needs no redeploy", async () => {
  const harness = memoryAdapters();
  let asked = 0;
  const handler = createNotificationReceiver({
    secrets: async () => {
      asked += 1;
      return [TEST_SECRET];
    },
    adapters: harness.adapters,
    now: () => NOW,
  });
  await handler(signedFunctionRequest({ event: makeEvent({ revision: 1 }) }).request, fakeResponse());
  await handler(signedFunctionRequest({ event: makeEvent({ revision: 2 }) }).request, fakeResponse());
  assert.equal(asked, 2);
});
