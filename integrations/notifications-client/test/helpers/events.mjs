// Fixtures shared by the suites in this package.
//
// `makeEvent` builds a body that is VALID against the published envelope -- every required property,
// no unknown one -- from a handful of overrides. Building one inline per test is how a suite ends up
// asserting against a shape the schema would refuse, which is the one thing these tests exist to
// notice.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const PACKAGE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
export const REPO_ROOT = path.resolve(PACKAGE_ROOT, "..", "..");

export function readVendoredContract(name) {
  return fs.readFileSync(path.join(PACKAGE_ROOT, "contracts", name), "utf8");
}

export const EVENT_SCHEMA = JSON.parse(readVendoredContract("event.schema.json"));
export const SIGNING_VECTORS = JSON.parse(readVendoredContract("signing-vectors.json"));

/** The generation-2 test secret from the vectors file. Appears in no deployment. */
export const TEST_SECRET = "whsec_BkAJ0Xm6KdFJFqTHtWVZNVkRbPfjySIekSHQGgGwbX8=";
/** The generation-3 test secret, for the rotation cases. */
export const TEST_SECRET_NEXT = "whsec_RPljTBg368DvjbJ7Jj7O33x5V+/LL+PPThk19+WTFMg=";
/** Another destination's secret: it must verify nothing signed for the first. */
export const OTHER_DESTINATION_SECRET = "whsec_8Z8dFhFl9JvobGMFBq8xXJtCRY6HCJLMQ42ZbXZ62DU=";

let counter = 0;

export function makeEvent(overrides = {}) {
  counter += 1;
  const alertId = overrides.alert_id ?? "sub_a:VYG:L1:2026-09-09:1";
  const revision = overrides.revision ?? 1;
  const event = {
    schema_version: "1",
    event_id: `evt_${alertId}_${revision}_${counter}`,
    type: "journey.alert.created",
    phase: "predicted",
    alert_id: alertId,
    revision,
    occurred_at: "2026-09-09T05:00:00+00:00",
    valid_until: "2026-09-09T06:00:00+00:00",
    subscription_id: "sub_a",
    subscription_version: 1,
    partner_ref: "commuter-4711",
    test: false,
    operator: "VYG",
    line: "VYG:Line:L1",
    journey_ref: "VYG:DatedServiceJourney:1234",
    call_date: "2026-09-09",
    stop_place: "NSR:StopPlace:337",
    stop_order: 3,
    situations: ["NSB:SituationNumber:99"],
    payload: {
      notification: {
        title: "Tog L1 07:18 er innstilt",
        body: "Åsgårdstrand → Oslo S\nToget kl. 07:18 er innstilt.",
      },
    },
    ...overrides,
  };
  // A deterministic id unless the test named one, so two events about one subject at one revision
  // collide exactly as the sender's own deterministic minting would.
  if (!overrides.event_id) {
    event.event_id = `evt_${hash(`${event.alert_id}|${event.revision}`)}`;
  }
  return event;
}

function hash(text) {
  let value = 0;
  for (const character of text) {
    value = (value * 31 + character.codePointAt(0)) >>> 0;
  }
  return value.toString(16).padStart(8, "0");
}

/** The exact bytes an event is signed and sent as. Serialized ONCE; never re-serialized. */
export function encode(event) {
  return Buffer.from(JSON.stringify(event), "utf8");
}
