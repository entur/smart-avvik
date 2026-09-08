// Standard Webhooks verification, over BYTES, with an injectable clock.
//
// The HMAC itself comes from the `standardwebhooks` npm package: it is the reference implementation
// of the scheme the sender signs under, and hand-rolling the MAC would put a second definition of
// the signed content in the world. What this module adds is the two things that package cannot do
// for a receiver that has to be TESTED:
//
//  1. **An injectable clock.** `Webhook.verify()` reads `Date.now()` and takes no clock, so it
//     cannot verify a fixed vector: the sender's cross-language vectors carry timestamps in 2026
//     and they will be stale forever. Verifying them through the package's own `verify()` would
//     therefore prove only that a stale timestamp is refused -- never that the MAC is right. The
//     vectors file says this in `how_to_consume.typescript_standardwebhooks` and prescribes the
//     clock-free comparison this module performs.
//  2. **A reason, not an exception.** `verify()` throws one error type for a missing header, a stale
//     timestamp and a wrong key alike. A receiver has to tell those apart -- a stale timestamp is a
//     replay refusal, a wrong key is a credential problem, and a partner debugging an integration
//     needs to know which -- so the outcome is a value.
//
// Signing is over the exact bytes received. Never over a re-serialized object, and never over
// anything measured in JavaScript string units: the `norwegian-body` vector exists to catch that
// and disagrees about its own length between UTF-8 (400) and UTF-16 (386).

import { Webhook } from "standardwebhooks";
import { timingSafeEqual } from "node:crypto";

/** The Standard Webhooks header names, lower-cased as HTTP headers reach a Node handler. */
export const HEADER_ID = "webhook-id";
export const HEADER_TIMESTAMP = "webhook-timestamp";
export const HEADER_SIGNATURE = "webhook-signature";

/**
 * The replay window, in seconds.
 *
 * 300 s, matching the scheme and matching `standardwebhooks`' own hard-coded tolerance. It is what
 * makes the sender regenerate the timestamp on every attempt: a retry that reused the first
 * attempt's timestamp would land outside this window, and a CORRECT receiver would drop the
 * sender's own retry as an attack.
 */
export const SIGNATURE_TOLERANCE_SECONDS = 300;

export type VerifyFailure =
  | "missing_headers"
  | "malformed_timestamp"
  | "timestamp_too_old"
  | "timestamp_too_new"
  | "no_matching_signature"
  | "no_secrets";

export type VerifyOutcome =
  | { ok: true; webhookId: string; timestampSeconds: number; secretIndex: number }
  | { ok: false; failure: VerifyFailure };

export interface VerifyRequest {
  /**
   * Every secret that may sign for this destination, active generation first.
   *
   * Plural, and that is the rotation contract rather than defensiveness: the sender emits one
   * `v1,<sig>` entry per live generation and each generation is revocable alone, so a receiver
   * holding both accepts either and a rotation needs no flag day. A revoked generation is removed
   * from this array; that is what revocation MEANS here.
   */
  secrets: readonly string[];
  headers: Readonly<Record<string, string | string[] | undefined>>;
  /** The exact bytes received. */
  body: Uint8Array | string;
  /** Seconds since the epoch. Injected so a fixed vector can be verified. */
  nowSeconds: number;
}

/**
 * Verify a signed request.
 *
 * Order is deliberate: headers, then the timestamp, then the MAC. Checking the MAC first would spend
 * an HMAC per secret on a request that is refused anyway, and would report a stale replay as a
 * signature failure.
 */
