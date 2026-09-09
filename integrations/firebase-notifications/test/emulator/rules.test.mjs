// `firestore.rules`, asserted in the emulator against a real client SDK.
//
// The rules are not defence in depth here; they are the only thing between a client's own app users
// and this data. A Firestore database in a project with a Firebase app registered -- which a
// client's mobile BFF certainly has -- is reachable by a client SDK holding a public API key, and
// the console's default template opens the whole database to the world until a date passes.
//
// The three things an open ruleset would allow, and the second and third are why this is not merely
// a disclosure risk:
//
//   * READ every event: the Norwegian prose of every commuter's disruption alert, with their origin,
//     destination and departure time.
//   * FORGE a `notification_decisions` document marking a real event as already settled, which
//     SUPPRESSES a genuine cancellation notification before it is ever dispatched.
//   * FORGE a `notification_inbox` document, which fires the trigger and INJECTS a notification.
//
// The suite asserts denial for an unauthenticated context AND for a signed-in one, because "signed
// in" is the state a client's own users are in, and a rule of `if request.auth != null` is the
// commonest way this data ends up readable. It also asserts an ADMIN write succeeding in the same
// run: without that, every deny assertion would pass equally well against a broken emulator.
//
// THE TWO EMULATOR FILES RUN SERIALLY (`--test-concurrency=1` in `test:emulator`), and that is not a
// speed preference. `node --test` runs test FILES in parallel by default, and the Firestore emulator
// is started with `--single_project_mode`, under which a distinct project id does NOT give a
// distinct database -- every write from both files lands in one data space. So the sibling file's
// admin writes and this file's `beforeEach` truncation interleave. The symptom is a lie: a test
// asserting one decision document finds four, which reads as a bug in the ledger. Measured, and
// reproducible in roughly one run in three before the flag was added.

import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

import {
  assertFails,
  assertSucceeds,
  initializeTestEnvironment,
} from "@firebase/rules-unit-testing";
import { doc, getDoc, setDoc, deleteDoc, setLogLevel } from "firebase/firestore";
import { deleteApp, initializeApp } from "firebase-admin/app";
import { getFirestore } from "firebase-admin/firestore";

import { ALL_COLLECTIONS, COLLECTIONS } from "../../src/collections.ts";
import { PACKAGE_ROOT } from "../helpers/adapters.mjs";

// Derived from the project the emulator was started for, so the rules suite cannot drift from the
// data suite or from `--project`. Two ids, because the two rulesets differ and rules are per project.
const PROJECT_ID = `${process.env.GCLOUD_PROJECT}-rules`;
const PERMISSIVE_PROJECT_ID = `${process.env.GCLOUD_PROJECT}-rules-permissive`;

let environment;
let adminApp;

before(async () => {
  assert.ok(
    process.env.FIRESTORE_EMULATOR_HOST,
    "FIRESTORE_EMULATOR_HOST is unset -- run this through `npm run test:emulator`",
  );
  // Every assertion below is a PERMISSION_DENIED, and the client SDK logs each one at error level
  // with a stack. A green run that prints twenty errors teaches its reader to skip the output, which
  // is how a real failure goes unnoticed -- so the denials are the test's result rather than its
  // output.
  setLogLevel("silent");
  const [host, port] = process.env.FIRESTORE_EMULATOR_HOST.split(":");

  // Drop FIREBASE_EMULATOR_HUB before initialising, and this is a fix rather than tidying.
  //
  // `initializeTestEnvironment` calls `discoverEmulators(hub)` UNCONDITIONALLY whenever that
  // variable is set -- read the library's own source, not its documentation: the explicit
  // `firestore: { host, port }` below is consulted only AFTER the hub has answered. So a suite that
  // needs nothing from the hub still fails outright when the hub is not listening, with
  // `TypeError: fetch failed / connect ECONNREFUSED 127.0.0.1:4401` out of `before()` and therefore
  // every rules assertion marked failed.
  //
  // That is not hypothetical. Measured on this machine: start the repo-root emulator suite and this
  // one at the same instant, and the Firebase CLI's hub comes up late or not at all -- it printed
  // its generic `Error: An unexpected error has occurred.` before running the script, and a probe
  // found 4401 listening for 6 samples out of 40. Separating the ports cannot help, because what is
  // contended is the CLI's own hub startup rather than a port this config can name.
  //
  // Deleting the variable makes the suite depend on exactly what it was already given.
  delete process.env.FIREBASE_EMULATOR_HUB;
  environment = await initializeTestEnvironment({
    projectId: PROJECT_ID,
    firestore: {
      // The SHIPPED file, read from disk. Loading anything else here would test a string in this
      // suite rather than the artifact a client installs.
      rules: fs.readFileSync(path.join(PACKAGE_ROOT, "firestore.rules"), "utf8"),
      host,
      port: Number(port),
    },
  });
  adminApp = initializeApp({ projectId: PROJECT_ID }, `rules-admin-${Date.now()}`);
});

after(async () => {
  await environment.cleanup();
  await deleteApp(adminApp);
});

