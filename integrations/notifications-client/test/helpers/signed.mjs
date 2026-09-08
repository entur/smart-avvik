// Build a signed request the way the sender does, so the suites exercise the real verifier.

import { signatureHeader } from "../../src/verify.ts";
import { TEST_SECRET, encode, makeEvent } from "./events.mjs";

/** A fixed instant inside every fixture's validity window. */
export const NOW = new Date("2026-09-09T05:10:00Z");

export function signedRequest({
  event = makeEvent(),
  secrets = [TEST_SECRET],
  webhookId = "dlv_test_0001",
  now = NOW,
  body = null,
} = {}) {
  const bytes = body ?? encode(event);
  const timestampSeconds = Math.floor(now.getTime() / 1000);
  return {
    event,
    body: bytes,
    headers: {
      "content-type": "application/json",
      "webhook-id": webhookId,
      "webhook-timestamp": String(timestampSeconds),
      "webhook-signature": signatureHeader({
        secrets,
        webhookId,
        timestampSeconds,
        body: bytes,
      }),
    },
    declaredBytes: bytes.byteLength,
  };
}
