// The wire envelope, pinned from this side, in both directions.
//
// The sender projects its stored event onto an allow-list of exactly the schema's top-level
// properties, and it does so because a deny-list would have shipped two extra fields that a
// conforming receiver refuses -- on every event, for every client. That is the sender's half. This
// is the receiver's half of the same pin, and the two halves are what make the claim symmetric:
//
//   * no field the schema does not declare is accepted (a regression on the sender fails HERE, in a
//     test, rather than in production at a client running a stricter verifier);
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
import {
  EVENT_SCHEMA,
  PACKAGE_ROOT,
  REPO_ROOT,
  encode,
  makeEvent,
  readVendoredContract,
} from "./helpers/events.mjs";

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
  // deny-list regression on the sender would actually ship: `client_id` and `destination_id` are
  // its own routing, and `expires_at` is a retention decision that is NOT validity -- on the wire it
  // would invite a receiver to read it as one.
  for (const field of ["client_id", "destination_id", "expires_at"]) {
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
  assert.deepEqual([...optional], ["external_ref", "situations"]);
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
  // The package ships its own copy so it is self-contained when vendored into the client's repo.
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
  for (const name of [
    "event.schema.json",
    "subscription.schema.json",
    "signing-vectors.json",
    "test-event-sample.json",
  ]) {
    assert.equal(
      fs.readFileSync(path.join(PACKAGE_ROOT, "contracts", name), "utf8"),
      fs.readFileSync(path.join(authored, name), "utf8"),
      `${name} has drifted from contracts/notifications/v1/`,
    );
  }
});

test("the generated types are in step with the vendored schemas", () => {
  // `--check` re-renders in memory and exits non-zero on any difference, so an edit to the schema
  // that nobody regenerated fails here rather than at a client's compiler.
  execFileSync(process.execPath, ["scripts/generate-contracts.mjs", "--check"], {
    cwd: PACKAGE_ROOT,
    stdio: "pipe",
  });
});

test("the sample test event parses as a conforming envelope, and carries the open payload's real keys", () => {
  // `contracts/test-event-sample.json` is dumped from `notification_api._synthetic_event` and is what the
  // Firebase receiver's end-to-end scenario now reads instead of a hand-written transcription. Two
  // things are pinned here, one layer below that.
  //
  // First, the envelope: `parseEvent` is the receiver's own allow-list, so a sample that failed it
  // would be a published example a conforming receiver refuses.
  //
  // Second -- and this is the check nothing in either language had -- the two OPEN sub-objects.
  // `payload.call` and `payload.probabilities` are bare `{"type": "object"}` in event.schema.json,
  // deliberately: the payload is the sender's to grow. The cost of that openness is that a
  // MISNAMED key inside them is schema-valid, which is how the receiver's fixture came to teach
  // `probabilities.cancel` and `call.aimed_departure` -- keys no producer has ever emitted -- for as
  // long as it did. Naming them here does not close the schema; it pins the sample, which is the
  // one artefact both languages now read.
  const sample = JSON.parse(readVendoredContract("test-event-sample.json"));
  const parsed = parseEvent(encode(sample.event));
  assert.equal(parsed.ok, true, JSON.stringify(parsed));
  assert.deepEqual(Object.keys(sample.event.payload.call), [
    "stop_place",
    "stop_order",
    "aimed_departure_time",
    "departs_tomorrow",
  ]);
  assert.deepEqual(Object.keys(sample.event.payload.probabilities), [
    "impact",
    "cancellation",
    "delay_ge10",
    "alert_threshold",
  ]);
  // A confirmed phase and a delay figure are not independent: every delay number meant "if it
  // runs", so a confirmed cancellation carries none. A sample regenerated from a producer that
  // broke that rule would be teaching the combination the producer's own docstring forbids.
  assert.equal(sample.event.phase, "confirmed");
  assert.equal(sample.event.payload.probabilities.impact, 1.0);
  assert.equal(sample.event.payload.probabilities.cancellation, 1.0);
  assert.equal(sample.event.payload.probabilities.delay_ge10, null);
  assert.equal(sample.event.payload.delay, null);
  assert.equal(sample.event.payload.effect, "cancelled");
  // Generated, not authored: the file says so, and says where from.
  assert.match(sample.how_to_regenerate.producer, /_synthetic_event/);
});

// --- The published OpenAPI document, from the side that consumes it -----------------------------
//
// `openapi.json` is not vendored into this package -- nothing here reads it at runtime, and the
// generator above renders types from the two JSON Schemas only. It is pinned from here anyway,
// because this package is where a client arrives and because both of the drifts below shipped: a
// document can promise a shape the server never sends without a single test in either language
// noticing.

function authoredOpenApi() {
  const authored = path.join(REPO_ROOT, "contracts", "notifications", "v1", "openapi.json");
  if (!fs.existsSync(authored)) {
    // A vendored copy with no repository around it. `ci.test.mjs` asserts the CI step that runs
    // this suite in a full checkout, so this branch is not a skip nobody ever un-skips.
    console.log("skipping: no contracts/notifications/v1/openapi.json -- this is a vendored copy");
    return null;
  }
  return JSON.parse(fs.readFileSync(authored, "utf8"));
}

