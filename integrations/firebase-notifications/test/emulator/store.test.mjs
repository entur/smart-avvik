// The Firestore-backed inbox and ledger, against the emulator.
//
// Run by `npm run test:emulator`, which wraps this in `firebase emulators:exec --project demo-kix`.
// A `demo-` project id is what makes it credential-free: the admin SDK picks up
// FIRESTORE_EMULATOR_HOST and skips Application Default Credentials entirely, and a demo project can
// reach no real GCP service.
//
// What the emulator can and cannot establish, stated rather than assumed. It uses a simple locking
// approach and does NOT mirror production's concurrency, so nothing here proves Firestore's
// transaction semantics -- those are argued from the transaction contract. What it does prove is
// that this code USES those semantics: that the duplicate check, the state read and the write are
// inside one `runTransaction`, that `create()` and not `set()` guards the inbox, and that the
// document ids are legal.
//
// THE TWO EMULATOR FILES RUN SERIALLY (`--test-concurrency=1` in `test:emulator`), and that is not a
// speed preference. `node --test` runs test FILES in parallel by default, and the Firestore emulator
// is started with `--single_project_mode`, under which a distinct project id does NOT give a
// distinct database -- every write from both files lands in one data space. So the sibling file's
// admin writes and this file's `beforeEach` truncation interleave. The symptom is a lie: a test
// asserting one decision document finds four, which reads as a bug in the ledger. Measured, and
// reproducible in roughly one run in three before the flag was added.

import { test, before, beforeEach, after } from "node:test";
import assert from "node:assert/strict";

import { deleteApp, initializeApp } from "firebase-admin/app";
import { getFirestore } from "firebase-admin/firestore";

import { COLLECTIONS } from "../../src/collections.ts";
import { createFirestoreInbox, createFirestoreSuppressionLog } from "../../src/inbox.ts";
import { createFirestoreLedger } from "../../src/ledger.ts";
import { encodeDocumentId } from "../../src/collections.ts";
import { process as processRecord, receive } from "@entur/kix-notifications-client";

import { NOW, TEST_SECRET, makeEvent, signedFunctionRequest } from "../helpers/adapters.mjs";

// Read from the environment, never hard-coded: `emulators:exec --project X` exports it as
// GCLOUD_PROJECT and the emulator runs in `--single_project_mode`, so a suite naming a different
// project is refused by the emulator rather than merely writing somewhere else.
const PROJECT_ID = process.env.GCLOUD_PROJECT;

let app;
let db;
let inbox;
let ledger;

before(() => {
  assert.ok(
    process.env.FIRESTORE_EMULATOR_HOST,
    "FIRESTORE_EMULATOR_HOST is unset -- run this through `npm run test:emulator`, never against a real project",
  );
  assert.ok(PROJECT_ID?.startsWith("demo-"), `GCLOUD_PROJECT is ${PROJECT_ID}, which is not a demo project`);
  app = initializeApp({ projectId: PROJECT_ID }, `store-${Date.now()}`);
  db = getFirestore(app);
  inbox = createFirestoreInbox(db);
  ledger = createFirestoreLedger(db);
});

after(async () => {
  await deleteApp(app);
});

beforeEach(async () => {
  // The emulator's own clear endpoint, not a query-and-delete loop.
  //
  // Deleting document by document reads the collection first, and that read is not reliable while
  // the previous test's writes are still settling: measured on this machine, a test asserting one
  // decision document found FOUR -- three of them leftovers a query-based clear had failed to see.
  // The failure looks exactly like a bug in the code under test, which is the worst property a
  // fixture can have. The REST endpoint truncates the database in one call and cannot be raced.
  const response = await fetch(
    `http://${process.env.FIRESTORE_EMULATOR_HOST}/emulator/v1/projects/${PROJECT_ID}/databases/(default)/documents`,
    { method: "DELETE" },
  );
  assert.ok(response.ok, `could not clear the emulator: ${response.status}`);
});

