// The inbox and the alert ledger: the CONTRACTS, plus an in-memory implementation.
//
// Two stores, two jobs, and conflating them is the mistake this file exists to prevent.
//
//   * The **inbox** makes one signed HTTP delivery into one durable record. Its key is the
//     `event_id`, which is deterministic over (alert_id, revision) on the sending side, so a
//     redelivery of the same event -- concurrently, or after a restart, both of which are normal
//     under at-least-once -- creates nothing and is acknowledged. It is written BEFORE the 2xx and
//     read by a retry-enabled trigger afterwards.
//   * The **ledger** makes one inbox record into one native DECISION. It holds the standing revision
//     and phase per `alert_id`, and it applies the three suppression rules inside one transaction so
//     that two workers racing on one record cannot both decide.
//
// The distinction that costs a notification when it is got wrong: a decision that has been MADE is
// not the same as a dispatch that has FINISHED. `decide()` therefore records the decision with
// `settled: false` and replays it verbatim on re-entry until `settle()` marks it done. Reporting an
// unsettled decision as a duplicate would turn every retryable dispatch failure -- an FCM 503, a
// cold start that timed out -- into a notification the traveller never receives, and it would do so
// silently, because a suppression looks exactly like a correct suppression in a log.
//
// So: exactly one decision, at-least-once dispatch. That is the contract the sender publishes, and
// the receiver cannot do better than it -- a crash between the dispatch and `settle()` re-dispatches,
// which is the residual duplicate the partner's own notification tag absorbs.

import type { NotificationEvent } from "./contracts.ts";
import { decideSuppression, type DisplayResolver, type PolicyDecision } from "./policy.ts";

/** Why an inbox record is no longer pending. */
export type InboxOutcome = "dispatched" | "suppressed" | "expired" | "failed";

export interface InboxRecord {
  eventId: string;
  /**
   * The delivery id from the `webhook-id` header: stable across the sender's attempts, so two
   * attempts at one delivery share it while two revisions about one train do not. Recorded for
   * tracing; it is NOT the dedup key, because a re-derived event id must dedupe too.
   */
  webhookId: string;
  /** ISO-8601, UTC. When the receiver accepted the request. */
  receivedAt: string;
  /**
   * The exact bytes that were signed, base64-encoded.
   *
   * Stored, rather than only the parsed event, so the trigger works from what actually arrived. It
   * is also the only thing that can settle a later "did the signature really cover this?" question,
   * and it is what makes the inbox record self-contained if the parsed shape ever has to be
   * re-derived under a newer build.
   */
  bodyBase64: string;
  event: NotificationEvent;
  status: "pending" | InboxOutcome;
  /** How many times a trigger has started work on this record. */
  attempts: number;
}

export interface InboxStore {
  /**
   * Persist a record, or report that its `event_id` is already present.
   *
   * MUST NOT overwrite. A create-if-absent is the whole dedup guarantee: an implementation that
   * writes unconditionally turns a redelivery into a second pending record, and therefore into a
   * second trigger, a second decision and a second notification.
   */
  put(record: InboxRecord): Promise<"created" | "duplicate">;
  get(eventId: string): Promise<InboxRecord | null>;
  /**
   * Record the terminal outcome of a record's processing.
   *
   * MUST NOT move a record off a status that is already terminal. A re-entry -- an Eventarc
   * redelivery, a sweep racing the trigger -- would otherwise rewrite `dispatched` as `suppressed`
   * or `expired`, destroying the only record of what actually happened to the event.
   */
  settle(eventId: string, outcome: InboxOutcome, detail?: string): Promise<void>;
  /** Note that a trigger has started work on this record. */
  noteAttempt(eventId: string): Promise<void>;
  /**
   * Records still `pending` whose `receivedAt` is at or before `receivedBefore`.
   *
   * This is what the recovery sweep reads. Bounded by `limit` because it runs on a schedule with a
   * deadline of its own, and an unbounded read of a backlog is how a sweep becomes the outage.
   */
  pending(query: { receivedBefore: Date; limit: number }): Promise<InboxRecord[]>;
}

/** A recorded suppression. Written for every drop, so a missing notification has a reason. */
export interface SuppressionRecord {
  eventId: string;
  alertId: string;
  rule: string;
  detail: string;
  at: string;
}

export interface SuppressionLog {
  record(entry: SuppressionRecord): Promise<void>;
}

