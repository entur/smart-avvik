// The declared consumption shape, loaded the way a client's Cloud Functions runtime loads it.
//
// A Firebase Functions deployment `require()`s the entry point at cold start. That is CommonJS on
// the Node major the runtime pins, which is a different program from the .ts sources `node --test`
// runs -- different module system, different resolver, different runtime treatment of a type-only
// import. A suite that only exercised the sources would pass while the deployed function failed to
// load at all, and the symptom is a cold-start crash rather than a test failure.
//
// Run by `npm run verify:dist`, which builds both this package and its sibling first.

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

import { PACKAGE_ROOT } from "../helpers/adapters.mjs";

const require = createRequire(import.meta.url);
const DIST = path.join(PACKAGE_ROOT, "dist");

test("the artifact loads through require(), as CommonJS", () => {
  const loaded = require(path.join(DIST, "index.js"));
  assert.equal(typeof loaded.createNotificationReceiver, "function");
  assert.equal(typeof loaded.createInboxWorker, "function");
  assert.equal(typeof loaded.createFirestoreInbox, "function");
  assert.equal(typeof loaded.createFirestoreLedger, "function");
  assert.equal(typeof loaded.createFirestoreSuppressionLog, "function");
  assert.equal(typeof loaded.runRecoverySweep, "function");
  assert.equal(loaded.COLLECTIONS.inbox, "notification_inbox");
});

test("the compiled worker still declares retry -- the option survives the build", () => {
  // The check that would be missed by testing only the sources: `retry` is set in an options object
  // and read by the deploy from `__endpoint`, so an emit that dropped or reordered it would produce
  // a trigger that never retries, with nothing failing until the first store blip.
  const loaded = require(path.join(DIST, "index.js"));
  const worker = loaded.createInboxWorker({
    inbox: {},
    ledger: {},
    suppressions: {},
    recipients: async () => [],
    eligibility: async () => ({ ok: true }),
    dispatch: async () => {},
  });
  assert.equal(worker.__endpoint.eventTrigger.retry, true);
});

test("the sibling client package is consumed as its BUILT artifact, not as sources", () => {
  // The `file:` link resolves to the sibling's package.json `main`, which is its dist. So a client
  // vendoring both gets two compiled packages, and this package never depends on Node's type
  // stripping at runtime.
  const link = path.join(PACKAGE_ROOT, "node_modules", "@entur", "smart-avvik-notifications-client");
  const manifest = JSON.parse(fs.readFileSync(path.join(link, "package.json"), "utf8"));
  assert.equal(manifest.main, "./dist/index.js");
  assert.ok(fs.existsSync(path.join(link, manifest.main)));
  const emitted = fs.readFileSync(path.join(DIST, "receiver.js"), "utf8");
  assert.match(emitted, /require\("@entur\/smart-avvik-notifications-client"\)/);
});

test("every source module has a declaration file beside it", () => {
  const sources = fs
    .readdirSync(path.join(PACKAGE_ROOT, "src"))
    .filter((name) => name.endsWith(".ts"));
  for (const name of sources) {
    assert.ok(
      fs.existsSync(path.join(DIST, name.replace(/\.ts$/, ".d.ts"))),
      `missing declaration for ${name}`,
    );
  }
});

test("the packaged manifest points at files that exist and ships the rules", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(PACKAGE_ROOT, "package.json"), "utf8"));
  assert.ok(fs.existsSync(path.join(PACKAGE_ROOT, manifest.main)));
  assert.ok(fs.existsSync(path.join(PACKAGE_ROOT, manifest.types)));
  // The rules are part of the deliverable, not documentation: a client who installs the code and
  // not the rules has an open database.
  assert.ok(manifest.files.includes("firestore.rules"));
  assert.ok(manifest.files.includes("firestore.indexes.json"));
});