export function verifySignature(request: VerifyRequest): VerifyOutcome {
  const webhookId = header(request.headers, HEADER_ID);
  const timestamp = header(request.headers, HEADER_TIMESTAMP);
  const signature = header(request.headers, HEADER_SIGNATURE);
  if (!webhookId || !timestamp || !signature) {
    return { ok: false, failure: "missing_headers" };
  }
  // `parseInt` would accept "123abc" and a leading "+", and `Number("")` is 0. The scheme says
  // whole seconds as a decimal string, so require exactly that.
  if (!/^-?[0-9]+$/.test(timestamp)) {
    return { ok: false, failure: "malformed_timestamp" };
  }
  const timestampSeconds = Number(timestamp);
  if (!Number.isSafeInteger(timestampSeconds)) {
    return { ok: false, failure: "malformed_timestamp" };
  }
  if (request.nowSeconds - timestampSeconds > SIGNATURE_TOLERANCE_SECONDS) {
    return { ok: false, failure: "timestamp_too_old" };
  }
  if (timestampSeconds - request.nowSeconds > SIGNATURE_TOLERANCE_SECONDS) {
    return { ok: false, failure: "timestamp_too_new" };
  }
  if (request.secrets.length === 0) {
    return { ok: false, failure: "no_secrets" };
  }
  const offered = signature.split(" ").filter((entry) => entry.length > 0);
  for (let index = 0; index < request.secrets.length; index += 1) {
    const expected = signBody({
      secret: request.secrets[index] as string,
      webhookId,
      timestampSeconds,
      body: request.body,
    });
    for (const entry of offered) {
      if (equalEntries(entry, expected)) {
        return { ok: true, webhookId, timestampSeconds, secretIndex: index };
      }
    }
  }
  return { ok: false, failure: "no_matching_signature" };
}

/**
 * One `v1,<base64>` entry over the given bytes.
 *
 * `standardwebhooks`' `sign()` accepts `string | Buffer` and rejects anything else by testing
 * `payload.constructor.name === "Buffer"` -- so a plain `Uint8Array`, which is what several HTTP
 * stacks hand you, THROWS. Normalising here is not defensive tidiness; it is the difference between
 * a working receiver and a 500 on every event for a caller who did nothing wrong.
 */
export function signBody(input: {
  secret: string;
  webhookId: string;
  timestampSeconds: number;
  body: Uint8Array | string;
}): string {
  const payload = typeof input.body === "string" ? input.body : asBuffer(input.body);
  return new Webhook(input.secret).sign(
    input.webhookId,
    new Date(input.timestampSeconds * 1000),
    payload,
  );
}

/**
 * The full `webhook-signature` header for a set of secrets: one entry per generation, joined by a
 * single space, in the order given. Used by the test suites to sign their own messages, and by a
 * partner reproducing a header by hand.
 */
export function signatureHeader(input: {
  secrets: readonly string[];
  webhookId: string;
  timestampSeconds: number;
  body: Uint8Array | string;
}): string {
  return input.secrets
    .map((secret) =>
      signBody({
        secret,
        webhookId: input.webhookId,
        timestampSeconds: input.timestampSeconds,
        body: input.body,
      }),
    )
    .join(" ");
}

function asBuffer(bytes: Uint8Array): Buffer {
  return Buffer.isBuffer(bytes)
    ? bytes
    : Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength);
}

/**
 * Constant-time comparison of two `v1,<base64>` entries.
 *
 * `timingSafeEqual` throws on a length mismatch, which would itself leak the length and, worse,
 * crash the handler on a malformed entry -- so the lengths are compared first and a mismatch is
 * simply "no match". A tag that is not `v1` is not a match either: the scheme reserves the prefix,
 * and treating an unknown tag as a candidate would let a future algorithm's signature be compared
 * against an HMAC-SHA256 one.
 */
function equalEntries(offered: string, expected: string): boolean {
  const separator = offered.indexOf(",");
  if (separator < 0 || offered.slice(0, separator) !== "v1") {
    return false;
  }
  const left = Buffer.from(offered, "utf8");
  const right = Buffer.from(expected, "utf8");
  if (left.length !== right.length) {
    return false;
  }
  return timingSafeEqual(left, right);
}

function header(
  headers: Readonly<Record<string, string | string[] | undefined>>,
  name: string,
): string | undefined {
  // Node lower-cases inbound header names, but a caller assembling this object by hand may not, and
  // a case-sensitive lookup would fail a request that is perfectly well formed.
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() !== name) {
      continue;
    }
    if (Array.isArray(value)) {
      // A repeated signature header is ambiguous: which one was meant? Refuse rather than pick.
      return value.length === 1 ? value[0] : undefined;
    }
    return value;
  }
  return undefined;
}
