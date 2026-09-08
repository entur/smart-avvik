// The cross-language signing vectors, consumed exactly as the vectors file prescribes.
//
// The file is produced by the sender's own `webhook_signing.py` and re-derived by its test suite, so
// it cannot drift from the code that signs. What these checks establish is that THIS package agrees
// with it byte for byte -- which is the only thing standing between "our verifier is fine" and a
// partner whose receiver rejects every event we send.
//
// Two things the file insists on, and both are here because the obvious version is wrong:
//
//   * The MAC is checked through a **clock-free** comparison -- `sign()` at the vector's own
//     timestamp, compared with the recorded entry -- because `Webhook.verify()` reads `Date.now()`
//     and takes no injectable clock. Every vector's timestamp is fixed in 2026, so running them
//     through `verify()` would prove only that a stale timestamp is refused, never that the MAC is
//     right. The `stale-timestamp` vector is the one that IS fed to `verify()`, where it must throw.
//   * `norwegian-body` disagrees about its own length between UTF-8 (400) and UTF-16 (386). That is
//     not trivia: it is the live failure mode when a receiver measures or re-encodes the string
//     instead of hashing the UTF-8 bytes, and `.length` in JavaScript is the UTF-16 count.

import { test } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";

import { Webhook } from "standardwebhooks";

import { signBody, signatureHeader, verifySignature, SIGNATURE_TOLERANCE_SECONDS } from "../src/verify.ts";
import { SIGNING_VECTORS } from "./helpers/events.mjs";

const VECTORS = SIGNING_VECTORS.vectors;

test("the vectors file is the shape this suite assumes", () => {
  assert.equal(SIGNING_VECTORS.version, 1);
  assert.equal(SIGNING_VECTORS.scheme.algorithm, "HMAC-SHA256");
  assert.equal(SIGNING_VECTORS.scheme.timestamp_tolerance_seconds, SIGNATURE_TOLERANCE_SECONDS);
  assert.equal(VECTORS.length, 7, "a vector was added or removed; read it before widening this");
});

test("every vector's body is the exact bytes its sha256 names, and the lengths are UTF-8", () => {
  for (const vector of VECTORS) {
    const bytes = Buffer.from(vector.body, "utf8");
    assert.equal(
      crypto.createHash("sha256").update(bytes).digest("hex"),
      vector.body_sha256_hex,
      `${vector.name}: body bytes`,
    );
    assert.equal(bytes.byteLength, vector.body_utf8_byte_length, `${vector.name}: utf8 length`);
    assert.equal(
      vector.body.length,
      vector.body_utf16_code_unit_length,
      `${vector.name}: utf16 length`,
    );
  }
});

test("the norwegian body's two lengths really do disagree, which is what it is for", () => {
  const vector = VECTORS.find((entry) => entry.name === "norwegian-body");
  assert.notEqual(vector.body_utf8_byte_length, vector.body_utf16_code_unit_length);
  assert.equal(vector.body_utf8_byte_length, 400);
  assert.equal(vector.body_utf16_code_unit_length, 386);
});

test("signing each secret over the vector's bytes reproduces its recorded entry", () => {
  // The clock-free check the vectors file prescribes. It compares byte for byte and consults no
  // clock, so it is the only check that can prove the MAC on a fixed 2026 timestamp.
  for (const vector of VECTORS) {
    for (const entry of vector.secrets) {
      const produced = signBody({
        secret: entry.secret,
        webhookId: vector.webhook_id,
        timestampSeconds: Number(vector.webhook_timestamp),
        body: Buffer.from(vector.body, "utf8"),
      });
      if (vector.mac_is_correct === false) {
        // `tampered-body`: the recorded entry is a valid signature over a DIFFERENT body, so what
        // we produce over this body must not match it.
        assert.notEqual(produced, vector.signature_header, `${vector.name}: ${entry.generation}`);
      }
      assert.equal(produced, entry.signature, `${vector.name}: generation ${entry.generation}`);
    }
  }
});

