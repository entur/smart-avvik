// The DECLARED consumption shape, loaded the way a client loads it.
//
// KTD11: `node --test` over the .ts sources is a test convenience; what a client consumes is
// compiled CommonJS plus type declarations on the Node major their Cloud Functions runtime pins.
// Those are two different programs -- different module systems, a different resolver, different
// runtime semantics for a type-only import -- so a suite that only ever exercised the sources would
// pass while the published artifact failed to load at all.
//
// Run by `npm run verify:dist`, which builds first. Not part of `npm test`, which must stay
// build-free.

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

import { PACKAGE_ROOT } from "../helpers/events.mjs";

const require = createRequire(import.meta.url);
const DIST = path.join(PACKAGE_ROOT, "dist");

test("the artifact loads through require(), as CommonJS", () => {
  const loaded = require(path.join(DIST, "index.js"));
  // A named export off a CommonJS module: this is the call that fails if the emit went ESM, and it
  // is the whole reason the build is verified separately from the sources.
  assert.equal(typeof loaded.receive, "function");
  assert.equal(typeof loaded.process, "function");
  assert.equal(typeof loaded.verifySignature, "function");
  assert.equal(typeof loaded.decideSuppression, "function");
  assert.equal(typeof loaded.createMemoryStores, "function");
  assert.equal(typeof loaded.createNodeReceiver, "function");
  assert.equal(typeof loaded.parseEvent, "function");
  assert.equal(loaded.SCHEMA_VERSION, "1");
  assert.equal(loaded.DEFAULT_MAX_BODY_BYTES, 64 * 1024);
});

test("the packaged manifest points at the artifact that exists", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(PACKAGE_ROOT, "package.json"), "utf8"));
  assert.ok(fs.existsSync(path.join(PACKAGE_ROOT, manifest.main)), manifest.main);
  assert.ok(fs.existsSync(path.join(PACKAGE_ROOT, manifest.types)), manifest.types);
});

test("every source module has a declaration file beside it", () => {
  // The second half of the declared shape. A client without .d.ts gets `any` for the whole
  // package, which is exactly the homework KTD11 says the helper exists to remove.
  const sources = fs
    .readdirSync(path.join(PACKAGE_ROOT, "src"))
    .filter((name) => name.endsWith(".ts"));
  for (const name of sources) {
    const declaration = name.replace(/\.ts$/, ".d.ts");
    assert.ok(fs.existsSync(path.join(DIST, declaration)), `missing ${declaration}`);
  }
});

test("the emitted JavaScript is CommonJS, with the .ts import extensions rewritten", () => {
  const emitted = fs.readFileSync(path.join(DIST, "client.js"), "utf8");
  assert.match(emitted, /require\(/);
  assert.doesNotMatch(emitted, /^import\s/m, "an ESM import survived into the CommonJS emit");
  assert.doesNotMatch(emitted, /require\("\.\/[a-zA-Z]+\.ts"\)/, "a .ts extension survived");
  assert.match(emitted, /require\("\.\/contracts\.js"\)/);
});

test("the quickstart from the README runs against the built artifact", async () => {
  // The quickstart is what a client types first, and running it here is what stops it drifting from
  // the API. It is the README's snippet, condensed: verify, persist, decide, dispatch.
  const {
    createMemoryStores,
    process: processRecord,
    receive,
    signatureHeader,
  } = require(path.join(DIST, "index.js"));

  const secret = "whsec_BkAJ0Xm6KdFJFqTHtWVZNVkRbPfjySIekSHQGgGwbX8=";
  const stores = createMemoryStores();
  const event = {
    schema_version: "1",
    event_id: "evt_quickstart",
    type: "journey.alert.created",
    phase: "predicted",
    alert_id: "sub_a:VYG:L1:2026-09-09:1",
    revision: 1,
    occurred_at: "2026-09-09T05:00:00+00:00",
    valid_until: "2026-09-09T06:00:00+00:00",
    subscription_id: "sub_a",
    subscription_version: 1,
    external_ref: "commuter-4711",
    test: false,
    operator: "VYG",
    line: "VYG:Line:L1",
    journey_ref: "VYG:DatedServiceJourney:1234",
    call_date: "2026-09-09",
    stop_place: "NSR:StopPlace:337",
    stop_order: 3,
    situations: [],
    payload: { notification: { title: "Tog L1 07:18 er innstilt", body: "Oslo S → Ski" } },
  };
  const body = Buffer.from(JSON.stringify(event), "utf8");
  const now = new Date("2026-09-09T05:10:00Z");
  const timestampSeconds = Math.floor(now.getTime() / 1000);

  const accepted = await receive(
    { secrets: [secret], inbox: stores.inbox, now: () => now },
    {
      headers: {
        "webhook-id": "dlv_quickstart",
        "webhook-timestamp": String(timestampSeconds),
        "webhook-signature": signatureHeader({
          secrets: [secret],
          webhookId: "dlv_quickstart",
          timestampSeconds,
          body,
        }),
      },
      body,
      declaredBytes: body.byteLength,
    },
  );
  assert.equal(accepted.status, 200);

  const dispatched = [];
  const outcome = await processRecord(
    {
      inbox: stores.inbox,
      ledger: stores.ledger,
      suppressions: stores.suppressions,
      async recipients() {
        return ["device-token"];
      },
      async eligibility() {
        return { ok: true };
      },
      async dispatch(request) {
        dispatched.push(request);
      },
      now: () => now,
    },
    await stores.inbox.get("evt_quickstart"),
  );
  assert.deepEqual(outcome, { outcome: "dispatched", recipients: 1 });
  assert.equal(dispatched[0].display, "alert");
  assert.equal(dispatched[0].event.payload.notification.title, "Tog L1 07:18 er innstilt");
});
