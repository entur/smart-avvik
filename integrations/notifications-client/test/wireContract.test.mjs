// The wire envelope, pinned from this side, in both directions.
//
// The sender projects its stored event onto an allow-list of exactly the schema's top-level
// properties, and it does so because a deny-list would have shipped two extra fields that a
// conforming receiver refuses -- on every event, for every partner. That is the sender's half. This
// is the receiver's half of the same pin, and the two halves are what make the claim symmetric:
//
//   * no field the schema does not declare is accepted (a regression on the sender fails HERE, in a
//     test, rather than in production at a partner running a stricter verifier);
//   * every field the schema declares required is required (a projection that silently dropped one
//     produces a body that declares it required and does not carry it, which fails at the receiver
//     and not at the sender);
//   * the generated types are in step with the vendored schema, and the vendored schema is
//     byte-identical to the authored one.

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";

import {
  ENVELOPE_CHECKED_FIELDS,
  EVENT_REQUIRED_FIELDS,
  EVENT_WIRE_FIELDS,
  SCHEMA_VERSION,
  parseEvent,
} from "../src/contracts.ts";
import { EVENT_SCHEMA, PACKAGE_ROOT, REPO_ROOT, encode, makeEvent } from "./helpers/events.mjs";

test("the wire field list is exactly the schema's top-level properties, in order", () => {
  assert.deepEqual([...EVENT_WIRE_FIELDS], Object.keys(EVENT_SCHEMA.properties));
});

test("the required field list is exactly the schema's required set, in order", () => {
  assert.deepEqual([...EVENT_REQUIRED_FIELDS], EVENT_SCHEMA.required);
});

test("the schema really is additionalProperties: false, which is what makes the allow-list binding", () => {
  assert.equal(EVENT_SCHEMA.additionalProperties, false);
  assert.equal(EVENT_SCHEMA.properties.schema_version.const, SCHEMA_VERSION);
});

test("every declared property has a runtime check, and every runtime check names a declared property", () => {
  // Both directions. A property added to the schema with no entry in the kind table would be
  // accepted with any value at all; an entry naming a property the schema dropped is dead code that
  // reads like coverage.
  assert.deepEqual([...ENVELOPE_CHECKED_FIELDS].sort(), [...EVENT_WIRE_FIELDS].sort());
});

test("a valid event round-trips through parseEvent", () => {
  const event = makeEvent();
  const parsed = parseEvent(encode(event));
  assert.equal(parsed.ok, true);
  assert.deepEqual(parsed.event, event);
});

test("the three fields the sender's store holds but must never send are refused", () => {
  // Named individually rather than as "any extra field", because these three are the ones a
  // deny-list regression on the sender would actually ship: `partner_id` and `destination_id` are
  // its own routing, and `expires_at` is a retention decision that is NOT validity -- on the wire it
  // would invite a receiver to read it as one.
  for (const field of ["partner_id", "destination_id", "expires_at"]) {
    const body = { ...makeEvent(), [field]: "leaked" };
    const parsed = parseEvent(encode(body));
    assert.equal(parsed.ok, false, field);
    assert.equal(parsed.problem, "unknown_field");
    assert.equal(parsed.detail, field);
  }
});

test("a missing required field is refused, one field at a time", () => {
  for (const field of EVENT_REQUIRED_FIELDS) {
    const body = makeEvent();
    delete body[field];
    const parsed = parseEvent(encode(body));
    assert.equal(parsed.ok, false, field);
    if (field === "schema_version") {
      // The version is checked before anything else, so its absence reads as an unknown version
      // rather than as a missing field. That is the better answer: a body with no `schema_version`
      // is a body from something that is not this contract, and reporting it as one absent property
      // among nineteen present ones would understate it.
      assert.equal(parsed.problem, "unknown_schema_version");
      continue;
    }
    assert.equal(parsed.problem, "missing_field", field);
    assert.equal(parsed.detail, field);
  }
});

test("an optional field may be absent", () => {
  const optional = EVENT_WIRE_FIELDS.filter((name) => !EVENT_REQUIRED_FIELDS.includes(name));
  assert.deepEqual([...optional], ["partner_ref", "situations"]);
  for (const field of optional) {
    const body = makeEvent();
    delete body[field];
    assert.equal(parseEvent(encode(body)).ok, true, field);
  }
});

test("an unknown schema_version is refused as a VERSION, before the field checks", () => {
  // The order matters for the reader of a log: a future shape reported as a pile of unknown fields
  // sends someone looking for a bug on our side.
  const body = { ...makeEvent(), schema_version: "2", something_new: 1 };
  const parsed = parseEvent(encode(body));
  assert.equal(parsed.ok, false);
  assert.equal(parsed.problem, "unknown_schema_version");
});

test("the value kinds are checked, not just the presence of the keys", () => {
  const cases = [
    ["revision", 0],
    ["revision", 1.5],
    ["revision", "1"],
    ["subscription_version", 0],
    ["stop_order", -1],
    ["test", "false"],
    ["phase", "cancelled"],
    ["type", "journey.alert.exploded"],
    ["situations", [1]],
    ["event_id", ""],
    ["payload", { notification: { title: "x" } }],
  ];
  for (const [field, value] of cases) {
    const parsed = parseEvent(encode({ ...makeEvent(), [field]: value }));
    assert.equal(parsed.ok, false, `${field}=${JSON.stringify(value)}`);
    assert.equal(parsed.problem, "wrong_type", `${field}=${JSON.stringify(value)}`);
  }
});

test("a body that is not JSON, or not an object, is refused rather than thrown on", () => {
  assert.equal(parseEvent(Buffer.from("not json", "utf8")).problem, "not_json");
  assert.equal(parseEvent(Buffer.from("[]", "utf8")).problem, "not_object");
  assert.equal(parseEvent(Buffer.from("null", "utf8")).problem, "not_object");
});

test("the vendored contracts are byte-identical to the authored ones", () => {
  // The package ships its own copy so it is self-contained when vendored into the partner's repo.
  // That copy is a duplication, and this is the guard that keeps it from becoming a divergence --
  // the same arrangement `personvern.ts` already has in this repository.
  const authored = path.join(REPO_ROOT, "contracts", "notifications", "v1");
  if (!fs.existsSync(authored)) {
    // Running from a vendored copy with no repository around it. The CI self-grep in `ci.test.mjs`
    // is what stops that becoming a skip nobody ever un-skips: the step it asserts runs in a full
    // checkout, where this branch is not taken.
    console.log("skipping: no contracts/notifications/v1 -- this is a vendored copy");
    return;
  }
  for (const name of ["event.schema.json", "subscription.schema.json", "signing-vectors.json"]) {
    assert.equal(
      fs.readFileSync(path.join(PACKAGE_ROOT, "contracts", name), "utf8"),
      fs.readFileSync(path.join(authored, name), "utf8"),
      `${name} has drifted from contracts/notifications/v1/`,
    );
  }
});

test("the generated types are in step with the vendored schemas", () => {
  // `--check` re-renders in memory and exits non-zero on any difference, so an edit to the schema
  // that nobody regenerated fails here rather than at a partner's compiler.
  execFileSync(process.execPath, ["scripts/generate-contracts.mjs", "--check"], {
    cwd: PACKAGE_ROOT,
    stdio: "pipe",
  });
});
