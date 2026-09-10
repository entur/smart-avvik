// The Firestore inbox: one accepted delivery, one document, keyed by `event_id`.
//
// The whole dedup guarantee is `create()` rather than `set()`. `create()` fails when the document
// exists, so a redelivery -- concurrent, or after a restart, both normal under at-least-once -- costs
// one failed write and produces no second record, no second trigger and no second notification.
// `set()` would overwrite happily, fire the update trigger, and look correct in every log.
//
// The key is `event_id` and NOT the `webhook-id` delivery id. Both are stable across the sender's
// attempts, but only `event_id` is deterministic over (alert_id, revision): a decision the sender
// re-derives mints the same event id under a NEW delivery id, and keying on the delivery would let
// that through as a second notification about one train.

import { FieldValue } from "firebase-admin/firestore";
import type { Firestore } from "firebase-admin/firestore";

import type { InboxOutcome, InboxRecord, InboxStore, SuppressionLog, SuppressionRecord } from "@entur/kix-notifications-client";

import { COLLECTIONS, encodeDocumentId } from "./collections.ts";

/**
 * Every composite-index-requiring query this module runs, declared once.
 *
 * The queries are BUILT from these specs rather than described by them, so a spec cannot drift from
 * the query it stands for -- which is the only thing that makes a static index check worth running.
 * `test/ci.test.mjs` compares this list against `firestore.indexes.json` in both directions.
 *
 * A static check is the only kind available. The Firestore emulator tracks NO composite indexes: a
 * query missing its production index answers perfectly happily in every test here and throws
 * `FAILED_PRECONDITION` on the client's first deployed sweep. That is the same trap the sending
 * side closes with `test_every_query_in_the_module_has_a_checked_in_index`, and this is the same
 * shape of guard.
 */
export const QUERY_SPECS = [
  {
    name: "pending inbox records for the recovery sweep",
    collection: COLLECTIONS.inbox,
    // An equality on one field plus an inequality on another, ordered by the second: a composite
    // index in production. A single-field query would need none -- Firestore indexes those
    // automatically -- which is why the list carries the fields rather than just the collection.
    fields: [
      { fieldPath: "status", order: "ASCENDING" },
      { fieldPath: "receivedAt", order: "ASCENDING" },
    ],
  },
] as const;

const PENDING_SPEC = QUERY_SPECS[0];

/** gRPC ALREADY_EXISTS. What `create()` rejects with when the document is already there. */
const ALREADY_EXISTS = 6;

function isAlreadyExists(error: unknown): boolean {
  const code = (error as { code?: unknown }).code;
  if (code === ALREADY_EXISTS) {
    return true;
  }
  // The emulator and some client versions surface it as a string status instead of the numeric
  // gRPC code. Matching both is defensive on purpose: reading a real ALREADY_EXISTS as an unknown
  // error would turn every duplicate delivery into a 503 and an endless sender retry.
  return typeof code === "string" && code.toUpperCase() === "ALREADY_EXISTS";
}

export function createFirestoreInbox(db: Firestore): InboxStore {
  const collection = db.collection(COLLECTIONS.inbox);

  return {
    async put(record: InboxRecord): Promise<"created" | "duplicate"> {
      try {
        await collection.doc(encodeDocumentId(record.eventId)).create({
          ...record,
          // Stored as a field as well as the document id so a collection-group query and an
          // operator reading one document both see it without parsing the path.
          eventId: record.eventId,
        });
        return "created";
      } catch (error) {
        if (isAlreadyExists(error)) {
          return "duplicate";
        }
        throw error;
      }
    },

    async get(eventId: string): Promise<InboxRecord | null> {
      const snapshot = await collection.doc(encodeDocumentId(eventId)).get();
      return snapshot.exists ? (snapshot.data() as InboxRecord) : null;
    },

    async settle(eventId: string, outcome: InboxOutcome, detail?: string): Promise<void> {
      // A transaction, and only ever off `pending`. A terminal status is the record of what happened
      // to this event, and a re-entry -- an Eventarc redelivery, a sweep racing the trigger -- would
      // otherwise rewrite `dispatched` as `suppressed` or `expired`. That costs no duplicate
      // notification, which is exactly why it is easy to miss: what it destroys is the only record
      // that the notification was sent at all. `process()` guards the common case with a freshness
      // read; this closes the race.
      //
      // One transaction per event, on a write that happens once per event, is a price worth paying.
      const reference = collection.doc(encodeDocumentId(eventId));
      await db.runTransaction(async (transaction) => {
        const snapshot = await transaction.get(reference);
        if (!snapshot.exists || (snapshot.data() as InboxRecord).status !== "pending") {
          return;
        }
        transaction.set(
          reference,
          {
            status: outcome,
            settledAt: new Date().toISOString(),
            ...(detail === undefined ? {} : { settledDetail: detail }),
          },
          { merge: true },
        );
      });
    },

    async noteAttempt(eventId: string): Promise<void> {
      // A blind increment rather than a read-modify-write: the count is diagnostic, so paying a
      // transaction for it would put a round trip in front of every attempt to make a number that
      // nothing branches on exact.
      await collection.doc(encodeDocumentId(eventId)).set(
        { attempts: FieldValue.increment(1), lastAttemptAt: new Date().toISOString() },
        { merge: true },
      );
    },

    async pending({ receivedBefore, limit }): Promise<InboxRecord[]> {
      // Built from `QUERY_SPECS` above, field names and all, so the declaration a test checks
      // against `firestore.indexes.json` is the declaration this query is made of.
      const [equality, inequality] = PENDING_SPEC.fields;
      const snapshot = await collection
        .where(equality.fieldPath, "==", "pending")
        .where(inequality.fieldPath, "<=", receivedBefore.toISOString())
        .orderBy(inequality.fieldPath)
        .limit(limit)
        .get();
      return snapshot.docs.map((doc) => doc.data() as InboxRecord);
    },
  };
}

export function createFirestoreSuppressionLog(db: Firestore): SuppressionLog {
  const collection = db.collection(COLLECTIONS.suppressions);
  return {
    async record(entry: SuppressionRecord): Promise<void> {
      // Keyed on (event, rule) rather than an auto id: a retried worker that suppresses the same
      // event for the same reason twice should leave one row, or the log turns a retry storm into
      // what looks like a storm of suppressions.
      await collection.doc(encodeDocumentId(`${entry.eventId}__${entry.rule}`)).set(entry);
    },
  };
}
