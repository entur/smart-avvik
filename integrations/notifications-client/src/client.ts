// The receiver, transport-agnostic: the published decision order in one place.
//
// Two halves, and the split between them is the whole design rather than a tidying choice.
//
//   `receive()`  -- runs on the HTTP request. Size, signature, envelope, inbox write, 2xx. Nothing
//                   else. It is deliberately incapable of dispatching a notification.
//   `process()`  -- runs on the retry-enabled trigger the inbox write fires. Policy, state,
//                   eligibility, recheck, dispatch.
//
// Why they are not one function, in the words the Firebase lifetime documentation uses: a function
// that has returned its response CANNOT ACCESS THE CPU, and work left unawaited after the response
// may not run at all -- or may resume, unpredictably, inside a LATER request, where it would see
// that request's context. So "reply 2xx and then send the notification" is not a shortcut with a
// small failure rate; it is a program whose notification arrives on some other traveller's request
// or not at all. Persisting first and letting a trigger do the work is the only shape that both
// acknowledges promptly and does the work reliably.

import { parseEvent, type NotificationEvent } from "./contracts.ts";
import type { AlertLedger, InboxRecord, InboxStore, SuppressionLog } from "./ledger.ts";
import { isStillValid, type Display, type DisplayResolver } from "./policy.ts";
import { verifySignature } from "./verify.ts";

/**
 * The inbound size ceiling, in bytes.
 *
 * 64 KiB. The largest event the sender can produce is a confirmed cancellation with several
 * contributing situations, their operator-authored summaries and an alternative journey -- low
 * single-digit kilobytes, and the `norwegian-body` signing vector is 400 bytes. So this is not a fit
 * bound; it is an abuse bound on a public unauthenticated endpoint, sized with two orders of
 * magnitude of headroom so a legitimate event can never approach it. The number matters less than
 * WHERE it is enforced: before the body is read, before the MAC is computed, and before a single
 * store call.
 */
export const DEFAULT_MAX_BODY_BYTES = 64 * 1024;

/**
 * How long after acceptance a record may still be processed, in milliseconds.
 *
 * The trigger retries a thrown error for up to 24 hours. That is right for a store outage and wrong
 * for a bug: a receiver whose dispatch code throws on some payload would otherwise re-run it every
 * few minutes for a day, and every one of those runs is a chance of a duplicate notification about a
 * train that left hours ago. 15 minutes sits inside the event's own 60-minute validity horizon and
 * far inside the trigger's window, so a permanent fault is abandoned while it is still cheap.
 *
 * This is a SECOND cutoff, not a replacement for `valid_until`: a record can go stale by either, and
 * the earlier one wins.
 */
export const DEFAULT_PROCESSING_CUTOFF_MS = 15 * 60 * 1000;

export type ReceiveOutcome =
  | "too_large"
  | "unauthorized"
  | "bad_envelope"
  | "accepted"
  | "duplicate"
  | "store_unavailable";

export interface ReceiveResult {
  /**
   * The HTTP status to answer with.
   *
   * Chosen against the SENDER's own classification, which is published: 410 pauses the destination,
   * other 4xx are terminal for that event, and 408, 429 and 5xx are retried. So 413, 401 and 400
   * are all correct -- none of those conditions fixes itself for this event, and asking the sender
   * to retry an oversized or unsigned body would be a busy loop. 503 is the one answer that means
   * "come back", and it is reserved for the store.
   */
  status: 200 | 400 | 401 | 413 | 503;
  outcome: ReceiveOutcome;
  detail?: string;
  event?: NotificationEvent;
}

export interface ReceiveRequest {
  headers: Readonly<Record<string, string | string[] | undefined>>;
  /**
   * The exact bytes received, or `null` when the transport refused to buffer them.
   *
   * `null` with an over-limit `declaredBytes` is how a transport that CAN reject early says so, and
   * `receive()` answers 413 without touching it.
   */
  body: Uint8Array | null;
  /**
   * The declared length, from `Content-Length`, or `null` when absent.
   *
   * Consulted FIRST and on its own, because it is the only length available before the body is
   * read. It is also attacker-controlled, so the actual byte count is checked too -- a header that
   * under-declares a large body must not buy its way past the ceiling.
   */
  declaredBytes: number | null;
}

