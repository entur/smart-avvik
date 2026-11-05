// The Firestore alert ledger: one decision per event, inside one transaction.
//
// The transaction is the whole point. Split the duplicate check, the standing-state read, the rule
// application and the state write across separate round trips and two workers racing on one inbox
// record both read "nothing applied", both decide to dispatch, and the traveller is notified twice
// about one train. Firestore's transactions are the only thing here that makes "one native decision"
// a property of the system rather than a hope about timing.
//
// Read the class comment in the client package's `ledger.ts` for why an UNSETTLED decision is
// replayed rather than reported as a duplicate. It is the single most costly thing to get wrong in
// this file: reporting it as a duplicate turns every retryable dispatch failure into a notification
// nobody ever receives, and it does so silently, because a suppression looks exactly like a correct
// suppression in a log.

import type { Firestore, Transaction } from "firebase-admin/firestore";

import {
  decideSuppression,
  type AlertLedger,
  type AppliedAlert,
  type DisplayResolver,
  type NotificationEvent,
  type PolicyDecision,
} from "@entur/smart-avvik-notifications-client";

import { COLLECTIONS, encodeDocumentId } from "./collections.ts";

interface DecisionDocument {
  decision: PolicyDecision;
  settled: boolean;
  decidedAt: string;
  alertId: string;
}

export function createFirestoreLedger(db: Firestore): AlertLedger {
  const decisions = db.collection(COLLECTIONS.decisions);
  const alerts = db.collection(COLLECTIONS.alerts);

  return {
    async decide(
      event: NotificationEvent,
      options: { display?: DisplayResolver } = {},
    ): Promise<PolicyDecision> {
      const decisionRef = decisions.doc(encodeDocumentId(event.event_id));
      const alertRef = alerts.doc(encodeAlertId(event.alert_id));

      return db.runTransaction(async (transaction: Transaction) => {
        // BOTH reads before ANY write. Firestore rejects a transaction that reads after writing,
        // and the rejection is a runtime error rather than a type error -- so the ordering here is
        // load-bearing rather than stylistic.
        const decisionSnapshot = await transaction.get(decisionRef);
        const alertSnapshot = await transaction.get(alertRef);

        if (decisionSnapshot.exists) {
          const stored = decisionSnapshot.data() as DecisionDocument;
          if (stored.settled) {
            // Rule 1 through the policy rather than by returning a literal, so the drop reads the
            // same here as it does in a generic receiver and in the unit tests.
            return decideSuppression(event, { duplicate: true, applied: null }, options);
          }
          // Decided, not finished. Replay verbatim: the dispatch is still owed.
          return stored.decision;
        }

        const applied = alertSnapshot.exists
          ? (alertSnapshot.data() as AppliedAlert)
          : null;
        const decision = decideSuppression(event, { duplicate: false, applied }, options);

        const document: DecisionDocument = {
          decision,
          // A drop is finished the moment it is decided: there is nothing left to dispatch, so a
          // later re-entry must read as a duplicate rather than replay the drop.
          settled: decision.action === "drop",
          decidedAt: new Date().toISOString(),
          alertId: event.alert_id,
        };
        transaction.set(decisionRef, document);
        if (decision.action === "dispatch") {
          transaction.set(alertRef, {
            ...decision.next,
            alertId: event.alert_id,
            updatedAt: new Date().toISOString(),
          });
        }
        return decision;
      });
    },

    async settle(eventId: string): Promise<void> {
      await decisions.doc(encodeDocumentId(eventId)).set(
        { settled: true, settledAt: new Date().toISOString() },
        { merge: true },
      );
    },

    async standing(alertId: string): Promise<{ revision: number; phase: string } | null> {
      const snapshot = await alerts.doc(encodeAlertId(alertId)).get();
      if (!snapshot.exists) {
        return null;
      }
      const data = snapshot.data() as AppliedAlert;
      return { revision: data.revision, phase: data.phase };
    },
  };
}

/**
 * The document id for an `alert_id`. See `encodeDocumentId` for why every wire-supplied id is
 * encoded rather than used raw.
 */
export function encodeAlertId(alertId: string): string {
  return encodeDocumentId(alertId);
}