export interface AlertLedger {
  /**
   * Decide once, atomically, what to do with this event -- and replay that decision on re-entry.
   *
   * An implementation MUST perform the duplicate check, the standing-state read, the rule
   * application and the state write in ONE transaction. Split across two round trips, two workers
   * racing on one record both read "nothing applied", both decide to dispatch, and the traveller is
   * notified twice about one train.
   */
  decide(event: NotificationEvent, options?: { display?: DisplayResolver }): Promise<PolicyDecision>;
  /** Mark this event's dispatch finished, so a later re-entry reports it as a duplicate. */
  settle(eventId: string): Promise<void>;
  /** The standing state for an alert, for tests and for an operator answering "what do we hold?". */
  standing(alertId: string): Promise<{ revision: number; phase: string } | null>;
}

/**
 * An in-memory inbox, ledger and suppression log.
 *
 * Not a stub: it is what a generic Node receiver runs behind a single process, and it is what the
 * policy and wire suites exercise so that they need no Firebase at all. Its atomicity is real for
 * the reason JavaScript's is real -- `decide()` contains no `await` between the read and the write,
 * so no other task can interleave. A Firestore-backed implementation has to buy the same property
 * with a transaction, and `firebase-notifications` does.
 */
export function createMemoryStores(): {
  inbox: InboxStore;
  ledger: AlertLedger;
  suppressions: SuppressionLog;
  /** Everything recorded, for assertions. */
  readonly state: {
    inbox: Map<string, InboxRecord>;
    alerts: Map<string, { revision: number; phase: string }>;
    decisions: Map<string, { decision: PolicyDecision; settled: boolean }>;
    suppressions: SuppressionRecord[];
  };
} {
  const inboxRecords = new Map<string, InboxRecord>();
  const alerts = new Map<string, { revision: number; phase: string }>();
  const decisions = new Map<string, { decision: PolicyDecision; settled: boolean }>();
  const suppressions: SuppressionRecord[] = [];

  const inbox: InboxStore = {
    async put(record) {
      if (inboxRecords.has(record.eventId)) {
        return "duplicate";
      }
      inboxRecords.set(record.eventId, { ...record });
      return "created";
    },
    async get(eventId) {
      const found = inboxRecords.get(eventId);
      return found ? { ...found } : null;
    },
    async settle(eventId, outcome) {
      const found = inboxRecords.get(eventId);
      // Only ever off `pending`. A terminal status is the record of what happened to this event, and
      // a second worker settling it again -- for a duplicate, or past its validity -- would rewrite
      // that record with its own, later, and much less informative verdict. The belt to
      // `process()`'s braces: the guard there stops the common case, this stops the race.
      if (found && found.status === "pending") {
        found.status = outcome;
      }
    },
    async noteAttempt(eventId) {
      const found = inboxRecords.get(eventId);
      if (found) {
        found.attempts += 1;
      }
    },
    async pending({ receivedBefore, limit }) {
      return [...inboxRecords.values()]
        .filter(
          (record) =>
            record.status === "pending" && Date.parse(record.receivedAt) <= receivedBefore.getTime(),
        )
        .sort((left, right) => left.receivedAt.localeCompare(right.receivedAt))
        .slice(0, limit)
        .map((record) => ({ ...record }));
    },
  };

  const ledger: AlertLedger = {
    async decide(event, options = {}) {
      // No `await` from here to the writes below -- see the class comment. Adding one would make
      // this implementation lie about the property the Firestore one buys with a transaction.
      const previous = decisions.get(event.event_id);
      if (previous) {
        if (previous.settled) {
          return decideSuppression(event, { duplicate: true, applied: null }, options);
        }
        return previous.decision;
      }
      const applied = alerts.get(event.alert_id) ?? null;
      const decision = decideSuppression(
        event,
        {
          duplicate: false,
          applied: applied
            ? { revision: applied.revision, phase: applied.phase as NotificationEvent["phase"] }
            : null,
        },
        options,
      );
      decisions.set(event.event_id, { decision, settled: decision.action === "drop" });
      if (decision.action === "dispatch") {
        alerts.set(event.alert_id, decision.next);
      }
      return decision;
    },
    async settle(eventId) {
      const previous = decisions.get(eventId);
      if (previous) {
        previous.settled = true;
      }
    },
    async standing(alertId) {
      const found = alerts.get(alertId);
      return found ? { ...found } : null;
    },
  };

  const suppressionLog: SuppressionLog = {
    async record(entry) {
      suppressions.push({ ...entry });
    },
  };

  return {
    inbox,
    ledger,
    suppressions: suppressionLog,
    state: { inbox: inboxRecords, alerts, decisions, suppressions },
  };
}