export interface ReceiveConfig {
  /**
   * Every secret that may sign for the destination this request arrived on, active generation first.
   *
   * An empty array means the receiver holds no credential for this destination -- an unknown
   * partner, or a destination whose secrets were revoked. That is answered 401 with the same status
   * and body as a bad signature, deliberately: distinguishing them would turn the endpoint into an
   * oracle for which destinations exist.
   */
  secrets: readonly string[];
  inbox: InboxStore;
  maxBodyBytes?: number;
  now?: () => Date;
}

/**
 * The request half: size, signature, envelope, inbox, 2xx. In that order, and the order is the
 * contract.
 */
export async function receive(
  config: ReceiveConfig,
  request: ReceiveRequest,
): Promise<ReceiveResult> {
  const limit = config.maxBodyBytes ?? DEFAULT_MAX_BODY_BYTES;
  const now = (config.now ?? (() => new Date()))();

  // 1. Size, from the declared length alone where it is present. Nothing has been read, nothing
  //    verified, nothing stored.
  if (request.declaredBytes !== null && request.declaredBytes > limit) {
    return {
      status: 413,
      outcome: "too_large",
      detail: `declared ${request.declaredBytes} bytes exceeds ${limit}`,
    };
  }
  if (request.body === null) {
    // The transport declined to buffer. With a declared length inside the limit that is not a size
    // problem, so answering 413 would be a lie -- but there is no body to verify either.
    return { status: 400, outcome: "bad_envelope", detail: "no body" };
  }
  if (request.body.byteLength > limit) {
    return {
      status: 413,
      outcome: "too_large",
      detail: `${request.body.byteLength} bytes exceeds ${limit}`,
    };
  }

  // 2. Signature, over the exact bytes.
  const verified = verifySignature({
    secrets: config.secrets,
    headers: request.headers,
    body: request.body,
    nowSeconds: Math.floor(now.getTime() / 1000),
  });
  if (!verified.ok) {
    return { status: 401, outcome: "unauthorized", detail: verified.failure };
  }

  // 3. Envelope. After verification: parsing unauthenticated bytes is work an attacker can ask for
  //    for free, and a parse failure on a signed body means something quite different -- a version
  //    this build does not know -- which is worth a distinct answer.
  const parsed = parseEvent(request.body);
  if (!parsed.ok) {
    return {
      status: 400,
      outcome: "bad_envelope",
      detail: `${parsed.problem}: ${parsed.detail}`,
    };
  }

  // 4. Persist BEFORE the 2xx. A store failure is answered 503, which the sender retries: the event
  //    is still ours to deliver. Acknowledging first and storing after would acknowledge an event
  //    that then existed nowhere.
  const record: InboxRecord = {
    eventId: parsed.event.event_id,
    webhookId: verified.webhookId,
    receivedAt: now.toISOString(),
    bodyBase64: Buffer.from(request.body).toString("base64"),
    event: parsed.event,
    status: "pending",
    attempts: 0,
  };
  let written: "created" | "duplicate";
  try {
    written = await config.inbox.put(record);
  } catch (error) {
    return { status: 503, outcome: "store_unavailable", detail: (error as Error).message };
  }
  return {
    status: 200,
    outcome: written === "created" ? "accepted" : "duplicate",
    event: parsed.event,
  };
}

/** Whether the traveller is presented with this event, and to whom. Supplied by the partner. */
export interface DispatchRequest {
  event: NotificationEvent;
  display: Display;
  recipients: readonly unknown[];
}

export type Eligibility = { ok: true } | { ok: false; reason: string };