test("the combined header is the verifying entries joined by a single space, in order", () => {
  for (const vector of VECTORS) {
    const verifying = vector.secrets.filter((entry) => entry.verifies);
    if (verifying.length === 0) {
      continue;
    }
    assert.equal(
      signatureHeader({
        secrets: verifying.map((entry) => entry.secret),
        webhookId: vector.webhook_id,
        timestampSeconds: Number(vector.webhook_timestamp),
        body: Buffer.from(vector.body, "utf8"),
      }),
      vector.signature_header,
      vector.name,
    );
  }
});

test("verifySignature honours every vector's `verifies` flag at its own `verify_at`", () => {
  for (const vector of VECTORS) {
    for (const entry of vector.secrets) {
      const outcome = verifySignature({
        secrets: [entry.secret],
        headers: {
          "webhook-id": vector.webhook_id,
          "webhook-timestamp": vector.webhook_timestamp,
          "webhook-signature": vector.signature_header,
        },
        body: Buffer.from(vector.body, "utf8"),
        nowSeconds: Number(vector.verify_at_unix_seconds),
      });
      assert.equal(
        outcome.ok,
        entry.verifies,
        `${vector.name}: generation ${entry.generation} expected verifies=${entry.verifies}`,
      );
    }
  }
});

test("a revoked generation is revoked by being absent from the secret list, nothing else", () => {
  const rotated = VECTORS.find((entry) => entry.name === "rotated-pair");
  const live = rotated.secrets.filter((entry) => entry.verifies).map((entry) => entry.secret);
  const revoked = rotated.secrets.find((entry) => !entry.verifies).secret;
  const headers = {
    "webhook-id": rotated.webhook_id,
    "webhook-timestamp": rotated.webhook_timestamp,
    "webhook-signature": rotated.signature_header,
  };
  const body = Buffer.from(rotated.body, "utf8");
  const nowSeconds = Number(rotated.verify_at_unix_seconds);

  // Both live generations accept the header, in either order: entry order carries no meaning.
  assert.equal(verifySignature({ secrets: live, headers, body, nowSeconds }).ok, true);
  assert.equal(verifySignature({ secrets: [...live].reverse(), headers, body, nowSeconds }).ok, true);
  // The revoked generation on its own does not.
  assert.equal(verifySignature({ secrets: [revoked], headers, body, nowSeconds }).ok, false);
});

test("the stale vector fails for its TIMESTAMP and not for its MAC", () => {
  const stale = VECTORS.find((entry) => entry.name === "stale-timestamp");
  const secret = stale.secrets[0].secret;
  const headers = {
    "webhook-id": stale.webhook_id,
    "webhook-timestamp": stale.webhook_timestamp,
    "webhook-signature": stale.signature_header,
  };
  const body = Buffer.from(stale.body, "utf8");

  // At `verify_at`, 301 s past its own timestamp, it is refused -- and the reason names the window.
  const late = verifySignature({
    secrets: [secret],
    headers,
    body,
    nowSeconds: Number(stale.verify_at_unix_seconds),
  });
  assert.deepEqual(late, { ok: false, failure: "timestamp_too_old" });

  // At its own timestamp the MAC verifies. That is what separates a replay refusal from a wrong key,
  // and it is why the vector records `mac_is_correct: true`.
  assert.equal(stale.mac_is_correct, true);
  assert.equal(
    verifySignature({
      secrets: [secret],
      headers,
      body,
      nowSeconds: Number(stale.webhook_timestamp),
    }).ok,
    true,
  );
});

test("the package's own wall-clock verify() rejects the stale vector, as the file says it will", () => {
  // The one place `Webhook.verify()` is used on a vector: it reads Date.now(), so this vector is
  // stale forever and the throw is the assertion.
  const stale = VECTORS.find((entry) => entry.name === "stale-timestamp");
  assert.throws(
    () =>
      new Webhook(stale.secrets[0].secret).verify(stale.body, {
        "webhook-id": stale.webhook_id,
        "webhook-timestamp": stale.webhook_timestamp,
        "webhook-signature": stale.signature_header,
      }),
    /timestamp too old/i,
  );
});