function record(event, overrides = {}) {
  return {
    eventId: event.event_id,
    webhookId: "dlv_emulator",
    receivedAt: NOW.toISOString(),
    bodyBase64: Buffer.from(JSON.stringify(event), "utf8").toString("base64"),
    event,
    status: "pending",
    attempts: 0,
    ...overrides,
  };
}

test("an inbox record is created once; a redelivery reports a duplicate", async () => {
  const event = makeEvent();
  assert.equal(await inbox.put(record(event)), "created");
  assert.equal(await inbox.put(record(event)), "duplicate");
  const snapshot = await db.collection(COLLECTIONS.inbox).get();
  assert.equal(snapshot.size, 1);
});

test("AE5: concurrent deliveries of one event create ONE inbox record", async () => {
  const event = makeEvent();
  const results = await Promise.all([
    inbox.put(record(event)),
    inbox.put(record(event)),
    inbox.put(record(event)),
  ]);
  assert.equal(results.filter((result) => result === "created").length, 1);
  assert.equal(results.filter((result) => result === "duplicate").length, 2);
  assert.equal((await db.collection(COLLECTIONS.inbox).get()).size, 1);
});

test("a duplicate does not OVERWRITE the record already there", async () => {
  // The failure `set()` would produce: the second delivery silently replaces the first, resetting
  // the status of a record a worker is mid-way through.
  const event = makeEvent();
  await inbox.put(record(event));
  await inbox.settle(event.event_id, "dispatched");
  await inbox.put(record(event));
  const stored = await inbox.get(event.event_id);
  assert.equal(stored.status, "dispatched");
});

test("the stored record keeps the exact signed bytes", async () => {
  const event = makeEvent();
  const original = record(event);
  await inbox.put(original);
  const stored = await inbox.get(event.event_id);
  assert.equal(stored.bodyBase64, original.bodyBase64);
  assert.deepEqual(JSON.parse(Buffer.from(stored.bodyBase64, "base64").toString("utf8")), event);
});

test("attempts are counted across workers", async () => {
  const event = makeEvent();
  await inbox.put(record(event));
  await inbox.noteAttempt(event.event_id);
  await inbox.noteAttempt(event.event_id);
  assert.equal((await inbox.get(event.event_id)).attempts, 2);
});

test("the ledger decides once and replays an unsettled decision verbatim", async () => {
  const event = makeEvent({ revision: 5 });
  const first = await ledger.decide(event);
  assert.equal(first.action, "dispatch");
  const replay = await ledger.decide(event);
  assert.deepEqual(replay, first, "an unfinished dispatch is replayed, not reported as a duplicate");
  await ledger.settle(event.event_id);
  const afterSettle = await ledger.decide(event);
  assert.equal(afterSettle.action, "drop");
  assert.equal(afterSettle.rule, "duplicate_event");
});

test("the three suppression rules hold through the transaction", async () => {
  const alertId = "sub_a:VYG:L1:2026-09-09:1";
  const confirmed = makeEvent({ alert_id: alertId, revision: 12, phase: "confirmed" });
  assert.equal((await ledger.decide(confirmed)).action, "dispatch");
  await ledger.settle(confirmed.event_id);

  // Rule 1.
  assert.equal((await ledger.decide(confirmed)).rule, "duplicate_event");
  // Rule 2.
  const older = makeEvent({ alert_id: alertId, revision: 11, phase: "predicted" });
  assert.equal((await ledger.decide(older)).rule, "stale_revision");
  // Rule 3 -- a NEWER forecast for a confirmed train.
  const newerForecast = makeEvent({ alert_id: alertId, revision: 99, phase: "predicted" });
  assert.equal((await ledger.decide(newerForecast)).rule, "already_confirmed");

  assert.deepEqual(await ledger.standing(alertId), { revision: 12, phase: "confirmed" });
});