export interface ProcessAdapters {
  inbox: InboxStore;
  ledger: AlertLedger;
  suppressions: SuppressionLog;
  /**
   * Recipient lookup for a LIVE subscription.
   *
   * MUST NOT be called for a test event, and `process()` does not: a synthetic event reaching a real
   * recipient map is the failure R15's immutable `test` marker exists to prevent, and the marker
   * rides inside the signed body precisely so a receiver can act on it before native dispatch.
   */
  recipients(event: NotificationEvent): Promise<readonly unknown[]>;
  /**
   * Recipient lookup for a TEST subscription.
   *
   * Absent means the adapter has NOT opted into test events, and a test event is then suppressed
   * rather than routed anywhere. Opting in is a deliberate act by the integrator, not a default.
   */
  testRecipients?(event: NotificationEvent): Promise<readonly unknown[]>;
  /**
   * R13's recheck, immediately before dispatch: is the subscription still active, is the departure
   * still ahead, is anything else the partner knows still true?
   *
   * Called AFTER the ledger has applied the state, which is the flowchart's own order and is
   * deliberate: an ineligible event still carried the newest fact about that train, and forgetting
   * it would let a later, older revision be applied on top.
   */
  eligibility(event: NotificationEvent): Promise<Eligibility>;
  dispatch(request: DispatchRequest): Promise<void>;
  display?: DisplayResolver;
  now?(): Date;
  processingCutoffMs?: number;
}

export type ProcessOutcome =
  | { outcome: "dispatched"; recipients: number }
  | { outcome: "suppressed"; rule: string; detail: string }
  | { outcome: "expired"; detail: string }
  | { outcome: "already_settled"; status: string };

/**
 * A failure the caller should retry.
 *
 * Thrown out of `process()`, which is what makes a retry-enabled trigger re-run. Everything
 * `process()` decides for itself -- an unknown schema, an expired event, no recipient, a suppression
 * -- returns NORMALLY, because re-running it would reach the same conclusion at the same cost. Only
 * a fault that might not recur is worth a retry, and the store is the archetype.
 */
export class TransientReceiverError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "TransientReceiverError";
  }
}

/**
 * A failure the caller should NOT retry.
 *
 * For an adapter to say "this payload will never work" from inside `dispatch` -- an unregistered
 * device token, a recipient the partner has since deleted. Recorded as a suppression and settled.
 */
export class TerminalReceiverError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "TerminalReceiverError";
  }
}

/**
 * The trigger half: policy, state, eligibility, dispatch.
 *
 * Throws only `TransientReceiverError` and whatever an adapter throws that is not
 * `TerminalReceiverError` -- a raw store error included, which is the safe default direction: an
 * unclassified fault is more often a store than a payload, and the staleness cutoff is what stops a
 * misclassified one retrying all day.
 */