test("the test-event request publishes the id form the route accepts, not the create-side one", () => {
  // The create body must carry the client's own reference and nothing else, so
  // `subscription.schema.json`'s definition caps at 128 characters and excludes `~`. `POST
  // /test-events` also accepts the opaque `subscription_id` an EVENT carries, which is
  // `<client_id>~<reference>` -- so while this property was a `$ref` into that stricter definition,
  // the published schema refused a value the API had itself just served, and a client validating
  // outbound requests against it reproduced the server bug in their own client.
  //
  // Pinned against the PATH parameter rather than against a literal: the document already publishes
  // the bound in one place, and the point of the check is that the two cannot drift apart.
  const openapi = authoredOpenApi();
  if (openapi === null) {
    return;
  }
  const property =
    openapi.components.schemas.TestEventRequest.properties.subscription_id;
  assert.equal(property.$ref, undefined, "the create-side definition is the wrong constraint here");
  const pathParameter = openapi.paths["/subscriptions/{subscription_id}"].parameters.find(
    (parameter) => parameter.name === "subscription_id",
  );
  assert.equal(property.maxLength, pathParameter.schema.maxLength);
  assert.equal(property.minLength, 1);
  assert.ok(property.pattern.includes("~"), "the namespaced form must validate");
  assert.ok(new RegExp(property.pattern).test("client-a~morgentur"));
  // And the create side stays strict: widening it to reconcile the two would be the same mistake
  // pointing the other way. Read through the VENDORED copy, which needs no existence guard and is
  // the file the byte-identity test above pins -- a second unguarded read of the authored directory
  // would put back exactly the assumption `authoredOpenApi` exists to avoid.
  const createSide = JSON.parse(
    readVendoredContract("subscription.schema.json"),
  ).properties.subscription_id;
  assert.ok(!new RegExp(createSide.pattern).test("client-a~morgentur"));
  assert.equal(createSide.maxLength, 128);
});

test("every documented 422 admits the framework's array `detail`, and constrained routes document one", () => {
  // `Error` (`detail: string`) is what a handler in this application returns. A request refused
  // before any handler runs -- an out-of-range `limit`, an unknown body property, a `pattern`
  // violation, an over-long path segment -- is refused by FastAPI, whose `detail` is an ARRAY of
  // objects. The document declared only the string shape until 2026-09-09, so a generated client
  // that read `detail` as a string threw on exactly the errors an integrator hits first.
  //
  // It is documented rather than reshaped: `RequestValidationError` handlers are registered per
  // APPLICATION, not per router, and this API shares its process with `/varsel-api`,
  // `/historikk-api` and `/meldinger-api`.
  const openapi = authoredOpenApi();
  if (openapi === null) {
    return;
  }
  const detail = openapi.components.schemas.ValidationError.properties.detail;
  assert.equal(detail.type, "array");
  assert.deepEqual(detail.items.required, ["type", "loc", "msg", "input"]);
  // `loc` carries an integer index into an array property (`["body", "days", 1]`), so a
  // string-only item type would be false.
  assert.deepEqual(detail.items.properties.loc.items.type, ["string", "integer"]);
  // The item is deliberately NOT `additionalProperties: false`: the key set is Pydantic's, and
  // closing a set we do not own would be a second false promise.
  assert.equal(detail.items.additionalProperties, undefined);

  const admitsArray = (response) => {
    const schema = response.content["application/json"].schema;
    const refs = (schema.oneOf ?? [schema]).map((entry) => entry.$ref);
    return refs.includes("#/components/schemas/ValidationError");
  };
  for (const [name, response] of Object.entries(openapi.components.responses)) {
    if (name === "Unprocessable" || name === "ValidationFailed") {
      assert.ok(admitsArray(response), `${name} must admit the array shape`);
    }
  }

  // Every operation that constrains a parameter or a body can be refused this way, so every one of
  // them documents a 422. `/destinations/current` constrains nothing -- no body, no query, no path
  // parameter -- and documenting one there would be the opposite error. The public
  // `GET /journey-alerts` (2026-09-10) constrains every one of its query parameters -- the two stop
  // refs, the optional period, the repeated `days`, `wait` and `limit` -- so it belongs here: a
  // client that reads its `detail` as a string throws on the first mistyped favourite.
  const constrained = new Set([
    "/journey-alerts|get",
    "/journey-status|get",
    "/subscriptions|post",
    "/subscriptions|get",
    "/subscriptions/{subscription_id}|get",
    "/subscriptions/{subscription_id}|delete",
    "/test-events|post",
  ]);
  const documented = new Set();
  for (const [route, operations] of Object.entries(openapi.paths)) {
    for (const [method, operation] of Object.entries(operations)) {
      if (!["get", "post", "put", "patch", "delete"].includes(method)) {
        continue;
      }
      if (operation.responses["422"]) {
        documented.add(`${route}|${method}`);
      }
    }
  }
  assert.deepEqual([...documented].sort(), [...constrained].sort());
});
