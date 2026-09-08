// The Firestore collection names, in one place.
//
// Five things have to agree on these: the inbox, the ledger, the suppression log, the recovery sweep
// and `firestore.rules`. Four of them are TypeScript and one is not, so a constant cannot make the
// rules file follow -- what it does is make the disagreement checkable, and the emulator suite
// asserts every name here is denied to a client SDK. A collection added in code and forgotten in the
// rules is readable by any signed-in user of the partner's own app, and there is nothing in a
// TypeScript build that would notice.

export const COLLECTIONS = {
  /** One record per accepted delivery, keyed by `event_id`. Written before the 2xx. */
  inbox: "notification_inbox",
  /** The standing revision and phase per `alert_id`. The ledger's state. */
  alerts: "notification_alerts",
  /** One marker per decided `event_id`, carrying the decision and whether it finished. */
  decisions: "notification_decisions",
  /** Every drop, with its rule and reason, so a missing notification has an explanation. */
  suppressions: "notification_suppressions",
} as const;

export type CollectionName = (typeof COLLECTIONS)[keyof typeof COLLECTIONS];

/** Every collection this package writes. The rules must deny a client SDK on all of them. */
export const ALL_COLLECTIONS: readonly CollectionName[] = Object.values(COLLECTIONS);

/**
 * A Firestore document id built from a value this package did not choose.
 *
 * A document id may not contain `/`, may not be `.` or `..`, and is capped at 1500 bytes. Two of the
 * three ids here come off the wire: `alert_id` is opaque -- the sender composes it from a
 * subscription and a train and publishes no format at all -- and `event_id`, while today a 40-character
 * hex digest on the sending side (`partner_store.event_id_for`, SHA-256 truncated), is documented
 * only as "immutable and deterministic over (alert_id, revision)". A receiver that hard-codes today's
 * shape is a receiver that breaks silently the day it changes, and a `/` slipping into a document id
 * does not error: it creates a SUBCOLLECTION, so the write appears to succeed and the read finds
 * nothing.
 *
 * Encoding rather than hashing, because an operator asking "what do we hold for this subscription?"
 * should be able to read the answer in the console; a hash would make every such question a join. A
 * 40-hex id encodes to itself, so nothing about the ordinary case changes.
 */
export function encodeDocumentId(value: string): string {
  const encoded = encodeURIComponent(value).replace(/\./g, "%2E");
  if (Buffer.byteLength(encoded, "utf8") <= 1500) {
    return encoded;
  }
  // Nothing the sender mints comes close to this, so the branch is a guard rather than a path.
  // Truncating alone would collide; the suffix is what keeps two long values distinct.
  return `${encoded.slice(0, 1400)}__${fingerprint(value)}`;
}

function fingerprint(text: string): string {
  let value = 0;
  for (const character of text) {
    value = (Math.imul(value, 31) + character.codePointAt(0)!) >>> 0;
  }
  return value.toString(16).padStart(8, "0");
}