test("concurrent decisions about one event produce ONE decision document", async () => {
  // A contention abort is RETRIED here rather than failed on, and that is not the test being made
  // lenient. The Verification Contract says it in as many words: the emulator "uses a simple locking
  // approach and does not mirror production concurrency", so its behaviour under three simultaneous
  // transactions on one document is a fact about the emulator, not about this code -- measured on a
  // loaded machine, one of them fails with `3 INVALID_ARGUMENT: Transaction is invalid or closed`,
  // and with a THIRD contender and a second emulator running beside it, `10 ABORTED: Transaction
  // lock timeout` after 53 s. Production's answer to that same condition is a retry, which is
  // exactly what the sending side does (its client raises after five attempts and the plan classes
  // an exhausted transaction as retryable), so retrying is the faithful model and failing would be
  // the artefact.
  //
  // TWO contenders, not three, for the same reason: the property under test -- one decision, one
  // state, one document -- is fully exercised by two, and a third only buys contention the emulator
  // cannot serve. A test that spends 53 s losing a lock is measuring the emulator.
  //
  // What is asserted is unchanged and is the property that matters: ONE decision document, ONE
  // standing state, and every caller seeing the same decision.
  const event = makeEvent({ revision: 7 });
  const decide = async () => {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        return await ledger.decide(event);
      } catch (error) {
        if (attempt === 2) {
          throw error;
        }
      }
    }
    throw new Error("unreachable");
  };
  const decisions = await Promise.all([decide(), decide()]);
  for (const decision of decisions) {
    assert.deepEqual(decision, decisions[0], "two workers reached two different decisions");
  }
  assert.equal((await db.collection(COLLECTIONS.decisions).get()).size, 1);
  assert.deepEqual(await ledger.standing(event.alert_id), { revision: 7, phase: "predicted" });
});

test("two subscriptions about one train keep separate ledger state (AE17)", async () => {
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
  assert.equal((await ledger.decide(morning)).action, "dispatch");
  assert.equal((await ledger.decide(evening)).action, "dispatch");
  assert.deepEqual(await ledger.standing(morning.alert_id), { revision: 40, phase: "predicted" });
  assert.deepEqual(await ledger.standing(evening.alert_id), { revision: 3, phase: "predicted" });
});

test("an alert id with characters Firestore forbids in a document id is still storable", async () => {
  // `alert_id` is opaque to a receiver -- the sender publishes no format -- so it must not be used
  // as a document id unchanged. A `/` would silently create a SUBCOLLECTION rather than a document,
  // and `.` or `..` are refused outright.
  for (const alertId of ["sub/a:VYG/L1", "..", ".", "a#b?c", "sub a:VYG:L1"]) {
    const event = makeEvent({ alert_id: alertId, revision: 2 });
    const decision = await ledger.decide(event);
    assert.equal(decision.action, "dispatch", alertId);
    assert.deepEqual(await ledger.standing(alertId), { revision: 2, phase: "predicted" }, alertId);
    assert.equal(encodeDocumentId(alertId).includes("/"), false, alertId);
  }
});

test("the sweep query returns pending records oldest first, bounded", async () => {
  for (let index = 0; index < 4; index += 1) {
    const event = makeEvent({ revision: index + 1, alert_id: `sub_a:t:${index}` });
    await inbox.put(
      record(event, { receivedAt: new Date(NOW.getTime() + index * 1000).toISOString() }),
    );
  }
  await inbox.settle(
    (await db.collection(COLLECTIONS.inbox).get()).docs[0].data().eventId,
    "dispatched",
  );
  const pending = await inbox.pending({
    receivedBefore: new Date(NOW.getTime() + 60_000),
    limit: 2,
  });
  assert.equal(pending.length, 2);
  assert.ok(pending[0].receivedAt <= pending[1].receivedAt);
  for (const found of pending) {
    assert.equal(found.status, "pending");
  }
});

test("a suppression is recorded once per (event, rule), not once per retry", async () => {
  const suppressions = createFirestoreSuppressionLog(db);
  const event = makeEvent();
  const entry = {
    eventId: event.event_id,
    alertId: event.alert_id,
    rule: "stale_revision",
    detail: "revision 3 is not newer than the applied 5",
    at: NOW.toISOString(),
  };
  await suppressions.record(entry);
  await suppressions.record(entry);
  assert.equal((await db.collection(COLLECTIONS.suppressions).get()).size, 1);
});

