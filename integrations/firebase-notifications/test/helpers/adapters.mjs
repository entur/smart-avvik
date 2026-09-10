// Shared fixtures: in-memory adapters, a signed request, and a valid event.
//
// The in-memory stores come from the client package rather than being re-implemented here, which is
// the point of that package existing: the Firebase adapter is one implementation of a contract, and
// a test double that did not implement the same contract would prove nothing about it.

import path from "node:path";
import { fileURLToPath } from "node:url";

import { createMemoryStores, receive, signatureHeader } from "@entur/kix-notifications-client";

export const PACKAGE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
export const REPO_ROOT = path.resolve(PACKAGE_ROOT, "..", "..");

/** The generation-2 test secret from the published signing vectors. Appears in no deployment. */
export const TEST_SECRET = "whsec_BkAJ0Xm6KdFJFqTHtWVZNVkRbPfjySIekSHQGgGwbX8=";

export const NOW = new Date("2026-09-09T05:10:00Z");

let counter = 0;

export function makeEvent(overrides = {}) {
  counter += 1;
  const alertId = overrides.alert_id ?? "sub_a:VYG:L1:2026-09-09:1";
  const revision = overrides.revision ?? 1;
  return {
    schema_version: "1",
    event_id: `evt_${counter}_${revision}`,
    type: "journey.alert.created",
    phase: "predicted",
    alert_id: alertId,
    revision,
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
    situations: ["NSB:SituationNumber:99"],
    payload: {
      notification: {
        title: "Tog L1 07:18 er innstilt",
        body: "Åsgårdstrand → Oslo S\nToget kl. 07:18 er innstilt.",
      },
    },
    ...overrides,
  };
}

export function encode(event) {
  return Buffer.from(JSON.stringify(event), "utf8");
}

/** A signed request in the shape the Functions framework hands a handler: headers plus `rawBody`. */
export function signedFunctionRequest({
  event = makeEvent(),
  secrets = [TEST_SECRET],
  webhookId = "dlv_test_0001",
  now = NOW,
  method = "POST",
} = {}) {
  const body = encode(event);
  const timestampSeconds = Math.floor(now.getTime() / 1000);
  return {
    event,
    request: {
      method,
      headers: {
        "content-type": "application/json",
        "content-length": String(body.byteLength),
        "webhook-id": webhookId,
        "webhook-timestamp": String(timestampSeconds),
        "webhook-signature": signatureHeader({
          secrets,
          webhookId,
          timestampSeconds,
          body,
        }),
      },
      rawBody: body,
      // The parsed body the framework also populates. Nothing in this package may read it: a
      // signature covers bytes, and re-serializing this object is the canonical webhook bug.
      body: JSON.parse(body.toString("utf8")),
    },
  };
}

/** A minimal express-shaped response that records what was sent. */
export function fakeResponse() {
  const sent = { status: null, body: null };
  return {
    sent,
    status(code) {
      sent.status = code;
      return this;
    },
    json(payload) {
      sent.body = payload;
      return this;
    },
  };
}

export function memoryAdapters(overrides = {}) {
  const stores = createMemoryStores();
  const dispatched = [];
  const adapters = {
    inbox: stores.inbox,
    ledger: stores.ledger,
    suppressions: stores.suppressions,
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
    ...overrides,
  };
  const harness = {
    stores,
    adapters,
    dispatched,
    async accepted(event = makeEvent()) {
      const { request } = signedFunctionRequest({ event });
      const result = await receive(
        { secrets: [TEST_SECRET], inbox: stores.inbox, now: () => NOW },
        { headers: request.headers, body: request.rawBody, declaredBytes: request.rawBody.byteLength },
      );
      if (result.status !== 200) {
        throw new Error(`fixture was not accepted: ${result.status} ${result.detail}`);
      }
      return { ...harness, record: await stores.inbox.get(event.event_id), event };
    },
  };
  return harness;
}
