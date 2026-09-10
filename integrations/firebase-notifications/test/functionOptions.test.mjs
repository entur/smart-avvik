// The trigger's `retry` option, asserted against the installed package rather than the docs.
//
// The plan defers this check to implementation for a good reason: the spelling has moved across
// major versions of `firebase-functions`. v1 configured it as
// `functions.runWith({ failurePolicy: true })`; v2 -- which this package uses -- puts `retry` on the
// event-handler options, and it is reachable ONLY through the options-object overload of
// `onDocumentCreated`. The `document: string` overload, which is the shape every quickstart shows,
// has nowhere to put it and silently produces a trigger that never retries.
//
// That failure has no symptom until it matters. A non-retrying trigger works perfectly until the
// first Firestore blip, and then it loses exactly the events the retry window existed to make
// recoverable. So the assertion is on the generated endpoint, which is what the deploy actually
// sends.

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

import * as firestore from "firebase-functions/v2/firestore";

import { COLLECTIONS } from "../src/collections.ts";
import { createInboxWorker } from "../src/receiver.ts";
import { PACKAGE_ROOT, memoryAdapters } from "./helpers/adapters.mjs";

test("the installed firebase-functions is the version this was verified against", () => {
  // Read off disk rather than imported: `firebase-functions` does not export `./package.json` in
  // its `exports` map, so `import("firebase-functions/package.json")` throws
  // ERR_PACKAGE_PATH_NOT_EXPORTED.
  const manifest = JSON.parse(
    fs.readFileSync(
      path.join(PACKAGE_ROOT, "node_modules", "firebase-functions", "package.json"),
      "utf8",
    ),
  );
  assert.equal(
    manifest.version,
    "7.3.2",
    "the retry option's shape has moved across majors -- re-verify it before widening this",
  );
});

test("the inbox worker is a retry-enabled trigger over the inbox collection", () => {
  const worker = createInboxWorker(memoryAdapters().adapters);
  const trigger = worker.__endpoint.eventTrigger;
  assert.equal(trigger.retry, true, "the trigger does not retry -- a store blip would lose events");
  assert.equal(trigger.eventType, "google.cloud.firestore.document.v1.created");
  assert.equal(
    trigger.eventFilterPathPatterns.document,
    `${COLLECTIONS.inbox}/{eventId}`,
  );
});

test("the region rides on the same options object when one is given", () => {
  const worker = createInboxWorker(memoryAdapters().adapters, { region: "europe-west1" });
  assert.deepEqual(worker.__endpoint.region, ["europe-west1"]);
  assert.equal(worker.__endpoint.eventTrigger.retry, true);
});

test("the string overload CANNOT carry retry, which is why the options object is used", () => {
  // The trap, made visible. This is the shape every quickstart shows, and the endpoint it produces
  // carries `retry: false` -- so the trigger deploys, runs, and silently drops an event on the first
  // store blip. Nothing in a type check, a lint or a deploy log distinguishes it from the correct
  // one; only this assertion does.
  const naive = firestore.onDocumentCreated(`${COLLECTIONS.inbox}/{eventId}`, async () => {});
  assert.equal(naive.__endpoint.eventTrigger.retry, false);
});

test("the worker tolerates an event with no snapshot instead of retrying for a day", async () => {
  const { adapters, dispatched } = memoryAdapters();
  const worker = createInboxWorker(adapters);
  await worker.run({ data: undefined, params: { eventId: "evt_missing" } });
  assert.equal(dispatched.length, 0);
});

test("the worker runs a real record through to a dispatch", async () => {
  const { adapters, dispatched, stores, record } = await memoryAdapters().accepted();
  const worker = createInboxWorker(adapters);
  await worker.run({ data: { data: () => record }, params: { eventId: record.eventId } });
  assert.equal(dispatched.length, 1);
  assert.equal(stores.state.inbox.get(record.eventId).status, "dispatched");
});

test("the package entry imports every module -- a type-only value import would throw here", async () => {
  const entry = await import("../src/index.ts");
  for (const name of [
    "COLLECTIONS",
    "ALL_COLLECTIONS",
    "createFirestoreInbox",
    "createFirestoreSuppressionLog",
    "createFirestoreLedger",
    "encodeAlertId",
    "createInboxWorker",
    "createNotificationReceiver",
    "runRecoverySweep",
    "DEFAULT_SWEEP_LIMIT",
    "DEFAULT_SWEEP_MIN_AGE_MS",
  ]) {
    assert.ok(name in entry, `missing export ${name}`);
  }
});