test("the package's verify() accepts a message this suite signs at the wall clock", () => {
  // The other half of the file's instruction: `verify()` is usable on messages the test signs
  // itself, which is what proves this package and the reference implementation agree end to end.
  const nowSeconds = Math.floor(Date.now() / 1000);
  const body = Buffer.from('{"schema_version":"1"}', "utf8");
  const header = signatureHeader({
    secrets: [VECTORS[0].secrets[0].secret],
    webhookId: "dlv_wallclock",
    timestampSeconds: nowSeconds,
    body,
  });
  assert.doesNotThrow(() =>
    new Webhook(VECTORS[0].secrets[0].secret).verify(body, {
      "webhook-id": "dlv_wallclock",
      "webhook-timestamp": String(nowSeconds),
      "webhook-signature": header,
    }),
  );
});

test("a retry reuses the delivery id and re-signs under a new timestamp", () => {
  // `retry-same-id-new-timestamp` is the second attempt at `plain`. Same id, same body, different
  // timestamp -- and therefore a different signature. A sender that reused the first attempt's
  // timestamp would land outside the 300 s window and a correct receiver would drop its own retry.
  const first = VECTORS.find((entry) => entry.name === "plain");
  const retry = VECTORS.find((entry) => entry.name === "retry-same-id-new-timestamp");
  assert.equal(retry.same_webhook_id_as, "plain");
  assert.equal(retry.webhook_id, first.webhook_id);
  assert.equal(retry.body, first.body);
  assert.notEqual(retry.webhook_timestamp, first.webhook_timestamp);
  assert.notEqual(retry.signature_header, first.signature_header);
  assert.ok(
    Number(retry.webhook_timestamp) - Number(first.webhook_timestamp) > SIGNATURE_TOLERANCE_SECONDS - 5,
    "the retry is far enough out that reusing the first timestamp would be refused",
  );
});

test("signing a Uint8Array that is not a Buffer does not throw", () => {
  // `standardwebhooks`' sign() tests `payload.constructor.name === "Buffer"` and throws otherwise,
  // so a plain Uint8Array -- what several HTTP stacks hand a handler -- would 500 every event.
  const vector = VECTORS[0];
  const bytes = new Uint8Array(Buffer.from(vector.body, "utf8"));
  assert.equal(Buffer.isBuffer(bytes), false);
  assert.equal(
    signBody({
      secret: vector.secrets[0].secret,
      webhookId: vector.webhook_id,
      timestampSeconds: Number(vector.webhook_timestamp),
      body: bytes,
    }),
    vector.secrets[0].signature,
  );
});

test("a body re-serialized from a parsed object does not verify", () => {
  // The canonical webhook bug, and the reason `parseEvent` takes bytes: JSON.stringify of a parsed
  // document is a different byte string -- key order, spacing, escaping -- and the signature covers
  // bytes.
  const vector = VECTORS.find((entry) => entry.name === "norwegian-body");
  const reserialized = Buffer.from(JSON.stringify(JSON.parse(vector.body), null, 1), "utf8");
  assert.notDeepEqual(reserialized, Buffer.from(vector.body, "utf8"));
  assert.equal(
    verifySignature({
      secrets: [vector.secrets[0].secret],
      headers: {
        "webhook-id": vector.webhook_id,
        "webhook-timestamp": vector.webhook_timestamp,
        "webhook-signature": vector.signature_header,
      },
      body: reserialized,
      nowSeconds: Number(vector.verify_at_unix_seconds),
    }).ok,
    false,
  );
});

test("signing over the UTF-16 view of the norwegian body does not reproduce the signature", () => {
  // The concrete form of the length disagreement: latin1 gives one byte per UTF-16 code unit, which
  // is 386 bytes rather than 400. A receiver that read the body as anything but UTF-8 lands here.
  const vector = VECTORS.find((entry) => entry.name === "norwegian-body");
  const misencoded = Buffer.from(vector.body, "latin1");
  assert.equal(misencoded.byteLength, vector.body_utf16_code_unit_length);
  assert.notEqual(
    signBody({
      secret: vector.secrets[0].secret,
      webhookId: vector.webhook_id,
      timestampSeconds: Number(vector.webhook_timestamp),
      body: misencoded,
    }),
    vector.secrets[0].signature,
  );
});