test("the rules file names every collection this package writes", () => {
  // A collection added in code and forgotten in the rules is readable by any signed-in user of the
  // client's app, and nothing in a TypeScript build would notice. So the code's own list is checked
  // against the file.
  const rules = fs.readFileSync(path.join(PACKAGE_ROOT, "firestore.rules"), "utf8");
  for (const collection of ALL_COLLECTIONS) {
    assert.match(rules, new RegExp(`match /${collection}/`), `${collection} is not in the rules`);
  }
});

test("firebase.json names the rules file, or emulators:exec would load none", () => {
  const config = JSON.parse(fs.readFileSync(path.join(PACKAGE_ROOT, "firebase.json"), "utf8"));
  assert.equal(config.firestore.rules, "firestore.rules");
  assert.equal(config.firestore.indexes, "firestore.indexes.json");
});

test("an UNAUTHENTICATED client SDK can neither read nor write any of the collections", async () => {
  const firestore = environment.unauthenticatedContext().firestore();
  for (const collection of ALL_COLLECTIONS) {
    const reference = doc(firestore, collection, "any-document");
    await assertFails(getDoc(reference));
    await assertFails(setDoc(reference, { forged: true }));
    await assertFails(deleteDoc(reference));
  }
});

test("a SIGNED-IN client SDK can neither read nor write any of the collections", async () => {
  // The state a client's own app users are in. `allow read: if request.auth != null` is the
  // commonest ruleset in the world and it would pass the unauthenticated test above.
  const firestore = environment.authenticatedContext("a-client-app-user").firestore();
  for (const collection of ALL_COLLECTIONS) {
    const reference = doc(firestore, collection, "any-document");
    await assertFails(getDoc(reference));
    await assertFails(setDoc(reference, { forged: true }));
    await assertFails(deleteDoc(reference));
  }
});

test("a client SDK cannot forge the two documents that change what a traveller is told", async () => {
  // Named individually because these two are not disclosure, they are a write path into the
  // notification stream: a forged decision marker suppresses a real cancellation, and a forged inbox
  // document fires the trigger and injects one.
  const firestore = environment.authenticatedContext("a-client-app-user").firestore();
  await assertFails(
    setDoc(doc(firestore, COLLECTIONS.decisions, "evt_real"), {
      settled: true,
      decision: { action: "drop", rule: "duplicate_event" },
    }),
  );
  await assertFails(
    setDoc(doc(firestore, COLLECTIONS.inbox, "evt_injected"), {
      event: { payload: { notification: { title: "Toget er innstilt", body: "" } } },
      status: "pending",
    }),
  );
});

test("the SERVER SDK is unaffected -- which is also what proves the emulator is not simply broken", async () => {
  // Security Rules do not apply to server client libraries at all: the Cloud Function authenticates
  // with a service account and is authorized by project IAM, which Rules cannot narrow. If this
  // write failed, every `assertFails` above would be meaningless.
  const db = getFirestore(adminApp);
  await db.collection(COLLECTIONS.inbox).doc("evt_admin").set({ status: "pending" });
  const snapshot = await db.collection(COLLECTIONS.inbox).doc("evt_admin").get();
  assert.equal(snapshot.exists, true);
  await db.collection(COLLECTIONS.inbox).doc("evt_admin").delete();
});

test("a path the package does not use is denied too, by the catch-all", async () => {
  // The catch-all is right for a project dedicated to this receiver and is the line a client drops
  // when merging into a project that also serves their app. Asserting it here documents which
  // behaviour is lost by that edit.
  const firestore = environment.authenticatedContext("a-client-app-user").firestore();
  await assertFails(getDoc(doc(firestore, "some_other_collection", "x")));
});

test("a permissive rule elsewhere in the ruleset DEFEATS these denials", async () => {
  // Firestore evaluates rules as a UNION: access is granted if ANY matching rule allows it, and a
  // deny cannot override an allow elsewhere in the ruleset. So the four matches in the shipped file
  // are NECESSARY and not SUFFICIENT; what makes them sufficient is that no broader rule in the
  // client's ruleset allows these paths. This is asserted as a property of a ruleset built here,
  // so the README's merge instruction is demonstrated rather than merely claimed.
  const permissive = await initializeTestEnvironment({
    projectId: PERMISSIVE_PROJECT_ID,
    firestore: {
      rules: `${fs
        .readFileSync(path.join(PACKAGE_ROOT, "firestore.rules"), "utf8")
        .replace(/match \/\{document=\*\*\} \{[\s\S]*?\}/, "")}
`.replace(
        /service cloud\.firestore \{\s*match \/databases\/\{database\}\/documents \{/,
        `service cloud.firestore {
  match /databases/{database}/documents {
    match /{anything=**} { allow read, write: if request.auth != null; }`,
      ),
      host: process.env.FIRESTORE_EMULATOR_HOST.split(":")[0],
      port: Number(process.env.FIRESTORE_EMULATOR_HOST.split(":")[1]),
    },
  });
  try {
    const firestore = permissive.authenticatedContext("a-client-app-user").firestore();
    await assertSucceeds(setDoc(doc(firestore, COLLECTIONS.inbox, "evt_leaked"), { forged: true }));
  } finally {
    await permissive.cleanup();
  }
});