test("scenario 1 end to end: two deliveries and a restart give one record and one decision", async () => {
  // The whole of AE5 through the real stores rather than through the in-memory ones: receive twice
  // concurrently, then process the surviving record twice as a restarted worker would.
  const event = makeEvent({ revision: 3 });
  const suppressionLog = createFirestoreSuppressionLog(db);
  const dispatched = [];
  const adapters = {
    inbox,
    ledger,
    suppressions: suppressionLog,
    async recipients() {
      return ["device-1"];
    },
    async eligibility() {
      return { ok: true };
    },
    async dispatch(request) {
      dispatched.push(request);
    },
    now: () => NOW,
  };

  const request = signedFunctionRequest({ event }).request;
  const config = { secrets: [TEST_SECRET], inbox, now: () => NOW };
  const outcomes = await Promise.all([
    receive(config, {
      headers: request.headers,
      body: request.rawBody,
      declaredBytes: request.rawBody.byteLength,
    }),
    receive(config, {
      headers: request.headers,
      body: request.rawBody,
      declaredBytes: request.rawBody.byteLength,
    }),
  ]);
  assert.deepEqual(
    outcomes.map((outcome) => outcome.outcome).sort(),
    ["accepted", "duplicate"],
  );
  assert.equal((await db.collection(COLLECTIONS.inbox).get()).size, 1);

  const stored = await inbox.get(event.event_id);
  await processRecord(adapters, stored);
  // The "restart": the same record, processed again by a fresh worker.
  const second = await processRecord(adapters, stored);
  // The freshness read at `process()` entry stops it before the ledger: the record is no longer
  // `pending`, so nothing re-decides and nothing rewrites its terminal status.
  assert.deepEqual(second, { outcome: "already_settled", status: "dispatched" });
  assert.equal(dispatched.length, 1, "one native decision, one dispatch");
  assert.equal((await db.collection(COLLECTIONS.decisions).get()).size, 1);
});

test("settle never moves a record off a terminal status", async () => {
  // The write that would destroy the only record of what happened: a re-entry -- an Eventarc
  // redelivery, a sweep racing the trigger -- settling a `dispatched` event as `suppressed` or
  // `expired`. No duplicate notification results, which is exactly why it is easy to miss.
  const event = makeEvent();
  await inbox.put(record(event));
  await inbox.settle(event.event_id, "dispatched");
  await inbox.settle(event.event_id, "suppressed", "duplicate_event");
  await inbox.settle(event.event_id, "expired", "past valid_until");
  assert.equal((await inbox.get(event.event_id)).status, "dispatched");
});

test("an event id with characters Firestore forbids in a document id is still storable", async () => {
  // `event_id` is a 40-character hex digest on the sending side today, and the schema promises only
  // that it is immutable and deterministic. Encoding it costs nothing for the ordinary case -- hex
  // encodes to itself -- and a `/` slipping in would otherwise create a SUBCOLLECTION, where the
  // write appears to succeed and the read finds nothing.
  for (const [index, eventId] of ["evt/with/slashes", "..", "evt with spaces"].entries()) {
    // A distinct alert per case: sharing one would make the second iteration a stale revision and
    // the test would be measuring rule 2 rather than the document id.
    const event = makeEvent({ event_id: eventId, alert_id: `sub_a:VYG:L1:2026-09-09:id${index}` });
    assert.equal(await inbox.put(record(event)), "created", eventId);
    const stored = await inbox.get(eventId);
    assert.ok(stored, eventId);
    assert.equal(stored.eventId, eventId);
    assert.equal(await inbox.put(record(event)), "duplicate", eventId);
    assert.equal((await ledger.decide(event)).action, "dispatch", eventId);
    await ledger.settle(eventId);
    assert.equal((await ledger.decide(event)).rule, "duplicate_event", eventId);
  }
});
