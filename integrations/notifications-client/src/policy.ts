// R14's three suppression rules, as pure functions with no Firebase dependency.
//
// Delivery is at-least-once with no ordering guarantee. That is published rather than apologised
// for, and it is exactly why these rules are SUPPLIED instead of left as homework: every one of them
// is a rule whose absence produces a plausible-looking notification about a train that is not in the
// state the notification claims.
//
// Keyed on `alert_id`, whose grain is (subscription, train) and NOT the train alone. A per-train key
// would make rule 2 suppress the SECOND commuter's notification about the same disrupted train --
// one client serves many commuters through one receiver, and the highest revision seen for a train
// says nothing about what a different subscription has been told. The event schema's `alert_id`
// description records this; the train's own fields ride along for display and for a client's joins,
// and must never be used as the identity.
//
// No I/O, no clock, no Firebase: everything the decision needs is an argument. That is what lets the
// same rules run in a generic Node receiver, in a Firestore transaction, and in a unit test with a
// literal for prior state.

import type { EventPhase, NotificationEvent } from "./contracts.ts";

/**
 * How the client's own app should present a dispatched event.
 *
 * `silent` means deliver the data without waking the traveller: the notification should replace what
 * is already on the lock screen rather than arrive as a second alarm. `alert` means present it.
 * The client supplies the actual presentation; this is the DEFAULT the supplied policy chooses, and
 * a client is free to override it.
 */
export type Display = "alert" | "silent";

/**
 * How a client chooses presentation. Takes the whole event, not just the phase: an integrator may
 * legitimately want to key on the operator, the line or the hour, and narrowing the argument here
 * would make that a fork of the package rather than a configuration.
 */
export type DisplayResolver = (event: NotificationEvent) => Display;

export type SuppressionRule = "duplicate_event" | "stale_revision" | "already_confirmed";

/**
 * What the receiver has already applied for one `alert_id`.
 *
 * `phase` is the phase of the highest applied revision -- the state that STANDS -- not a
 * high-water mark over every phase ever seen. That distinction is load-bearing for rule 3: a
 * withdrawal legitimately supersedes a confirmation (a cancelled train can be reinstated, and the
 * sender mints `withdrawn` for any standing alert that falls below the threshold), and after a
 * withdrawal a fresh `predicted` is a real lifecycle transition the sender does mint. Reading rule 3
 * as "ever confirmed" would silence every alert about that train for the rest of the day.
 */
export interface AppliedAlert {
  revision: number;
  phase: EventPhase;
}

export interface PriorState {
  /**
   * Whether this exact `event_id` has already been decided AND finished.
   *
   * `event_id` is deterministic over (alert_id, revision), so the same decision re-run by the sender
   * mints the same id and this is what recognises it. An event whose decision was made but whose
   * native dispatch has not finished is NOT a duplicate -- see `ledger.ts`: reporting it as one
   * would turn a retryable dispatch failure into a notification nobody ever receives.
   */
  duplicate: boolean;
  /** The standing state for this `alert_id`, or null if nothing has been applied for it. */
  applied: AppliedAlert | null;
}

export type PolicyDecision =
  | { action: "dispatch"; display: Display; next: AppliedAlert }
  | { action: "drop"; rule: SuppressionRule; detail: string };

/**
 * The default presentation for an event.
 *
 * A confirmation defaults to `silent`. The traveller has already been alerted by the forecast this
 * confirmation replaces -- that is what a confirmation IS -- so presenting it as a second alarm
 * charges them twice for one disruption. It still has to arrive, because the fact changed from "91 %
 * chance of cancellation" to "cancelled", and a client replacing the visible notification in place
 * is the behaviour `/varsel` itself has: one notification tag per train per registration, so the
 * confirmation replaces the forecast on the lock screen rather than stacking beneath it.
 *
 * A withdrawal is also `silent`: it retracts a warning rather than raising one.
 */
export function defaultDisplay(event: NotificationEvent): Display {
  return event.phase === "predicted" ? "alert" : "silent";
}

/**
 * Apply the three rules, in the published order, and say what to do.
 *
 * The order is the receiver decision order the sender publishes, and it is not interchangeable.
 * Rule 1 before rule 2, because a duplicate of the highest applied revision is not "stale" -- it is
 * the same event arriving twice, which is normal under at-least-once and is not worth a
 * revision-ordering explanation in a log. Rule 2 before rule 3, because an out-of-order forecast is
 * the ordinary case and the terminal-phase rule is the one that catches what ordering alone cannot.
 */
export function decideSuppression(
  event: NotificationEvent,
  prior: PriorState,
  options: { display?: DisplayResolver } = {},
): PolicyDecision {
  // Rule 1: drop a duplicate event id.
  if (prior.duplicate) {
    return {
      action: "drop",
      rule: "duplicate_event",
      detail: "this event_id has already been decided and dispatched",
    };
  }

  const applied = prior.applied;

  // Rule 2: drop a revision not STRICTLY newer than the highest applied for this alert_id.
  // `<=` and not `<`: an equal revision carrying a different phase would otherwise be applied on
  // top of itself, and since `event_id` is deterministic over (alert_id, revision) two events with
  // one revision and two phases cannot both be authentic.
  if (applied && event.revision <= applied.revision) {
    return {
      action: "drop",
      rule: "stale_revision",
      detail: `revision ${event.revision} is not newer than the applied ${applied.revision}`,
    };
  }

  // Rule 3: never dispatch a `predicted` revision for a train already `confirmed`, WHATEVER its
  // revision number. Rule 2 already catches a lower-numbered forecast, so this rule exists for the
  // case rule 2 admits: a `predicted` with a HIGHER revision than the confirmation. The sender
  // never mints confirmed -> predicted, so such an event is a bug or a forgery either way -- and
  // presenting it would tell a traveller that a train we know is cancelled might merely be late,
  // which is the single worst thing this channel can say.
  if (applied && applied.phase === "confirmed" && event.phase === "predicted") {
    return {
      action: "drop",
      rule: "already_confirmed",
      detail: `alert ${event.alert_id} is confirmed at revision ${applied.revision}; a forecast at revision ${event.revision} is not dispatched`,
    };
  }

  const display = (options.display ?? defaultDisplay)(event);
  return {
    action: "dispatch",
    display,
    next: { revision: event.revision, phase: event.phase },
  };
}

/**
 * Whether an event is still worth acting on at `now`.
 *
 * `valid_until` is the earlier of the sender's 60-minute horizon and the train's departure, and it
 * bounds the RECEIVER's dispatch window: past it the event is expired rather than failed. A
 * notification about a train that has already left is not a late warning, it is noise -- and worse,
 * it teaches the traveller that these notifications are not about anything they can act on.
 *
 * The subscription's own lifetime is deliberately NOT one of the terms: the sender enforces that at
 * dispatch by re-reading the live subscription, so an event may legitimately carry a `valid_until`
 * that outlives the subscription by up to an hour. The receiver's own recipient recheck is what
 * closes that gap, which is why `eligibility` in `client.ts` is a separate hook.
 */
export function isStillValid(event: NotificationEvent, now: Date): boolean {
  const validUntil = Date.parse(event.valid_until);
  if (Number.isNaN(validUntil)) {
    // An unparseable timestamp is not treated as "valid forever". Failing closed here costs one
    // notification; failing open costs a stream of them about departed trains.
    return false;
  }
  return now.getTime() <= validUntil;
}