export async function process(
  adapters: ProcessAdapters,
  record: InboxRecord,
): Promise<ProcessOutcome> {
  const now = (adapters.now ?? (() => new Date()))();
  const cutoffMs = adapters.processingCutoffMs ?? DEFAULT_PROCESSING_CUTOFF_MS;

  // Re-read the record before anything else, and stop if it is no longer `pending`.
  //
  // The argument is the trigger's snapshot of the document AS CREATED, so its `status` is always
  // `pending` however many times the trigger re-fires -- it is not a reading of the current state and
  // must not be trusted as one. Without this read, a re-entry on a finished record walks the whole
  // function again and OVERWRITES its terminal status: a dispatched event re-fired after a crash
  // becomes `suppressed` (the ledger reports the duplicate, and the suppression path settles the
  // inbox), and one re-fired past its `valid_until` becomes `expired` -- which reads to an operator
  // as a notification that was never sent, in the one package whose whole promise is that a missing
  // notification has a reason. Neither costs a duplicate notification; both destroy the record of
  // what actually happened, which is worse than it sounds because it is the only record there is.
  const current = await adapters.inbox.get(record.eventId);
  if (current && current.status !== "pending") {
    return { outcome: "already_settled", status: current.status };
  }
  // `current` where it exists: `receivedAt` and `attempts` are then the stored values rather than a
  // snapshot's. A null means the record has been deleted under us, and the passed copy is all there
  // is -- proceeding with it is right, since the work was accepted.
  const working = current ?? record;
  const event = working.event;

  await adapters.inbox.noteAttempt(working.eventId);

  // Staleness first, at entry, and BOTH terms. Past either, nothing else about this record matters:
  // no policy, no state, no dispatch, and above all no throw -- a throw here is what would keep a
  // permanently-failing record alive for the trigger's full 24-hour window.
  const stale = staleness(working, now, cutoffMs);
  if (stale) {
    await settleExpired(adapters, working, now, stale);
    return { outcome: "expired", detail: stale };
  }

  // One decision, atomically, replayed verbatim on re-entry. See `ledger.ts`.
  const decision = await adapters.ledger.decide(
    event,
    adapters.display ? { display: adapters.display } : {},
  );
  if (decision.action === "drop") {
    await adapters.suppressions.record({
      eventId: event.event_id,
      alertId: event.alert_id,
      rule: decision.rule,
      detail: decision.detail,
      at: now.toISOString(),
    });
    await adapters.inbox.settle(working.eventId, "suppressed", decision.rule);
    return { outcome: "suppressed", rule: decision.rule, detail: decision.detail };
  }

  const eligible = await adapters.eligibility(event);
  if (!eligible.ok) {
    return await suppress(adapters, working, now, "ineligible", eligible.reason);
  }

  let recipients: readonly unknown[];
  if (event.test) {
    if (!adapters.testRecipients) {
      return await suppress(
        adapters,
        working,
        now,
        "test_event_not_opted_in",
        "the adapter supplies no isolated test recipient map",
      );
    }
    recipients = await adapters.testRecipients(event);
  } else {
    recipients = await adapters.recipients(event);
  }
  if (recipients.length === 0) {
    return await suppress(
      adapters,
      working,
      now,
      "no_recipient",
      "the subscription maps to no recipient",
    );
  }

  try {
    await adapters.dispatch({ event, display: decision.display, recipients });
  } catch (error) {
    if (error instanceof TerminalReceiverError) {
      return await suppress(adapters, working, now, "dispatch_refused", error.message);
    }
    throw error;
  }
  // Only now is the decision finished, and only now does a re-entry report it as a duplicate. The
  // gap between the dispatch and this line is the residual at-least-once window: a crash inside it
  // re-dispatches, which is what the sender's contract already tells the partner to expect and what
  // a per-train notification tag absorbs on the device.
  await adapters.ledger.settle(working.eventId);
  await adapters.inbox.settle(working.eventId, "dispatched");
  return { outcome: "dispatched", recipients: recipients.length };
}

function staleness(record: InboxRecord, now: Date, cutoffMs: number): string | null {
  if (!isStillValid(record.event, now)) {
    return `past valid_until ${record.event.valid_until}`;
  }
  const received = Date.parse(record.receivedAt);
  if (Number.isNaN(received)) {
    return `unreadable receivedAt ${record.receivedAt}`;
  }
  if (now.getTime() - received > cutoffMs) {
    return `more than ${cutoffMs} ms after acceptance at ${record.receivedAt}`;
  }
  return null;
}

async function settleExpired(
  adapters: ProcessAdapters,
  record: InboxRecord,
  now: Date,
  detail: string,
): Promise<void> {
  await adapters.suppressions.record({
    eventId: record.eventId,
    alertId: record.event.alert_id,
    rule: "expired",
    detail,
    // The CALLER's clock, like `suppress` below and like everything else in `process()`. It read
    // `new Date()` here, which is the two-clocks bug `recovery.ts` names by this repository's own
    // incident: a record selected as stale against one clock and then stamped against another.
    // Milliseconds apart in production and arbitrarily far apart under an injected clock, which is
    // where a receiver's tests live.
    at: now.toISOString(),
  });
  await adapters.inbox.settle(record.eventId, "expired", detail);
}

async function suppress(
  adapters: ProcessAdapters,
  record: InboxRecord,
  now: Date,
  rule: string,
  detail: string,
): Promise<ProcessOutcome> {
  await adapters.suppressions.record({
    eventId: record.eventId,
    alertId: record.event.alert_id,
    rule,
    detail,
    at: now.toISOString(),
  });
  // The ledger decision stays UNSETTLED on purpose for an ineligible or unroutable event: nothing
  // was dispatched, so nothing should report as a duplicate if the sender re-delivers. The inbox
  // record is what stops this receiver re-working it.
  await adapters.inbox.settle(record.eventId, "suppressed", rule);
  return { outcome: "suppressed", rule, detail };
}
